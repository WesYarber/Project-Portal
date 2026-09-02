"""Stdio MCP server the CLI runs, relaying every call to the portal over HTTP.

The sibling of `app/hookrelay.py`, and stdlib-only for the same reasons: it is
started by the CLI under whatever cwd it likes, it must keep working while the
portal codebase around it is being rewritten by the very run it is serving, and
the decision it relays belongs in one testable place with the database in reach
(`app/portalmcp.py`), not out here.

Two rules this file exists to keep:

1. **Nothing but JSON-RPC ever reaches stdout.** A stray print - a traceback, a
   warning, a debug line - is not noise on this pipe, it is a protocol violation
   that makes the CLI drop the server. Every diagnostic goes to stderr, and the
   one place that could raise on the way to stdout (`json.dumps` of a value from
   the portal) is inside the try.

2. **`initialize` answers from nothing.** The CLI blocks its own startup on the
   handshake, so touching the network there would put the portal's health on the
   critical path of every run's first second. The first HTTP call happens at
   `tools/list`, and if it fails the answer is an empty tool list: the run then
   proceeds exactly as runs did before this existed, which is the correct
   degradation.

A notification (a message with no `id`) gets no reply at all - answering one is
the other way to get dropped.
"""
from __future__ import annotations

import http.client
import json
import sys
import time
import urllib.error
import urllib.request

# The version the portal implements. When a client asks for something else we
# echo its choice back: this server uses only the parts of MCP that have not
# changed across revisions (initialize, tools/list, tools/call), so refusing a
# client over a date string would break a CLI upgrade for no gain.
PROTOCOL_VERSION = "2025-06-18"

# Longer than `portalmcp.MAX_WAIT`, because a call to `ask` deliberately blocks
# server-side while it waits for a person to answer. Timing out here would kill
# the wait from the wrong end and lose an answer that was about to arrive.
CALL_TIMEOUT = 300
LIST_TIMEOUT = 15

# How long a portal that is not answering is given before a call is reported
# as failed - the same figure as `app/hookrelay.py`'s `POST_RETRY_SEC`, and for
# the same reason: the portal restarts itself to load an update (about four
# seconds off the air, ten at the outside while it waits for a request in
# flight), and a call that lands in that window used to be reported as "the
# portal could not be reached, so nothing was filed". For an `ask` cut off
# mid-wait that was false - the question was filed the moment the first post
# arrived - and it lost the answer the person was about to give. The budget is
# counted from the *failure*, not from the start of the call, because an ask
# legitimately holds its connection open for minutes before a restart cuts it.
POST_RETRY_SEC = 10.0
RETRY_INTERVAL_SEC = 0.5
# `ask`'s own default wait, so a retried ask can be told how much of its wait
# is left. `app/portalmcp.py` owns the figure; a test pins the two together.
DEFAULT_WAIT = 120

# Seams for the tests' fake clock.
_clock = time.monotonic
_sleep = time.sleep

# What a transport failure looks like from urllib: a refused or reset
# connection, nothing listening, a socket timeout (all OSError, which
# URLError is), and a connection the server closed mid-response
# (RemoteDisconnected is both, IncompleteRead is only an HTTPException).
TRANSPORT_ERRORS = (OSError, http.client.HTTPException)


def _patiently(attempt, budget_sec: float):
    """Call `attempt()` again through a portal that is not answering, until
    `budget_sec` has passed since the first transport failure. An HTTP error
    is the portal answering and is raised at once. Each retry is told it is
    one, so the portal can tell a repeated ask from a new one."""
    deadline = None
    retry = False
    while True:
        try:
            return attempt(retry)
        except urllib.error.HTTPError:
            raise
        except TRANSPORT_ERRORS:
            now = _clock()
            if deadline is None:
                deadline = now + budget_sec
            elif now >= deadline:
                raise
            retry = True
            _sleep(RETRY_INTERVAL_SEC)


def _post(url: str, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(url: str, timeout: int) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Relay:
    def __init__(self, base: str, run_id: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.run_id = run_id
        self.token = token

    def _url(self, path: str) -> str:
        return f"{self.base}/mcp/{path}?run={self.run_id}&token={self.token}"

    def tools(self) -> list:
        try:
            answer = _patiently(
                lambda retry: _get(self._url("tools"), LIST_TIMEOUT), POST_RETRY_SEC
            )
        except Exception as exc:  # noqa: BLE001 - no tools beats a broken server
            print(f"portal mcp: could not list tools: {exc}", file=sys.stderr)
            return []
        tools = answer.get("tools") if isinstance(answer, dict) else None
        return tools if isinstance(tools, list) else []

    def call(self, name: str, arguments) -> dict:
        started = _clock()

        def attempt(retry: bool) -> dict:
            return _post(
                self._url("call"),
                {"name": name, "arguments": self._arguments(name, arguments, retry, started)},
                CALL_TIMEOUT,
            )

        try:
            answer = _patiently(attempt, POST_RETRY_SEC)
        except Exception as exc:  # noqa: BLE001
            print(f"portal mcp: call failed: {exc}", file=sys.stderr)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "The portal could not be reached, so nothing was filed. "
                                "Put it in your report instead.",
                    }
                ],
                "isError": True,
            }
        if not isinstance(answer, dict) or "content" not in answer:
            return {
                "content": [{"type": "text", "text": "The portal answered with nothing usable."}],
                "isError": True,
            }
        return answer

    @staticmethod
    def _arguments(name: str, arguments, retry: bool, started: float):
        """The arguments as posted. A retry carries `retry: true`, and a
        retried `ask` carries the wait it has left rather than the wait it
        started with: the person's time to answer was promised once, and the
        CLI's own tool timeout is counted from the first attempt."""
        if not retry or not isinstance(arguments, dict):
            return arguments
        out = dict(arguments)
        out["retry"] = True
        if name == "ask":
            try:
                wait = int(out.get("wait_seconds", DEFAULT_WAIT))
            except (TypeError, ValueError):
                wait = DEFAULT_WAIT
            out["wait_seconds"] = max(0, wait - int(_clock() - started))
        return out


def handle(relay: Relay, message: dict) -> dict | None:
    """One request in, one response out - or None for a notification."""
    method = message.get("method")
    msg_id = message.get("id")
    if msg_id is None:
        return None  # a notification: acknowledged by saying nothing

    if method == "initialize":
        params = message.get("params") or {}
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        return _ok(msg_id, {
            "protocolVersion": asked if isinstance(asked, str) and asked else PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "portal", "version": "1.0.0"},
        })
    if method == "ping":
        return _ok(msg_id, {})
    if method == "tools/list":
        return _ok(msg_id, {"tools": relay.tools()})
    if method == "tools/call":
        params = message.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        return _ok(msg_id, relay.call(str(params.get("name") or ""), params.get("arguments") or {}))
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _ok(msg_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def serve(relay: Relay, stdin, stdout) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            # Not addressed to anything we can answer - a parse error response
            # needs an id we do not have, so the only correct move is silence.
            print("portal mcp: unparseable line", file=sys.stderr)
            continue
        if not isinstance(message, dict):
            continue
        try:
            response = handle(relay, message)
        except Exception as exc:  # noqa: BLE001
            print(f"portal mcp: {exc}", file=sys.stderr)
            msg_id = message.get("id")
            if msg_id is None:
                continue
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32603, "message": "Internal error"},
            }
        if response is None:
            continue
        stdout.write(json.dumps(response) + "\n")
        stdout.flush()


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: mcpstdio.py <base-url> <run-id> <token>", file=sys.stderr)
        return 2
    serve(Relay(sys.argv[1], sys.argv[2], sys.argv[3]), sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
