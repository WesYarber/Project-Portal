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

A portal that stops answering is not given up on at once. The service restarts
itself to load its own updates - about four seconds off the air, a 3 s warning
and under a second of startup - and every run of the meta-project restarts it
under itself, so a tool call landing in that window would otherwise pass the
write guard unscreened, and a held run would wake. A fresh post keeps trying
for `POST_RETRY_SEC`; a hold keeps trying for `HOLD_RETRY_SEC`, since a pause
is at stake and the hold itself is on the run's row for the process that comes
back to answer "keep holding". Only transport failures are retried: an answer
the portal gave, even a bad one, is final.

Deliberately stdlib-only with no app imports: it runs under whatever cwd the
CLI gives hooks, and it must keep working even when the portal codebase around
it is mid-upgrade. Fail-open is the contract: any error - portal down past the
retry budget, junk response - exits 0 with no output, which the CLI reads as
"allow". The guardrail exists to stop a hostile tool call, never to strand a
healthy run.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

# Fallback between polls when the portal's answer names no interval.
DEFAULT_POLL_SEC = 3

# How long a fresh post keeps trying to reach a portal that is not answering
# before failing open. Long enough to ride out a service restart with room to
# spare, and inside the 15 s the CLI allows the PreToolUse and Stop hooks, so
# the relay decides the outcome rather than the CLI's timeout.
POST_RETRY_SEC = 10

# The same for a relay already holding a paused run. A pause is at stake here,
# so a portal that has gone quiet is waited on far longer before the run is
# let go: a minute and a half of silence is an outage, not a restart.
HOLD_RETRY_SEC = 90

# Between attempts while the portal is not answering.
RETRY_INTERVAL_SEC = 1


def _post(url: str, raw: str):
    req = urllib.request.Request(
        url,
        data=raw.encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _clock() -> float:
    return time.monotonic()


def _post_patiently(url: str, raw: str, budget_sec: float):
    """`_post`, retried through a portal that is not answering - connection
    refused or reset, nothing listening, a socket timeout - until `budget_sec`
    has passed since the first attempt. An HTTP error is the portal answering,
    so it is raised at once; so is anything that is not a transport failure."""
    deadline = _clock() + budget_sec
    while True:
        try:
            return _post(url, raw)
        except urllib.error.HTTPError:
            raise
        except OSError:
            if _clock() >= deadline:
                raise
            time.sleep(RETRY_INTERVAL_SEC)


def main() -> int:
    try:
        url = sys.argv[1]
        raw = sys.stdin.read()
        answer = _post_patiently(url, raw, POST_RETRY_SEC)
        # A hold: keep asking at the address given until the answer changes.
        # A portal that stops answering mid-hold - the service restarting
        # under a paused run - is retried for HOLD_RETRY_SEC; past that, the
        # failure falls out to the except below and releases the run, fail-open.
        while isinstance(answer, dict) and isinstance(answer.get("poll"), str):
            interval = answer.get("interval")
            time.sleep(interval if isinstance(interval, (int, float)) and interval > 0 else DEFAULT_POLL_SEC)
            answer = _post_patiently(answer["poll"], raw, HOLD_RETRY_SEC)
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
