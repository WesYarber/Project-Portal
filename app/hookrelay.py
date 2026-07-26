"""Claude Code hook relay: POST the hook payload to the portal, speak the
CLI's hook protocol back.

Installed as a *command* hook via `--settings` on every guarded run (the CLI
build in use ignores URL-type hooks, so this script is the HTTP transport).
It reads the CLI's JSON payload from stdin, posts it to the portal's hook
endpoint (the URL, with run id and token, is argv[1] - /hooks/pre-tool for
the write guardrail, /hooks/stop for the report nudge), and prints a hook
response only when the portal says to.

Deliberately stdlib-only with no app imports: it runs under whatever cwd the
CLI gives hooks, and it must keep working even when the portal codebase around
it is mid-upgrade. Fail-open is the contract: any error - portal down, timeout,
junk response - exits 0 with no output, which the CLI reads as "allow". The
guardrail exists to stop a hostile tool call, never to strand a healthy run.
"""
from __future__ import annotations

import json
import sys
import urllib.request


def main() -> int:
    try:
        url = sys.argv[1]
        raw = sys.stdin.read()
        req = urllib.request.Request(
            url,
            data=raw.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            answer = json.loads(resp.read().decode("utf-8"))
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
