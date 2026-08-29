# Setting this up on a new machine

You are probably an agent reading this because somebody cloned the repo and
asked you to make it run. This file is the whole job. It assumes nothing about
the machine and asks you to decide nothing that belongs to a person.

## The short version

```bash
python3 deploy/setup.py
```

That is idempotent, safe to re-run, and ends by booting the app on a scratch
port and asking it `/api/ping` — so if it prints `ok  smoke test`, a real HTTP
request got a real answer out of this checkout. It exits 1 if a step failed and
0 otherwise, and it finishes by listing the things that need hands rather than
prompting for them.

`python3 deploy/setup.py --check` reports without changing anything, including
without creating the database.

Then start it:

```bash
venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8500
```

or install `deploy/project-portal.service` as a systemd **user** unit, which
works unedited if the checkout is at `~/project-portal`:

```bash
cp deploy/project-portal.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now project-portal
```

## What you cannot finish, and must hand back

Two things, and `deploy/setup.py` prints both under `human`:

1. **Logging the Claude Code CLI in.** Runs are spawned as `claude -p`. The
   subscription login is a browser flow, so an unattended setup cannot do it.
   The portal serves, holds ideas, and answers questions without it; what it
   cannot do is start a run. The alternative is an Anthropic API key, which is
   billed per token — set `auth_mode = "api_key"` in `portal.toml` and put the
   key in `secrets/anthropic_key.txt`. **Never turn that on for somebody
   without asking**: it moves them from a flat subscription onto a meter.
2. **Confirming the hostname is reachable.** With no config the portal calls
   itself by the system hostname. Every URL it prints — in the UI, in
   notifications, in agent prompts — is read on a *different device*, so a name
   that only resolves on the server makes all of them dead links. If the system
   hostname is not what a phone would type, set `host` in `portal.toml`.

## Configuration

Everything is optional. Copy `portal.example.toml` to `portal.toml` beside it
(or into `data/`) and edit; it documents every key. Anything there can also be
set as `PORTAL_<KEY>` in the environment, which wins over the file.

All state is under `data/`, which is gitignored: the SQLite database, the
per-project agent workspaces, memory files and uploads. Back that directory up
and you have backed up the portal. **`data/portal.db` is in WAL mode, so
copying the `.db` alone is not a backup** — the recent writes are in the
`-wal` sidecar. Use `VACUUM INTO`.

Credentials live in `secrets/`, also gitignored.

## Before you commit anything to a public remote

This repo has a standing guard against republishing somebody's infrastructure,
and you should run it rather than eyeball a diff:

```bash
venv/bin/python -c "from app import leakscan; [print(x) for x in leakscan.scan()]"
```

It reports two kinds of finding and both are hard stops for `deploy/publish.py`:

- **Identity** — the machine's hostname, its IP addresses (read from the kernel,
  not from a list), the owner's home directory, plus anything in the
  `leak_patterns` list in the gitignored `portal.toml`. It is derived from the
  installation on purpose, so it protects whoever installs it next as
  automatically as it protected the author.
- **Credentials** — anything shaped like a live key (Anthropic, OpenAI, GitHub,
  Slack, AWS, Google, Telegram, Tailscale, a PEM private key block, a JWT, or a
  named secret assigned a random-looking literal), reported **redacted**, by
  line number only.

`app/leakscan.py` explains the reasoning; `tests/test_leakscan.py` is where a
new pattern goes with the case that proves it fires.

## Running the tests

```bash
venv/bin/python -m pytest -q          # the whole suite, ~40s
venv/bin/python -m pytest tests/test_leakscan.py -q
venv/bin/python -m pytest -q -n0      # same suite, serial, ~3min
```

`pytest.ini` runs the suite across 8 processes, so the whole thing is about
40 seconds rather than three minutes. Two things follow. Output interleaves and
the run order is no longer the file order, so pass `-n0` when a failure has to
be read in sequence. And a test that passes serially but fails in parallel is
usually not a flake - it is module-global state leaking between files, which is
a real defect wherever it turns up.

### Module globals between tests

Every mutable module global in `app/` is listed in `tests/module_state.py`,
either as one an autouse fixture restores around each test or as one exempted
with its reason. `tests/test_module_state.py` fails if `app/` grows a global
that is in neither list, so adding one is a decision rather than an ambush.

That matters more here than in most suites because ids restart at 1 in every
test: a leftover entry in a run-id-keyed dict is read by the next test as being
about *its own* run 1, not as stale data.

The invariant is "after any test, every listed global equals its import value",
and this checks it against the real values as the suite runs:

```bash
venv/bin/python -m pytest tests/ -n0 -q -p deploy.find_module_state_leaks
```

Run it serially - under xdist each worker prints its own half of the report.
It is a diagnostic and prints a summary; it never fails a test.

The JavaScript tests under `tests/js/` need [Bun](https://bun.sh) and are run
with `bun test`. They are not part of the pytest suite.

`docs/verifying-with-mutations.md` describes how a change is expected to prove
itself here: delete the fix, watch the test fail, put it back. That is the
house standard for anything with a branch in it.

## The shape of the thing

- `app/main.py` — every route. FastAPI, server-rendered Jinja, no build step.
- `app/worker.py` — the scheduler: what runs, when, and why not.
- `app/agent_runner.py` — spawning `claude -p` and reading its stream back.
- `app/db.py` — the schema and every query. SQLite, migrations applied at boot.
- `app/static/app.js`, `app/static/style.css` — one script, one stylesheet.
- `docs/state-model.md` — stages, badges and the rules between them. Read this
  before changing anything about how a project moves between states.
</content>
