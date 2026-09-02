"""Claude Code hook relay: POST the hook payload to the portal, speak the
CLI's hook protocol back.

Installed as a *command* hook via `--settings` on every guarded run (the CLI
build in use ignores URL-type hooks, so this script is the HTTP transport).
It reads the CLI's JSON payload from stdin, posts it to the portal's hook
endpoint (the URL, with run id and token, is argv[1] - /hooks/pre-tool for
the write guardrail, /hooks/stop for the report nudge, /hooks/post-tool for
the audit trail and the mid-run channel), and prints a hook response only
when the portal says to.

One answer is not final: `{"poll": url}` means "hold here and ask again at
that address" - the portal's way of pausing a run between two turns without
spending anything (app/midrun.py). The relay sleeps and re-posts until an
answer arrives without `poll`, then treats that answer like any other. The
CLI's own hook timeout bounds the wait.

Deliberately stdlib-only with no app imports: it runs under whatever cwd the
CLI gives hooks, and it must keep working even when the portal codebase around
it is mid-upgrade. Fail-open is the contract: any error - portal down, timeout,
junk response - exits 0 with no output, which the CLI reads as "allow". The
guardrail exists to stop a hostile tool call, never to strand a healthy run.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

# Fallback between polls when the portal's answer names no interval.
DEFAULT_POLL_SEC = 3


def _post(url: str, raw: str):
    req = urllib.request.Request(
        url,
        data=raw.encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    try:
        url = sys.argv[1]
        raw = sys.stdin.read()
        answer = _post(url, raw)
        # A hold: keep asking at the address given until the answer changes.
        # Any failure on the way - the portal restarting under a paused run -
        # falls out to the except below and releases the run, fail-open.
        while isinstance(answer, dict) and isinstance(answer.get("poll"), str):
            interval = answer.get("interval")
            time.sleep(interval if isinstance(interval, (int, float)) and interval > 0 else DEFAULT_POLL_SEC)
            answer = _post(answer["poll"], raw)
        if not isinstance(answer, dict):
            return 0
        # Generic protocol: when the portal hands back a ready-made hook
        # response (the Stop endpoint does), print it verbatim - the portal
        # owns the shape, the relay stays a dumb pipe.
        out = answer.get("hook_output")
        if isinstance(out, dict) and out:
            print(json.dumps(out))
            return 0
        if answer.get("decision") == "deny":
            reason = str(answer.get("reason") or "Denied by the portal guardrail")
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": reason,
                        }
                    }
                )
            )
    except Exception:  # noqa: BLE001 - fail open, always
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
