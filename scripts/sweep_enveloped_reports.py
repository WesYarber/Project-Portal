#!/usr/bin/env python3
"""Delete-the-fix sweep for "a report in an envelope is still a report".

Follows all three safety rules in docs/verifying-with-mutations.md:

1. REFUSES a dirty tree, and restores however it dies (atexit + SIGTERM/SIGINT
   that call sys.exit, since a bare handler that returns does not run atexit).
2. Prints `SWEEP COMPLETE` at the end; nothing may read app/ until it appears.
3. The caller re-runs the plain suite afterwards.

Three files, because the decision is spread across three: the envelope reader
in app/unparsedreport.py, the runner that consults it and derives the run
summary in app/agent_runner.py, and the one-shot repair in
deploy/repair_enveloped_reports.py - which writes to Wes's live database and
so earns the same scrutiny as the code that stopped the bug.

Scoped to the owning test files rather than the full suite, per the trade
commit b1dcd35 made: a CAUGHT here is conclusive, while an ESCAPED only means
these files do not hold the line and has to be re-checked more widely.
"""
import atexit, signal, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UR = ROOT / "app" / "unparsedreport.py"
AR = ROOT / "app" / "agent_runner.py"
RP = ROOT / "deploy" / "repair_enveloped_reports.py"
TESTS = [
    "tests/test_unparsed_report.py",
    "tests/test_json_schema.py",
    "tests/test_repair_enveloped_reports.py",
]

if subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=ROOT).returncode != 0:
    sys.exit("REFUSING: tree is dirty. A sweep must start from a committed tree.")

ORIGINAL = {p: p.read_text(encoding="utf-8") for p in (UR, AR, RP)}


def restore():
    for path, text in ORIGINAL.items():
        path.write_text(text, encoding="utf-8")


atexit.register(restore)
for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, lambda *_: sys.exit("killed by signal"))


# (path, label, find, replace). Each is one decision the code makes.
MUTATIONS = [
    # --- app/unparsedreport.py: recognizing a report ---
    (UR, "anything that is a dict counts as a report",
     "    return isinstance(obj, dict) and bool(REPORT_KEYS & obj.keys())",
     "    return isinstance(obj, dict)"),
    (UR, "a report has to carry EVERY contract field to be recognized",
     "    return isinstance(obj, dict) and bool(REPORT_KEYS & obj.keys())",
     "    return isinstance(obj, dict) and REPORT_KEYS <= obj.keys()"),
    # --- app/unparsedreport.py: unwrap_envelope ---
    (UR, "a real report is unwrapped into its own innards",
     "    if not isinstance(structured, dict) or looks_like_report(structured):\n        return None",
     "    if not isinstance(structured, dict):\n        return None"),
    (UR, "a non-dict is walked as if it had values",
     "    if not isinstance(structured, dict) or looks_like_report(structured):\n        return None",
     "    if looks_like_report(structured):\n        return None"),
    (UR, "an envelope holding the object rather than its text is refused",
     "        obj = decode(value) if isinstance(value, str) else value",
     "        obj = decode(value) if isinstance(value, str) else None"),
    (UR, "a stringified envelope is refused",
     "        obj = decode(value) if isinstance(value, str) else value",
     "        obj = value"),
    (UR, "an ambiguous envelope is guessed at instead of refused",
     "    return found[0] if len(found) == 1 else None",
     "    return found[0] if found else None"),
    # --- app/agent_runner.py: consulting it ---
    (AR, "the runner never unwraps, so the envelope stands as the report",
     "        unwrapped = unparsedreport.unwrap_envelope(structured)\n"
     "        if unwrapped is not None:",
     "        unwrapped = unparsedreport.unwrap_envelope(structured)\n"
     "        if False:"),
    (AR, "an unwrapped report is reported as an ordinary structured one",
     '            return unwrapped, "recovered"',
     '            return structured, "structured"'),
    # --- app/agent_runner.py: _human_summary ---
    (AR, "a bullet-less report keeps the CLI's raw JSON as its summary",
     "        result_text = _human_summary(report)",
     "        result_text = _human_summary(report) or result_text"),
    (AR, "the bullets stop being preferred to the journal heading",
     "    bullets = report.get(\"summary\")\n"
     "    if isinstance(bullets, list) and bullets:\n"
     "        return \"; \".join(str(b) for b in bullets)\n",
     "    bullets = report.get(\"summary\")\n"
     "    if False:\n"
     "        return \"; \".join(str(b) for b in bullets)\n"),
    (AR, "an empty bullet list is joined into an empty summary anyway",
     "    if isinstance(bullets, list) and bullets:\n"
     "        return \"; \".join(str(b) for b in bullets)",
     "    if isinstance(bullets, list):\n"
     "        return \"; \".join(str(b) for b in bullets)"),
    (AR, "the journal heading is never used as a fallback",
     "    entry = report.get(\"journal_entry_md\")\n    if isinstance(entry, str):",
     "    entry = report.get(\"journal_entry_md\")\n    if False:"),
    (AR, "the fallback takes the raw first line, hashes and all",
     '            line = line.strip().lstrip("#").strip()',
     "            line = line.strip()"),
    (AR, "a blank leading line ends the search instead of being skipped",
     "            if line:\n                return line\n    return \"\"",
     "            return line\n    return \"\""),
    # --- deploy/repair_enveloped_reports.py: what it will touch ---
    (RP, "prose is treated as a serialization and rewritten",
     '    if not isinstance(text, str) or not text.lstrip().startswith("{"):\n        return None\n'
     "    obj = unparsedreport.decode(text)",
     "    obj = unparsedreport.decode(text) if isinstance(text, str) else None"),
    (RP, "any decodable object is taken for a report",
     "        if unparsedreport.looks_like_report(obj):\n            return obj",
     "        return obj"),
    (RP, "a truncated envelope is given up on",
     "    return _open_truncated_envelope(text)",
     "    return None"),
    (RP, "a cut landing mid-escape is not repaired before closing the string",
     '    while body.endswith("\\\\") and (len(body) - len(body.rstrip("\\\\"))) % 2:\n        body = body[:-1]',
     "    pass"),
    (RP, "the truncated open accepts whatever it decoded, report or not",
     "    return obj if unparsedreport.looks_like_report(obj) else None",
     "    return obj"),
    (RP, "every JSON-looking string counts as a serialized report",
     "    return bool(_CONTRACT_KEY.search(text[:400]))",
     "    return True"),
    (RP, "the summary is not capped to the column width",
     '        return "; ".join(str(b) for b in bullets)[:SUMMARY_CHARS]',
     '        return "; ".join(str(b) for b in bullets)'),
    (RP, "a report with no entry blanks the journal row instead of leaving it",
     '    entry = report.get("journal_entry_md")\n'
     '    return entry if isinstance(entry, str) and entry.strip() else None',
     '    return str(report.get("journal_entry_md") or "")'),
    (RP, "an ambiguous prefix match is used instead of refused",
     "    if len(rows) != 1:\n        return None",
     "    if not rows:\n        return None"),
    (RP, "the journal is never consulted, so a truncated summary stays truncated",
     '        report = _whole_report_for(conn, row["summary"]) or report_in(row["summary"])',
     '        report = report_in(row["summary"])'),
    (RP, "an unrecoverable row is skipped, leaving its JSON on the page",
     "        if report is None and not looks_serialized(row[\"summary\"]):\n            continue",
     "        if report is None:\n            continue"),
    (RP, "report_summary is left alone, so the banner keeps reading the short version",
     "        block = bullet_block(report) if report else (row[\"report_summary\"] or \"\")",
     '        block = row["report_summary"] or ""'),
]

caught = escaped = skipped = 0
for path, label, find, repl in MUTATIONS:
    text = ORIGINAL[path]
    if find not in text:
        print(f"SKIP (pattern missing): {label}", flush=True)
        skipped += 1
        continue
    path.write_text(text.replace(find, repl, 1), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", *TESTS, "-qx", "--no-header", "-p", "no:randomly"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    if r.returncode == 0:
        print(f"ESCAPED: {label}", flush=True)
        escaped += 1
    else:
        print(f"caught:  {label}", flush=True)
        caught += 1
    restore()

print(f"\n{caught} caught, {escaped} escaped, {skipped} skipped, of {len(MUTATIONS)}")
print("SWEEP COMPLETE", flush=True)
