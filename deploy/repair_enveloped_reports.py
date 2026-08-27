#!/usr/bin/env python3
"""Put the prose back where a serialized report is stored instead.

Two bugs, both fixed in app/agent_runner.py on 2026-08-12, left rows behind
that Wes reads every time he opens the affected project:

1. The CLI handed back an argument *envelope* -
   `{"parameter": "<the whole report as a JSON string>"}` - and the portal
   accepted it as the report. Nothing inside had a contract field, so the run
   recorded no bullets and no journal entry, and the envelope's raw JSON
   became both. Runs 962, 973 and 976; journal entries 1709 and 1741 (KvK Day
   4 Scheduler). This is what Wes reported as "this messed up format", and
   because `report_summary` stayed empty the "since you last looked" banner
   fell back to the run summary - so the JSON was on the project page too.
2. A structured report with `summary: []` - which every reflect run has -
   left the CLI's `result` string in place, and under `--json-schema` that
   string is the report's own JSON.

The journal keeps the report whole; the run summary is stored truncated at
500 characters, so a damaged run row is repaired from the journal entry it is
a prefix of. Where nothing can be recovered the summary is emptied rather than
left as JSON: a blank cell is honest, a serialized object is noise.

Dry run by default - it prints what it would change and touches nothing until
`--apply`. Idempotent: a row already carrying prose is not JSON, so it is
skipped, and re-running is safe.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import unparsedreport  # noqa: E402

SUMMARY_CHARS = 500

# The opening of an envelope: one key, then its string value. Used to reach
# inside one that was cut off mid-string, where plain JSON decoding cannot.
_ENVELOPE_OPENING = re.compile(r'^\{\s*"[^"]+"\s*:\s*"')

# A contract field appearing as a JSON key, bare or escaped one level down.
# Evidence that a string is a serialized report even when it will not decode.
_CONTRACT_KEY = re.compile(
    r'\\?"(' + "|".join(sorted(unparsedreport.REPORT_KEYS)) + r')\\?"\s*:'
)


def report_in(text: Optional[str]) -> Optional[dict]:
    """The report a stored string is really a serialization of, or None when
    the string is ordinary prose.

    Prose is the overwhelmingly common case and must never be touched, so the
    cheap `{` test comes first and anything that does not decode to something
    carrying a contract field is left alone.
    """
    if not isinstance(text, str) or not text.lstrip().startswith("{"):
        return None
    obj = unparsedreport.decode(text)
    if isinstance(obj, dict):
        found = unparsedreport.unwrap_envelope(obj)
        if found is not None:
            return found
        if unparsedreport.looks_like_report(obj):
            return obj
    return _open_truncated_envelope(text)


def _open_truncated_envelope(text: str) -> Optional[dict]:
    """The report inside an envelope whose JSON was cut off mid-string.

    A run summary is stored truncated, so the closing quote and brace are
    simply not there and no decoder will touch it. Closing the string by hand
    and salvaging what is inside recovers the leading fields - which is where
    the value is, since `summary` leads the contract.
    """
    match = _ENVELOPE_OPENING.match(text)
    if not match:
        return None
    body = text[match.end() :]
    # A cut can land mid-escape. Drop trailing backslashes until an even
    # number remains, or `"` + body + `"` re-opens the string it closes.
    while body.endswith("\\") and (len(body) - len(body.rstrip("\\"))) % 2:
        body = body[:-1]
    try:
        inner = json.loads('"' + body + '"')
    except json.JSONDecodeError:
        return None
    obj = unparsedreport.decode(inner)
    return obj if unparsedreport.looks_like_report(obj) else None


def looks_serialized(text: Optional[str]) -> bool:
    """True when a stored string is a serialized report, whole or cut off.

    The fallback test for a row that cannot be recovered: it says "this is
    JSON that should have been prose" without claiming to know what it said.
    """
    if not isinstance(text, str) or not text.lstrip().startswith("{"):
        return False
    return bool(_CONTRACT_KEY.search(text[:400]))


def summary_line(report: dict) -> str:
    """The run summary this report should have produced - the same rule the
    runner now applies, so a repaired row and a fresh one read alike."""
    bullets = report.get("summary")
    if isinstance(bullets, list) and bullets:
        return "; ".join(str(b) for b in bullets)[:SUMMARY_CHARS]
    entry = report.get("journal_entry_md")
    if isinstance(entry, str):
        for line in entry.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                return line[:SUMMARY_CHARS]
    return ""


def bullet_block(report: dict) -> str:
    """`report_summary` as db.set_run_report_summary stores it: one bullet a
    line. This is what the banner prefers over the truncated summary, so a
    repair that skipped it would leave the banner reading the short version."""
    bullets = report.get("summary")
    if not isinstance(bullets, list):
        return ""
    return "\n".join(str(b).strip() for b in bullets if str(b).strip())


def journal_body(report: dict) -> Optional[str]:
    """The journal entry the agent wrote, or None if it wrote none.

    None means leave the row alone rather than blank it: a journal entry
    holding the wrong text is bad, and one holding no text at all is worse -
    the run's own account of itself would be gone with nothing in its place.
    """
    entry = report.get("journal_entry_md")
    return entry if isinstance(entry, str) and entry.strip() else None


def _whole_report_for(conn: sqlite3.Connection, truncated: str) -> Optional[dict]:
    """The untruncated report behind a cut-off run summary.

    The same text was written to the journal in full, so the summary is a
    literal prefix of that row. Matching on the prefix rather than on a
    timestamp makes a wrong pairing impossible - there is no run whose
    500-character JSON prefix belongs to another run's report.
    """
    rows = conn.execute(
        "SELECT content_md FROM journal WHERE substr(content_md, 1, ?) = ?",
        (len(truncated), truncated),
    ).fetchall()
    if len(rows) != 1:
        return None
    return report_in(rows[0][0])


def plan(conn: sqlite3.Connection) -> tuple[list, list]:
    """(run fixes, journal fixes).

    A run fix is (id, new_summary, new_report_summary); a journal fix is
    (id, new_content).
    """
    conn.row_factory = sqlite3.Row
    runs = []
    for row in conn.execute(
        "SELECT id, summary, report_summary FROM runs WHERE summary LIKE '{%'"
    ):
        report = _whole_report_for(conn, row["summary"]) or report_in(row["summary"])
        if report is None and not looks_serialized(row["summary"]):
            continue
        summary = summary_line(report) if report else ""
        block = bullet_block(report) if report else (row["report_summary"] or "")
        # No "did this actually change" guard: a repaired summary is prose, so
        # `LIKE '{%'` above stops matching it and a second run selects nothing.
        # A sweep proved a guard here unreachable, and unreachable code that
        # looks like a safety net is worse than none.
        runs.append((row["id"], summary, block))
    entries = []
    for row in conn.execute(
        "SELECT id, content_md FROM journal WHERE content_md LIKE '{%'"
    ):
        report = report_in(row["content_md"])
        if report is None:
            continue
        new = journal_body(report)
        if new is not None and new != row["content_md"]:
            entries.append((row["id"], new))
    return runs, entries


def apply(conn: sqlite3.Connection, runs: list, entries: list) -> None:
    with conn:
        conn.executemany(
            "UPDATE runs SET summary = ?, report_summary = ? WHERE id = ?",
            [(summary, block or None, rid) for rid, summary, block in runs],
        )
        conn.executemany(
            "UPDATE journal SET content_md = ? WHERE id = ?",
            [(new, jid) for jid, new in entries],
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/portal.db")
    ap.add_argument("--apply", action="store_true", help="write the changes")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    runs, entries = plan(conn)
    for rid, summary, _block in runs:
        print(f"run {rid}: summary -> {summary[:90]!r}")
    for jid, new in entries:
        print(f"journal {jid}: entry -> {new.strip().splitlines()[0][:90]!r}")
    print(f"\n{len(runs)} run summaries, {len(entries)} journal entries")
    if not args.apply:
        print("(dry run - pass --apply to write)")
        return 0
    apply(conn, runs, entries)
    print("written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
