#!/usr/bin/env python3
"""Prove the cross-project round trip against the real CLI.

`tests/test_crossproject.py` runs against the portal's own functions, which is
where the decisions belong - but it cannot answer the question that decides
whether the feature exists at all: *does `claude -p --mcp-config` start the
relay, show the model three tools whose schemas it accepts, and hand back what
`crossproject` returned?* That is a property of the installed CLI.

So this stands the portal up for real - a throwaway database, two real
projects, a real workspace with a real file in it - serves the two endpoints
`app/mcpstdio.py` calls with the REAL `portalmcp.tools` and `portalmcp.call`,
and asks a live agent a question it can only answer by reading the other
project. Nothing here is a stand-in except the port.

    venv/bin/python scripts/crossproject_live.py

Costs one small `claude -p` run against the subscription.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RUN_ID = "4243"
TOKEN = "crossproject-live-token"

# Two facts the agent cannot know and cannot guess: one only on the other
# project's *todo list*, one only in a file in its *workspace*. Reporting both
# proves `project_context` and `project_files` each did their job.
#
# Deliberately NOT in the description: the first version of this check put it
# there, and the agent answered correctly off the one-line descriptions in the
# `projects` listing without ever calling `project_context`. The tool was fine;
# the check was not testing it.
THE_CONTEXT_FACT = "quartzine"
THE_FILE_FACT = "42-BRAVO-SEVEN"

calls: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: A003 - silence the access log
        pass

    def _query(self) -> dict:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        from app import portalmcp

        q = self._query()
        if not urlparse(self.path).path.endswith("/tools"):
            self._send({})
            return
        self._send({"tools": portalmcp.tools(int(q.get("run") or 0), q.get("token") or "") or []})

    def do_POST(self) -> None:
        from app import portalmcp

        q = self._query()
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        calls.append({"query": q, "payload": payload})
        result = asyncio.run(
            portalmcp.call(
                int(q.get("run") or 0),
                q.get("token") or "",
                str(payload.get("name") or ""),
                payload.get("arguments") or {},
            )
        )
        self._send(result)


def build_board(data_dir: Path) -> tuple[int, str]:
    """A real portal database with two related projects, one of which knows
    something the other does not. Returns (reader id, reader slug)."""
    from app import config, db, portalmcp

    config.DATA_DIR = data_dir
    config.PROJECTS_DIR = data_dir / "projects"
    config.DB_PATH = data_dir / "portal.db"
    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    db._CONN = None
    db.init_db()

    reader = db.create_project(
        "Widget Renderer", slug="widget-renderer", stage="active",
        description="Draws product shots of the widget.",
    )
    other = db.create_project(
        "Widget Shop", slug="widget-shop", stage="active",
        description="The storefront that sells the widget.",
    )
    db.add_todo(
        int(other["id"]),
        f"Restock the press with {THE_CONTEXT_FACT} sheet - it is the only "
        f"material the press takes",
        owner="agent",
    )
    db.add_journal(
        int(other["id"]), "agent", "progress",
        "## The press run\n\nThe press was recalibrated this week.",
    )
    ws = config.PROJECTS_DIR / "widget-shop"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "PLAN.md").write_text(
        f"# Widget Shop plan\n\nThe printer's calibration code is {THE_FILE_FACT}.\n"
    )
    (config.PROJECTS_DIR / "widget-renderer").mkdir(parents=True, exist_ok=True)

    portalmcp._SCOPES[int(RUN_ID)] = portalmcp._Scope(
        token=TOKEN, project_id=int(reader["id"]), run_id=int(RUN_ID)
    )
    return int(reader["id"]), str(reader["slug"])


def main() -> int:
    data_dir = Path(tempfile.mkdtemp(prefix="crossproject-live-"))
    try:
        reader_id, reader_slug = build_board(data_dir)
        from app import crossproject, portalmcp

        offered = [t["name"] for t in portalmcp.tools(int(RUN_ID), TOKEN) or []]
        print(f"tools the portal will serve: {offered}")
        print(f"related to {reader_slug}: "
              f"{[r['slug'] for r in crossproject.related(reader_id)]}\n")

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_port
        threading.Thread(target=server.serve_forever, daemon=True).start()

        config_json = {
            "mcpServers": {
                "portal": {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": [
                        str(ROOT / "app" / "mcpstdio.py"),
                        f"http://127.0.0.1:{port}",
                        RUN_ID,
                        TOKEN,
                    ],
                }
            }
        }
        prompt = (
            "You are an agent working on the project 'Widget Renderer'. Another "
            "project on this portal knows two things you need, and you have "
            "tools to read it.\n\n"
            "1. What material does the widget shop's press take? It is named "
            "in an item on that project's todo list.\n"
            "2. What is the printer's calibration code? It is written in a file "
            "called PLAN.md in that project's workspace.\n\n"
            "Use your mcp__portal__ tools to find out. Reply with ONLY the two "
            "answers, separated by a space. Do not guess."
        )
        cmd = [
            "claude", "-p", "--model", "haiku",
            "--dangerously-skip-permissions",
            "--mcp-config", json.dumps(config_json),
        ]
        started = time.time()
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            cwd=str(ROOT), timeout=300,
        )
        elapsed = time.time() - started
        out = (proc.stdout or "").strip()
        used = [c["payload"].get("name") for c in calls]

        checks = [
            ("the CLI started the relay and the run finished", proc.returncode == 0),
            ("the portal offered all three tools", offered[1:] ==
             ["projects", "project_context", "project_files"]),
            ("the model called at least one of them", bool(calls)),
            ("it read the other project's context", "project_context" in used),
            ("it read a file out of the other project's workspace",
             "project_files" in used),
            ("every call was authorized as this run",
             all(c["query"].get("run") == RUN_ID for c in calls)),
            (f"the todo-list-only fact reached the agent ({THE_CONTEXT_FACT})",
             THE_CONTEXT_FACT in out.lower()),
            (f"and the file-only fact did too ({THE_FILE_FACT})",
             THE_FILE_FACT in out.upper()),
        ]
        ok = True
        for label, passed in checks:
            print(f"{'ok  ' if passed else 'FAIL'} {label}")
            ok = ok and bool(passed)
        print(f"\ntools called: {used}")
        print(f"run took {elapsed:.1f}s, agent said: {out[:300]!r}")
        if proc.returncode != 0:
            print(f"stderr: {(proc.stderr or '')[-1500:]}", file=sys.stderr)
        return 0 if ok else 1
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
