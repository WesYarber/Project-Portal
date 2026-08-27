# Looking at the portal's own UI without restarting the live one

Every other project can be screenshotted by pointing `deploy/screenshot.sh` at
it. The portal cannot: the thing you want to look at is the process you are
running inside. Restarting the live service to load your edit kills your own
agent run mid-work, and waiting for the automatic post-run restart means you
report a UI change you have never seen.

The answer is a **throwaway instance under `/tmp`, with its own data directory
and its own port**. It is genuinely useful — it is how the memory-lease and the
person-picker screenshots were taken. It is also how production got broken once,
on 2026-07-29, so read the caveat before you reach for it.

## The recipe

`$SRC` below is the portal's own checkout (`~/project-portal` on the machine
this was written on) and `$VENV` is `$SRC/venv/bin/python`.

```bash
# 1. A copy of the source with NO data directory. config.DATA_DIR is
#    BASE_DIR/data with no environment override, so a separate source tree is
#    what gives you a separate database - there is no PORTAL_DATA env var.
rm -rf /tmp/portal-shot && mkdir -p /tmp/portal-shot
cd "$SRC" && cp -r app deploy portal.toml /tmp/portal-shot/
rm -rf /tmp/portal-shot/data

# 2. Seed it: worker off, plus whatever state the page you want needs.
cd /tmp/portal-shot && "$VENV" - <<'PY'
import sys; sys.path.insert(0, "/tmp/portal-shot")
from app import config, db, people
assert str(config.DATA_DIR).startswith("/tmp/"), "refusing to touch the live data dir"
db.init_db()
db.set_setting("worker_enabled", "0")
# ... create_project / create_question / people.add as the shot requires
PY

# 3. Run it on a port nothing else uses - check `ss -tln` first.
cd /tmp/portal-shot
nohup "$VENV" -m uvicorn app.main:app \
  --host 0.0.0.0 --port 8791 > /tmp/portal-shot.log 2>&1 &

# 4. Shoot it. PORTAL is the address the RENDER box reaches this machine on,
#    which is not always the address everybody else uses - `render_portal_url`
#    in portal.toml is that route, with the port swapped for the throwaway's.
cd "$SRC"
PORTAL=http://<render route>:8791 deploy/screenshot.sh /questions 900 /tmp/shot.png

# 5. Tear it down, every time.
pkill -f 'port 8791'   # NB: from a script, not from an interactive shell whose
                       # own command line contains the pattern - it will match
                       # itself and kill the shell you are typing into (exit
                       # 144, and the teardown looks like it did nothing).
                       # `pgrep -f` waits self-match the same way; see
                       # docs/verifying-with-mutations.md §5.
rm -rf /tmp/portal-shot
```

`--host 0.0.0.0` is not optional: the render box is a different machine and
cannot reach a loopback bind.

Also note `deploy/screenshot.sh`'s argument order, which is not the obvious one:
`screenshot.sh <path> <height> <out> <width>`, with width defaulting to 1280.
The side rail needs 1100px to appear at all, so passing a big number in the
second slot renders a very tall 1280px-wide page with no rail in it and the
change you meant to look at nowhere on screen.

### It will try to take the live preview port

The throwaway starts the whole app, and that includes the preview server on
`preview_port` (8501, `app/site.py`) — a fixed port, not derived from the
`--port` you passed. While the real portal is up that bind fails with
`[Errno 98] address already in use`, the throwaway carries on without it, and
the line in the log is harmless. **If the real portal is down, the throwaway
takes 8501 instead**, and the teardown below only kills the uvicorn on your own
port. Check `ss -tln | grep 8501` after tearing down.

## The caveat, which is the reason this file exists

**A second portal process sweeps the first one's systemd scopes.**

`worker._sweep_strays` runs on every tick and builds its "protected" set from
its **own** `runs` table. A throwaway instance has an empty one, so on
2026-07-29 it looked at the real systemd, concluded that every genuine
`portal-run-*.scope` on the machine was unprotected, and rehoused the live
agent run — the very run that had started it — out of its own cgroup into
`portal-stray-681-2437616-1.scope`. That matters more than a renamed unit:
`runs.scope_unit` is the only handle a later portal process has on an adopted
agent, so the next restart asks systemd, hears "gone", and declares a live run
dead — which unlocks an occupied workspace.

Two things follow, and the second is the one people get wrong:

- **`worker_enabled=0` does NOT stop the sweep.** The gate on `worker_enabled`
  sits *after* `await _sweep_strays()` in `_tick`, because housekeeping is
  supposed to keep happening on a paused portal. Setting it off stops runs
  being *started*; it does nothing about scopes.
- **What actually protects you is `strays._minted_by_a_live_stranger`.** The
  scope name carries the pid of the process that minted it, so a scope minted
  by a live process that is not us is left alone, with no shared state at all.
  That fence shipped on 2026-07-29 and is what makes this recipe safe.

So: **the fence is load-bearing, not belt-and-braces.** If you are ever editing
`app/strays.py`, this recipe is one of the things you are editing.

## Check afterward, every time

Cheap, and it catches the failure while it is still repairable:

```bash
grep -ci rehous /tmp/portal-shot.log            # expect 0
cat /proc/self/cgroup                           # still portal-run-<your id>-*
systemctl --user list-units 'portal-*' --no-legend | awk '{print $1, $3, $4}'
```

Use `awk`, not `cut -d' ' -f1`: `list-units` indents its rows, so cutting on the
first space yields a column of empty strings and every unit looks gone.

Cross-check that list against
`SELECT id, scope_unit FROM runs WHERE status='running'` in the **live**
database. If a row names a unit that no longer exists, repair the row by hand —
a rehoused unit keeps the same suffix, so `portal-run-681-2437616-1.scope`
became `portal-stray-681-2437616-1.scope` and the fix was a one-line `UPDATE`.

## When not to bother

If the page you want does not depend on your edit, shoot the **live** portal on
its normal port and save yourself the whole exercise. The throwaway is only for
looking at code that is committed but not yet loaded.
