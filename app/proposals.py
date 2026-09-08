"""Changes proposed by another portal, and what this one does with them.

The office install (app/nodes.py) follows the public mirror and cannot write
to it: the mirror is a generated copy of this machine's working tree, so a
commit pushed there lasts until the next publish and then arrives at every
follower as a pull that removes it. An agent at the office that improves the
portal - the walmart theme was the first - therefore ends with a patch series
and a note asking Wes to carry it home by hand: "curl ... | git am". He did
that once, on 2026-09-03, and on 2026-09-08 asked for the wire to go away:
"I also want the Walmart node of the portal to be able to push updates to the
git repo and you review and decide whether to approve them or not."

So a follower does not push; the publisher *pulls*, over the same tailnet the
node probe already crosses, and nobody is handed a credential:

- **On any install**, a project workspace's `patches/*.mbox` files are its
  outgoing proposals. That is the directory the office agent already writes
  into, and one file is one series, cut with `git format-patch --stdout`.
  `/api/proposals` lists them, `/api/proposals/<sha>/mbox` serves one, and a
  decision posted back to `/api/proposals/<sha>/decision` lands on that
  project's journal, where the agent that cut the series reads it next run.
- **On the install that publishes**, the node poller asks each registered
  node for its list after every probe, fetches any series it has not seen,
  checks the bytes against the sha it was promised under, and files it as a
  *proposal*: a row, the mailbox under `data/proposals/`, a journal entry on
  the portal's own project, a notification, and a run on that project so an
  agent reviews it. Approving runs `git am -3` on the source checkout; the
  existing self-update machinery then restarts the service, publishes the
  mirror, and pushes the update out to the very node that proposed it.

Identity is the sha256 of the mailbox bytes, on both sides: a series re-cut
after a review is a new file with a new sha and a new proposal, and a series
already seen is never filed twice however often it is listed. Nothing here
trusts what a node *says* about a series: the subjects and the diff shown for
review are parsed from the bytes this install fetched and hashed itself.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app import config, db, mirror, rundiff

log = logging.getLogger("portal.proposals")

# The directory in a workspace that means "these are for the publisher". The
# office agent chose it before this module existed, so it is a convention
# discovered rather than invented.
PATCH_DIR = "patches"
MAX_MBOX_BYTES = 4 * 1024 * 1024
FETCH_TIMEOUT_SEC = 20
DECISION_TIMEOUT_SEC = 8
GIT_TIMEOUT_SEC = 60

STATUSES = ("pending", "approved", "rejected")

_APPLY_LOCK = threading.Lock()


def _proposals_dir() -> Path:
    return Path(config.DATA_DIR) / "proposals"


# --- a series, parsed from its bytes ---------------------------------------

@dataclass
class Commit:
    subject: str
    author: str = ""
    date: str = ""
    body: str = ""
    files: list[rundiff.FileDiff] = field(default_factory=list)

    @property
    def insertions(self) -> int:
        return sum(f.insertions for f in self.files)

    @property
    def deletions(self) -> int:
        return sum(f.deletions for f in self.files)


_FROM_LINE = re.compile(r"^From [0-9a-f]{40} ", re.M)
_PATCH_TAG = re.compile(r"^\s*\[PATCH[^\]]*\]\s*")
_SEPARATOR = re.compile(r"^---\n", re.M)
_DIFF_START = re.compile(r"^diff --git ", re.M)


def _clean_subject(raw: str) -> str:
    return _PATCH_TAG.sub("", " ".join(raw.split())).strip()


def _headers(text: str) -> tuple[dict[str, str], str]:
    """The header block of one mail, folded lines joined the way git joins
    them, and the rest of the text. A header a mail lacks is simply absent."""
    head, _, rest = text.partition("\n\n")
    headers: dict[str, str] = {}
    current = ""
    for line in head.splitlines():
        if line[:1] in (" ", "\t") and current:
            headers[current] += " " + line.strip()
        elif ":" in line:
            current, _, value = line.partition(":")
            current = current.strip().lower()
            headers[current] = value.strip()
    return headers, rest


def _parse_diff(diff_text: str) -> list[rundiff.FileDiff]:
    files: list[rundiff.FileDiff] = []
    for block in rundiff._split_blocks(diff_text):
        path = rundiff._block_path(block) or block[0][len("diff --git a/"):].split(" b/")[0]
        old_path = ""
        for line in block:
            if line.startswith("rename from "):
                old_path = line[len("rename from "):].strip()
            if line.startswith("@@"):
                break
        binary = any(
            line.startswith("Binary files") or line.startswith("GIT binary patch")
            for line in block
        )
        adds = sum(1 for line in block if line.startswith("+") and not line.startswith("+++"))
        dels = sum(1 for line in block if line.startswith("-") and not line.startswith("---"))
        lines, dropped = rundiff._parse_hunks(block, rundiff.MAX_LINES_PER_FILE)
        files.append(rundiff.FileDiff(
            path=path, insertions=adds, deletions=dels, binary=binary,
            old_path=old_path, lines=lines, truncated=dropped,
        ))
    return files


def parse_mbox(data: bytes) -> list[Commit]:
    """The commits of a `git format-patch --stdout` mailbox, in order. Junk
    that is not a mailbox parses as no commits, which the caller treats as a
    series not worth filing."""
    text = data.decode("utf-8", "replace")
    starts = [m.start() for m in _FROM_LINE.finditer(text)]
    commits: list[Commit] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        mail = text[start:end]
        headers, rest = _headers(mail)
        subject = _clean_subject(headers.get("subject", ""))
        if not subject:
            continue
        # The message body ends at the `---` line format-patch writes before
        # the diffstat; the diff itself starts at the first `diff --git`.
        separator = _SEPARATOR.search(rest)
        if separator is not None:
            body, tail = rest[:separator.start()], rest[separator.end():]
        else:
            first_diff = _DIFF_START.search(rest)
            body = rest[:first_diff.start()] if first_diff else rest
            tail = rest[first_diff.start():] if first_diff else ""
        start = _DIFF_START.search(tail)
        diff_text = tail[start.start():] if start else ""
        # format-patch signs off with "-- \n<git version>"; that is not diff.
        sig = diff_text.rfind("\n-- \n")
        if sig >= 0:
            diff_text = diff_text[:sig]
        commits.append(Commit(
            subject=subject,
            author=headers.get("from", ""),
            date=headers.get("date", ""),
            body=body.strip(),
            files=_parse_diff(diff_text) if diff_text else [],
        ))
    return commits


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- the sending side: what this install offers ------------------------------

def _project_of_workspace(path: Path) -> str:
    return path.parent.parent.name


def outgoing(project_slug: Optional[str] = None) -> list[dict]:
    """Every series waiting in a workspace's `patches/`, newest first, with
    whatever decision has been posted back for it. `project_slug` narrows it
    to one project's."""
    root = Path(config.PROJECTS_DIR)
    if not root.exists():
        return []
    decisions = {row["sha"]: row for row in list_rows(direction="out")}
    out: list[dict] = []
    slugs = [project_slug] if project_slug else sorted(p.name for p in root.iterdir() if p.is_dir())
    for slug in slugs:
        patch_dir = root / slug / PATCH_DIR
        if not patch_dir.is_dir():
            continue
        for path in sorted(patch_dir.glob("*.mbox")):
            try:
                if path.stat().st_size > MAX_MBOX_BYTES:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            commits = parse_mbox(data)
            if not commits:
                continue
            sha = sha_of(data)
            decided = decisions.get(sha)
            out.append({
                "sha": sha,
                "project": slug,
                "name": path.name,
                "title": commits[0].subject if len(commits) == 1 else path.stem.replace("-", " "),
                "subjects": [c.subject for c in commits],
                "commits": len(commits),
                "size": len(data),
                "mtime": int(path.stat().st_mtime),
                "status": decided["status"] if decided else "pending",
                "note": decided["note"] if decided else "",
                "decided_at": decided["decided_at"] if decided else None,
                "decided_by": decided["decided_by"] if decided else "",
            })
    out.sort(key=lambda p: p["mtime"], reverse=True)
    return out


def outgoing_bytes(sha: str) -> Optional[bytes]:
    for item in outgoing():
        if item["sha"] == sha:
            path = Path(config.PROJECTS_DIR) / item["project"] / PATCH_DIR / item["name"]
            try:
                data = path.read_bytes()
            except OSError:
                return None
            return data if sha_of(data) == sha else None
    return None


def record_decision(sha: str, verdict: str, note: str, by: str, portal: str) -> Optional[dict]:
    """The publisher's verdict on a series this install offered. Lands on the
    journal of the project whose workspace holds it, which is where the agent
    that cut it looks next run. None when no such series is offered here."""
    item = next((p for p in outgoing() if p["sha"] == sha), None)
    if item is None or verdict not in ("approved", "rejected"):
        return None
    note = (note or "").strip()[:2000]
    conn = db.get_conn()
    with db._LOCK:
        conn.execute(
            """INSERT INTO proposals (direction, sha, node_id, node_name, project_slug, name,
                                      title, subjects_json, size, status, note, created_at,
                                      decided_at, decided_by)
               VALUES ('out', ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(sha) DO UPDATE SET status = excluded.status, note = excluded.note,
                   decided_at = excluded.decided_at, decided_by = excluded.decided_by,
                   node_name = excluded.node_name""",
            (sha, portal[:80], item["project"], item["name"], item["title"],
             json.dumps(item["subjects"]), item["size"], verdict, note,
             db.now(), db.now(), by[:80]),
        )
        conn.commit()
    project = db.get_project_by_slug(item["project"])
    if project is not None:
        where = f" at **{portal}**" if portal else ""
        who = f" by {by}" if by else ""
        if verdict == "approved":
            text = (
                f"Your proposed change `{item['name']}` (*{item['title']}*) was **approved**"
                f"{where}{who}. It lands here with the next update of this portal."
            )
        else:
            text = (
                f"Your proposed change `{item['name']}` (*{item['title']}*) was **rejected**"
                f"{where}{who}. A revised series must be a new file under `{PATCH_DIR}/`; "
                f"the same bytes are never looked at twice."
            )
        if note:
            text += f"\n\n> {note}"
        db.add_journal(project["id"], "system", "status", text)
    return item


# --- the receiving side: rows ---------------------------------------------

def _row(row: Optional[db.sqlite3.Row]) -> Optional[dict]:
    if row is None:
        return None
    out = dict(row)
    out["subjects"] = json.loads(out.pop("subjects_json") or "[]")
    out["applied"] = json.loads(out.pop("applied_json") or "[]")
    return out


def list_rows(direction: str = "in", status: Optional[str] = None) -> list[dict]:
    conn = db.get_conn()
    sql = "SELECT * FROM proposals WHERE direction = ?"
    args: list[Any] = [direction]
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, id DESC"
    return [_row(r) for r in conn.execute(sql, args).fetchall()]


def get(proposal_id: int) -> Optional[dict]:
    conn = db.get_conn()
    return _row(conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone())


def pending() -> list[dict]:
    return list_rows("in", "pending")


def pending_count() -> int:
    try:
        conn = db.get_conn()
        return int(conn.execute(
            "SELECT COUNT(*) FROM proposals WHERE direction = 'in' AND status = 'pending'"
        ).fetchone()[0])
    except db.sqlite3.Error:
        return 0


def _known(sha: str) -> bool:
    conn = db.get_conn()
    return conn.execute("SELECT 1 FROM proposals WHERE sha = ?", (sha,)).fetchone() is not None


def mbox_path(proposal: dict) -> Path:
    return _proposals_dir() / f"{proposal['sha'][:16]}.mbox"


def read_commits(proposal: dict) -> list[Commit]:
    try:
        return parse_mbox(mbox_path(proposal).read_bytes())
    except OSError:
        return []


# --- pulling from the nodes -------------------------------------------------

def _fetch(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "project-portal-node"})
    with urllib.request.urlopen(request, timeout=timeout) as answer:
        return answer.read(MAX_MBOX_BYTES + 1)


def file_series(data: bytes, node: dict, item: dict) -> Optional[dict]:
    """File one fetched series as a proposal. None when the bytes are not the
    series promised (the sha does not match) or not a mailbox at all."""
    sha = sha_of(data)
    if sha != item.get("sha") or len(data) > MAX_MBOX_BYTES:
        return None
    commits = parse_mbox(data)
    if not commits:
        return None
    if _known(sha):
        return None
    _proposals_dir().mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]", "-", str(item.get("name") or "series.mbox"))[:120]
    title = commits[0].subject if len(commits) == 1 else str(item.get("title") or name)
    conn = db.get_conn()
    with db._LOCK:
        cur = conn.execute(
            """INSERT INTO proposals (direction, sha, node_id, node_name, project_slug, name,
                                      title, subjects_json, size, status, created_at)
               VALUES ('in', ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (sha, node.get("id", ""), node.get("name", ""), str(item.get("project") or "")[:120],
             name, title[:300], json.dumps([c.subject for c in commits]), len(data), db.now()),
        )
        conn.commit()
        row = get(int(cur.lastrowid))
    assert row is not None
    mbox_path(row).write_bytes(data)
    return row


def pull(node: dict) -> list[dict]:
    """Fetch every series a node offers that this install has not seen.
    Blocking; never raises. Returns the rows it filed."""
    filed: list[dict] = []
    try:
        listing = json.loads(_fetch(node["url"] + "api/proposals", FETCH_TIMEOUT_SEC).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        log.info("Could not list proposals on %s: %s", node.get("name"), exc)
        return filed
    items = listing.get("proposals") if isinstance(listing, dict) else None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        sha = str(item.get("sha") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", sha) or _known(sha):
            continue
        try:
            data = _fetch(node["url"] + f"api/proposals/{sha}/mbox", FETCH_TIMEOUT_SEC)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            log.warning("Could not fetch proposal %s from %s: %s", sha[:12], node.get("name"), exc)
            continue
        row = file_series(data, node, item)
        if row is None:
            log.warning("Proposal %s from %s did not match its sha or was not a mailbox", sha[:12], node.get("name"))
            continue
        log.info("Filed proposal #%s from %s: %s", row["id"], node.get("name"), row["title"])
        filed.append(row)
    return filed


def pulls_enabled() -> bool:
    """Only the install that publishes reviews proposals: a follower pulling
    from the publisher would be reviewing its own upstream."""
    return mirror.configured()


def pull_all() -> list[dict]:
    from app import nodes  # local: nodes imports this module's announce path
    if not pulls_enabled():
        return []
    filed: list[dict] = []
    for node in nodes.registry():
        filed.extend(pull(node))
    return filed


def unannounced() -> list[dict]:
    conn = db.get_conn()
    return [_row(r) for r in conn.execute(
        "SELECT * FROM proposals WHERE direction = 'in' AND announced_at IS NULL ORDER BY id"
    ).fetchall()]


async def announce(proposal: dict) -> None:
    """Tell the people and the agent. A journal entry on the portal's own
    project, a notification that opens the proposal, and a run on that project
    so it is reviewed without anybody having to ask for one."""
    from app import notify, worker  # local: both import widely

    conn = db.get_conn()
    with db._LOCK:
        conn.execute("UPDATE proposals SET announced_at = ? WHERE id = ?", (db.now(), proposal["id"]))
        conn.commit()
    meta = db.get_project_by_slug(config.META_PROJECT_SLUG)
    count = len(proposal["subjects"])
    plural = "" if count == 1 else "s"
    if meta is not None:
        db.add_journal(
            meta["id"], "system", "status",
            f"**{proposal['node_name'] or proposal['node_id']}** proposes a change from its "
            f"`{proposal['project_slug']}` project: *{proposal['title']}* ({count} commit{plural}, "
            f"{proposal['size'] // 1024} KB). [Review it](/proposals/{proposal['id']}) - approve or "
            f"reject there, or leave it to the run queued to review it.",
        )
        try:
            if db.is_paused(meta) or meta["stage"] != "active":
                db.update_project(meta["id"], stage="active", paused=None)
            await worker.queue_manual_run(meta["id"])
        except Exception:  # noqa: BLE001 - the proposal is filed either way
            log.exception("Could not queue a review run for proposal #%s", proposal["id"])
    try:
        await notify.notify(
            f"Proposed change from {proposal['node_name'] or proposal['node_id']}",
            f"{proposal['title']} ({count} commit{plural}). Tap to review it.",
            project_title=meta["title"] if meta is not None else None,
            project_id=meta["id"] if meta is not None else None,
            navigate=f"/proposals/{proposal['id']}",
        )
    except Exception:  # noqa: BLE001 - a notify channel never costs the proposal
        log.exception("Could not notify about proposal #%s", proposal["id"])


async def announce_new() -> int:
    """Announce whatever was filed and not yet announced. Idempotent: the
    stamp is written before anything else, so a crash mid-announce loses at
    most one notification, never files a second run for the same series."""
    count = 0
    for row in unannounced():
        await announce(row)
        count += 1
    return count


# --- deciding ---------------------------------------------------------------

@dataclass
class Outcome:
    ok: bool
    detail: str = ""
    applied: list[str] = field(default_factory=list)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(config.APP_ROOT), capture_output=True, text=True,
        timeout=GIT_TIMEOUT_SEC,
    )


def _identity_args() -> list[str]:
    """`git am` commits as the local identity, and a checkout with none
    configured refuses with "unable to auto-detect email address". The author
    of each commit stays whoever wrote it on the other portal; this only names
    the committer, and only where nothing else does."""
    done = _git("config", "user.email")
    if done.returncode == 0 and done.stdout.strip():
        return []
    return ["-c", "user.name=Project Portal", "-c", f"user.email=portal@{socket.gethostname()}"]


def _head() -> str:
    done = _git("rev-parse", "HEAD")
    return done.stdout.strip() if done.returncode == 0 else ""


def _set_status(proposal_id: int, status: str, note: str, by: str, applied: Optional[list[str]] = None) -> None:
    conn = db.get_conn()
    with db._LOCK:
        conn.execute(
            """UPDATE proposals SET status = ?, note = ?, decided_at = ?, decided_by = ?,
                      applied_json = ? WHERE id = ?""",
            (status, note[:2000], db.now(), by[:80], json.dumps(applied or []), proposal_id),
        )
        conn.commit()


def _set_note(proposal_id: int, note: str) -> None:
    conn = db.get_conn()
    with db._LOCK:
        conn.execute("UPDATE proposals SET note = ? WHERE id = ?", (note[:2000], proposal_id))
        conn.commit()


def apply(proposal_id: int, by: str, note: str = "") -> Outcome:
    """`git am -3` the series onto the source checkout. Refuses a dirty tree
    and a series that does not apply, leaving the proposal pending with the
    reason on it - both are things a person or the next run can fix, and a
    verdict of "failed" would read as final."""
    proposal = get(proposal_id)
    if proposal is None or proposal["direction"] != "in":
        return Outcome(False, "no such proposal")
    if proposal["status"] != "pending":
        return Outcome(False, f"already {proposal['status']}")
    path = mbox_path(proposal)
    if not path.exists():
        _set_note(proposal_id, "the mailbox file is missing from data/proposals")
        return Outcome(False, "the mailbox file is missing")
    with _APPLY_LOCK:
        if not (Path(config.APP_ROOT) / ".git").exists():
            return Outcome(False, "the source checkout is not a git repository")
        if not mirror.source_clean():
            detail = "the source tree has uncommitted changes; commit or stash them, then approve again"
            _set_note(proposal_id, detail)
            return Outcome(False, detail)
        before = _head()
        # `git am --abort` wants an identity as much as `am` does, so the same
        # fallback covers both.
        identity = _identity_args()
        try:
            done = _git(*identity, "am", "-3", str(path))
        except subprocess.TimeoutExpired:
            _git(*identity, "am", "--abort")
            detail = "git am timed out"
            _set_note(proposal_id, detail)
            return Outcome(False, detail)
        if done.returncode != 0:
            _git(*identity, "am", "--abort")
            reason = (done.stderr.strip() or done.stdout.strip()).splitlines()
            detail = "does not apply cleanly: " + (reason[-1] if reason else "git am failed")
            _set_note(proposal_id, detail)
            return Outcome(False, detail)
        after = _head()
        listed = _git("rev-list", "--reverse", f"{before}..{after}")
        applied = [line.strip() for line in listed.stdout.splitlines() if line.strip()] if listed.returncode == 0 else []
    _set_status(proposal_id, "approved", note, by, applied)
    return Outcome(True, f"applied {len(applied)} commit(s)", applied)


def reject(proposal_id: int, by: str, note: str = "") -> Outcome:
    proposal = get(proposal_id)
    if proposal is None or proposal["direction"] != "in":
        return Outcome(False, "no such proposal")
    if proposal["status"] != "pending":
        return Outcome(False, f"already {proposal['status']}")
    _set_status(proposal_id, "rejected", note, by)
    return Outcome(True, "rejected")


def tell_sender(proposal: dict, portal_name: str) -> bool:
    """Post the verdict back to the node that offered the series. Best effort
    and blocking: the node may be asleep, and the verdict is on this journal
    regardless."""
    from app import nodes  # local

    node = nodes.get(proposal["node_id"]) if proposal.get("node_id") else None
    if node is None or proposal["status"] not in ("approved", "rejected"):
        return False
    body = json.dumps({
        "verdict": proposal["status"],
        "note": proposal.get("note") or "",
        "by": proposal.get("decided_by") or "",
        "applied": proposal.get("applied") or [],
        "portal": portal_name,
    }).encode()
    request = urllib.request.Request(
        node["url"] + f"api/proposals/{proposal['sha']}/decision",
        data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "project-portal-node"},
    )
    try:
        with urllib.request.urlopen(request, timeout=DECISION_TIMEOUT_SEC) as answer:
            return 200 <= answer.status < 300
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        log.info("Could not tell %s about proposal #%s: %s", node.get("name"), proposal["id"], exc)
        return False


def journal_decision(proposal: dict, outcome: Outcome) -> None:
    meta = db.get_project_by_slug(config.META_PROJECT_SLUG)
    if meta is None:
        return
    who = proposal.get("decided_by") or "somebody"
    sender = proposal.get("node_name") or proposal.get("node_id") or "a node"
    if proposal["status"] == "approved":
        shas = ", ".join(f"`{s[:7]}`" for s in (proposal.get("applied") or []))
        text = (
            f"Proposal #{proposal['id']} from **{sender}** (*{proposal['title']}*) was **approved** by "
            f"{who} and applied to the source as {shas or 'no new commits'}. The service restarts onto "
            f"it once nothing is running, the mirror publishes, and {sender} is updated from there."
        )
    else:
        text = f"Proposal #{proposal['id']} from **{sender}** (*{proposal['title']}*) was **rejected** by {who}."
    if proposal.get("note"):
        text += f"\n\n> {proposal['note']}"
    db.add_journal(meta["id"], "system", "status", text)


# --- the prompt --------------------------------------------------------------

def prompt_section(project: db.sqlite3.Row) -> str:
    """What a run needs to know about proposals, and only when it does.

    On the publisher's own project: the pending series, where the bytes are,
    and how to decide - so the run this module queues arrives knowing its job.
    On a follower, every project: how to propose a change to the portal
    itself, because the source checkout there is read-only and an agent that
    does not know about `patches/` ends its run asking Wes to carry the files.
    Empty everywhere else, so an ordinary prompt is unchanged.
    """
    from app import nodes  # local

    if project["slug"] == config.META_PROJECT_SLUG and pulls_enabled():
        rows = pending()
        if not rows:
            return ""
        lines = [
            "## Proposed changes waiting for your review",
            "Another portal's agent cut these patch series against the public mirror and this "
            "portal pulled them in. Reviewing them is part of this run. For each one: read the "
            "diff, apply it on a throwaway branch or worktree of the source checkout (`git am -3 "
            "<mailbox>`), run the tests that own the files it touches (the whole suite if it "
            "changes logic), and look for anything that must not come in - a path or hostname "
            "from the other machine, a secret, an edit outside what its subjects claim. Then "
            "decide, with one or two sentences the agent on the other portal will read in its "
            "own journal:",
            "",
            f"    curl -s -H 'Accept: application/json' -X POST http://127.0.0.1:{config.PORT}/proposals/<id>/approve --data-urlencode 'note=...' --data-urlencode by=agent",
            f"    curl -s -H 'Accept: application/json' -X POST http://127.0.0.1:{config.PORT}/proposals/<id>/reject --data-urlencode 'note=...' --data-urlencode by=agent",
            "",
            "Approving applies the series to the source checkout with `git am -3`, so leave the "
            "tree clean before you post it; the portal restarts onto the result, publishes the "
            "mirror and updates the node that proposed it by itself. Do not `git am` the series "
            "onto master yourself - the route is what records the verdict and reports it back.",
            "",
        ]
        for row in rows:
            count = len(row["subjects"])
            lines.append(
                f"- **#{row['id']}** from {row['node_name'] or row['node_id']} (project "
                f"`{row['project_slug']}`), {count} commit{'' if count == 1 else 's'}, "
                f"{row['size'] // 1024} KB, mailbox `{mbox_path(row)}`, page `/proposals/{row['id']}`:"
            )
            for subject in row["subjects"]:
                lines.append(f"  - {subject}")
            if row.get("note"):
                lines.append(f"  - last attempt: {row['note']}")
        return "\n".join(lines)

    if pulls_enabled():
        return ""
    publisher = next((n for n in nodes.view() if n.get("publishes")), None)
    if publisher is None:
        return ""
    return (
        "## Proposing a change to the portal itself\n"
        f"This portal follows **{publisher['name']}** ({publisher['url']}), which publishes the code "
        f"both run. The checkout at `{config.APP_ROOT}` is fast-forwarded from the public mirror and "
        "must never be edited or committed to here. To change the portal, commit on a branch in a "
        "clone of that checkout, then cut the series into this workspace with "
        f"`git format-patch --stdout <base>..<branch> > {PATCH_DIR}/<name>.mbox`. The publishing "
        "portal pulls every series under that directory within a few minutes, an agent there "
        "reviews it, and the verdict is written to this project's journal. One file is one "
        "series and is judged once: a revised series is a new file name, never an edit of the "
        "old one. Do not ask anybody to carry the files by hand."
    )

