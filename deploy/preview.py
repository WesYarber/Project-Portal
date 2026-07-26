#!/usr/bin/env python
"""Serve the WORKING TREE against a throwaway copy of the live database.

Every UI change wants looking at before it is committed, but the service on
8500 runs the *committed* code, so rendering that proves nothing about the
diff in the working tree. This boots the tree as it stands, on a spare port,
against a copy of portal.db.

Three things are switched off before the app is imported, and each one is here
because leaving it on would touch the real world:

  * the worker loop - otherwise this second process races the live one for the
    same projects and starts real `claude -p` runs;
  * the Telegram token in the copy - a preview must not message Wes;
  * the background pollers - they would hit the Claude usage API and shell out
    to `tailscale` for a page nobody is reading.

app/config.py has no environment override for DATA_DIR (it is computed from
the source tree), so the paths are patched in-process here, before app.main is
imported and reads them.

Usage: venv/bin/python deploy/preview.py [port]   # then Ctrl-C; it cleans up.
"""
from __future__ import annotations

import atexit
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8599

live_db = config.DB_PATH
tmp = Path(tempfile.mkdtemp(prefix="portal-preview-"))
atexit.register(shutil.rmtree, tmp, True)

# Copy the database (and its WAL sidecars, or we read a stale snapshot).
db = tmp / "portal.db"
shutil.copy2(live_db, db)
for suffix in ("-wal", "-shm"):
    side = live_db.with_name(live_db.name + suffix)
    if side.exists():
        shutil.copy2(side, db.with_name(db.name + suffix))

config.DATA_DIR = tmp
config.DB_PATH = db
config.RUNS_DIR = config.DATA_DIR / "runs"
# Projects/memory stay pointed at the real ones: they are read-only for a
# render, and copying 17 workspaces to look at a page would be absurd.

# memory.history_dir() is computed from DATA_DIR at call time, so without a
# copy the preview's /memory renders an empty revisions list whatever the live
# portal holds - which is exactly the kind of silent wrongness a preview
# exists to avoid. Copied rather than pointed at the real one: anything a
# preview page writes (a snapshot taken before a save, say) lands in the
# throwaway dir, not in the live history.
live_history = live_db.parent / "memory-history"
if live_history.is_dir():
    shutil.copytree(live_history, tmp / "memory-history")

# Same reasoning for everything else /memory reads from DATA_DIR: the
# retired-learnings archive, the freshness sidecar and the promoted skills all
# resolve from DATA_DIR at call time, so without copies the preview renders
# those sections empty whatever the live portal holds.
for name in ("learnings-archive.md", "learnings-meta.json"):
    src = live_db.parent / name
    if src.is_file():
        shutil.copy2(src, tmp / name)
live_skills = live_db.parent / "skills"
if live_skills.is_dir():
    shutil.copytree(live_skills, tmp / "skills")

from app import db as dbmod  # noqa: E402  (must follow the config patch)

dbmod.DB_PATH = db

from app import main  # noqa: E402

# Silence everything that would reach outside this process.
main.app.router.on_startup.clear()

# ...but the startup handler is also where init_db() runs, and init_db() is
# what adds columns the working tree has added since the live database was
# last migrated. Without this, previewing a schema change serves the copy
# *un-migrated*: the page renders, and the first write 500s on a column that
# does not exist. Run it here rather than leaving the startup list alone,
# because the rest of that handler is exactly what must not fire.
dbmod.init_db()

# get_conn() hands back a shared connection the whole app reuses - closing it
# here leaves every later request raising "Cannot operate on a closed database".
conn = dbmod.get_conn()
conn.execute("DELETE FROM settings WHERE key = 'telegram_bot_token'")
conn.commit()

print(f"preview of the working tree on :{port}, db copy at {db}", flush=True)

import uvicorn  # noqa: E402

uvicorn.run(main.app, host="0.0.0.0", port=port, log_level="warning")
