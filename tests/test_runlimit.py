"""Per-run memory containment (app/runlimit.py).

The incident these pin, from 2026-07-26: a tool inside an agent run allocated
~17 GB on an 18 GB machine seven times between 13:40 and 17:02. Each time the
kernel's global OOM killer fired, systemd's default `OOMPolicy=stop` took the
whole portal service down because the greedy process was a descendant of it,
and every run in flight was reconciled as "orphaned" on restart. Wes saw a
project that "keeps orphaning runs"; the word memory appeared nowhere.

Five were SimpleClickTrack's `bun test`; two were this project's own runs, as
`python3` at 17 GB. See `app/runlimit.py` for why that distinction matters.

Three properties are load-bearing:

1. **A cap is on by default, derived from the machine.** A shipped constant
   would be either no protection or a straitjacket on somebody else's box.
2. **Failing open is not optional.** No systemd, no cgroup v2, a refusing
   `systemd-run` - every one of those must mean runs spawn exactly as before.
   A hardening feature that can stop runs starting is worse than the bug.
3. **The kill is reported.** The cap kills only the greedy process, so a run
   can hit it and still succeed - and a silently swallowed OOM is precisely the
   failure mode that let this run for three hours unnoticed.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

import module_state

from app import agent_runner, config, db, runlimit

# Asked once, for the `skipif` conditions below. Wrapped because
# `runlimit.available()` memoizes into a process-wide global, and this runs at
# import - before any fixture exists to clear it, so the first test of the run
# would inherit the memo. See tests/module_state.py.
_HAVE_SCOPES = module_state.probe_without_memoizing(
    "app.runlimit", "_available", lambda: runlimit.available(refresh=True))


@pytest.fixture(autouse=True)
def _fresh_probe(monkeypatch):
    """`available()` caches its probe process-wide, and scope names are
    remembered per run id; no test may inherit another's."""
    monkeypatch.setattr(runlimit, "_available", None, raising=False)
    monkeypatch.setattr(runlimit, "_scopes", {}, raising=False)


# --- sizes -----------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("6G", 6 * 1024**3),
        ("6g", 6 * 1024**3),
        ("6GiB", 6 * 1024**3),
        ("6 GB", 6 * 1024**3),
        ("512M", 512 * 1024**2),
        ("1.5G", int(1.5 * 1024**3)),
        ("2048", 2048),
        (" 4G ", 4 * 1024**3),
        ("0", 0),
    ],
)
def test_parse_size_accepts_the_shapes_a_person_types(text, expected):
    assert runlimit.parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "lots", "G", "6 gigs", "-4G", "6/G", None])
def test_parse_size_rejects_everything_else(text):
    assert runlimit.parse_size(text) is None or runlimit.parse_size(text) < 0


def test_human_reads_as_a_person_would_say_it():
    assert runlimit.human(6 * 1024**3) == "6.0 GiB"
    assert runlimit.human(512 * 1024**2) == "512.0 MiB"
    assert runlimit.human(None) == "unlimited"
    assert runlimit.human(0) == "unlimited"


def test_total_memory_is_read_from_the_machine():
    total = runlimit.total_memory_bytes()
    assert total is not None and total > 256 * 1024**2


def test_total_memory_degrades_rather_than_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(runlimit, "MEMINFO", tmp_path / "nope")
    assert runlimit.total_memory_bytes() is None
    assert runlimit.default_max_bytes() is None


def test_default_is_a_fraction_of_this_machine(monkeypatch):
    monkeypatch.setattr(runlimit, "total_memory_bytes", lambda: 32 * 1024**3)
    assert runlimit.default_max_bytes() == int(32 * 1024**3 * runlimit.DEFAULT_FRACTION)


def test_a_small_machine_still_gets_a_usable_floor(monkeypatch):
    """A cap that fires on ordinary work reads as the portal being broken, so
    the fraction has a floor under it - on a 2 GB box, 40% would be 800 MB and
    would kill a routine build."""
    monkeypatch.setattr(runlimit, "total_memory_bytes", lambda: 2 * 1024**3)
    assert runlimit.default_max_bytes() == runlimit.MIN_BYTES


# --- the setting -----------------------------------------------------------


def test_blank_setting_means_the_derived_default():
    db.set_setting("run_memory_max", "")
    assert runlimit.configured_max_bytes() == runlimit.default_max_bytes()


def test_the_shipped_default_is_blank_not_a_number():
    """Because the right number is a property of the machine, not of the app."""
    assert config.DEFAULT_SETTINGS["run_memory_max"] == ""


def test_an_explicit_size_wins():
    db.set_setting("run_memory_max", "3G")
    assert runlimit.configured_max_bytes() == 3 * 1024**3


@pytest.mark.parametrize("off", ["0", "off", "none", "no", "unlimited", "OFF"])
def test_the_cap_can_be_turned_off_outright(off):
    """The escape hatch has to exist: a cap is the kind of thing that
    eventually stops legitimate work on a machine nobody foresaw."""
    db.set_setting("run_memory_max", off)
    assert runlimit.configured_max_bytes() is None


@pytest.mark.parametrize("bad", ["lots", "-2G", "gigabytes"])
def test_an_unparseable_setting_falls_back_to_the_default(bad):
    """Not to "no cap", and not to an exception - a typo must not silently
    remove the protection, nor stop the portal."""
    db.set_setting("run_memory_max", bad)
    assert runlimit.configured_max_bytes() == runlimit.default_max_bytes()


# --- wrapping the spawn ----------------------------------------------------


def test_wrap_puts_the_command_in_a_capped_scope(monkeypatch):
    monkeypatch.setattr(runlimit, "available", lambda: True)
    db.set_setting("run_memory_max", "4G")
    argv = runlimit.wrap(["claude", "-p", "hi"], run_id=7)

    assert argv[0] == "systemd-run"
    assert "--scope" in argv  # not --service: pipes and process group survive
    assert f"MemoryMax={4 * 1024**3}" in argv
    assert "MemorySwapMax=0" in argv
    # Only the greedy process dies, not the whole claude session.
    assert "OOMPolicy=continue" in argv
    # The original command survives intact, after the -- separator.
    assert argv[argv.index("--") + 1:] == ["claude", "-p", "hi"]


def test_wrap_is_a_no_op_when_systemd_run_is_unavailable(monkeypatch):
    monkeypatch.setattr(runlimit, "available", lambda: False)
    db.set_setting("run_memory_max", "4G")
    assert runlimit.wrap(["claude", "-p", "hi"], 7) == ["claude", "-p", "hi"]


def test_wrap_is_a_no_op_when_the_cap_is_turned_off(monkeypatch):
    monkeypatch.setattr(runlimit, "available", lambda: True)
    db.set_setting("run_memory_max", "off")
    assert runlimit.wrap(["claude", "-p", "hi"], 7) == ["claude", "-p", "hi"]


def test_scope_names_are_unique_per_run_and_per_portal_process():
    assert runlimit.scope_name(7) != runlimit.scope_name(8)
    assert str(os.getpid()) in runlimit.scope_name(7)
    assert runlimit.scope_name(None).endswith(".scope")
    # Stable for the life of one run, so the watcher can recognize its cgroup.
    assert runlimit.scope_name(7) == runlimit.scope_name(7)


def test_two_unidentified_runs_do_not_collide_on_one_unit_name():
    """`run_claude` is also called with run_id=None. systemd refuses a unit
    name that already exists, and the way that presented was the second such
    run producing no output whatsoever - not an error anyone could read."""
    first = runlimit.scope_name(None)
    runlimit.forget_scope(None)
    assert runlimit.scope_name(None) != first


def test_available_is_false_rather_than_raising_when_systemd_run_is_missing(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("systemd-run")

    monkeypatch.setattr(runlimit.subprocess, "run", boom)
    assert runlimit.available(refresh=True) is False


def test_available_is_false_when_systemd_run_refuses(monkeypatch):
    """A container with no user manager: systemd-run exists and exits non-zero."""

    def refuse(*a, **k):
        return subprocess.CompletedProcess(a[0], 1, b"", b"Failed to connect to bus")

    monkeypatch.setattr(runlimit.subprocess, "run", refuse)
    assert runlimit.available(refresh=True) is False


def test_available_is_probed_once_then_cached(monkeypatch):
    calls = []

    def probe(*a, **k):
        calls.append(1)
        return subprocess.CompletedProcess(a[0], 0, b"", b"")

    monkeypatch.setattr(runlimit.subprocess, "run", probe)
    runlimit.available(refresh=True)
    runlimit.available()
    runlimit.available()
    assert len(calls) == 1


# --- reading the cgroup back ----------------------------------------------


def _fake_cgroup(tmp_path: Path, name: str, oom_kills: int, peak: int | None) -> Path:
    cg = tmp_path / "cgroup" / name
    cg.mkdir(parents=True)
    (cg / "memory.events").write_text(
        f"low 0\nhigh 0\nmax 39\noom 1\noom_kill {oom_kills}\n", encoding="utf-8"
    )
    if peak is not None:
        (cg / "memory.peak").write_text(f"{peak}\n", encoding="utf-8")
    return cg


def test_sample_reads_the_kills_and_the_peak(tmp_path, monkeypatch):
    cg = _fake_cgroup(tmp_path, runlimit.scope_name(7), oom_kills=2, peak=314572800)
    monkeypatch.setattr(runlimit, "_cgroup_of", lambda pid: cg)
    got = runlimit.sample(pid=123, run_id=7)
    assert got == runlimit.Sample(peak_bytes=314572800, oom_kills=2)


def test_sample_survives_a_kernel_without_memory_peak(tmp_path, monkeypatch):
    """memory.peak is Linux 5.19+. Its absence costs the nice-to-have number
    and none of the kill detection."""
    cg = _fake_cgroup(tmp_path, runlimit.scope_name(7), oom_kills=1, peak=None)
    monkeypatch.setattr(runlimit, "_cgroup_of", lambda pid: cg)
    got = runlimit.sample(pid=123, run_id=7)
    assert got == runlimit.Sample(peak_bytes=None, oom_kills=1)


def test_sample_refuses_a_cgroup_that_is_not_this_runs_scope(tmp_path, monkeypatch):
    """The self-check that matters: on an unwrapped spawn the process sits in
    the *portal's own* cgroup, and reporting the portal's memory events as the
    run's would invent an OOM that never happened."""
    cg = _fake_cgroup(tmp_path, "project-portal.service", oom_kills=5, peak=1)
    monkeypatch.setattr(runlimit, "_cgroup_of", lambda pid: cg)
    assert runlimit.sample(pid=123, run_id=7) is None


def test_sample_is_none_for_a_process_that_has_gone(monkeypatch):
    assert runlimit.sample(pid=2**30, run_id=7) is None


def test_cgroup_of_reads_the_v2_line_for_a_live_process():
    cg = runlimit._cgroup_of(os.getpid())  # noqa: SLF001
    assert cg is not None
    assert str(cg).startswith(str(runlimit.CGROUP_ROOT))


def test_kill_note_names_the_cap_and_says_what_to_do():
    db.set_setting("run_memory_max", "3G")
    note = runlimit.kill_note()
    assert "3.0 GiB" in note
    assert "run_memory_max" in note
    # It must be clear the run itself was not stopped, or it reads as the cap
    # having killed the work.
    assert "not stopped" in note


# --- the watcher, and what the run reports --------------------------------


def test_watcher_records_a_kill_and_announces_it_once(tmp_path, monkeypatch):
    cg = _fake_cgroup(tmp_path, runlimit.scope_name(7), oom_kills=1, peak=999)
    monkeypatch.setattr(runlimit, "_cgroup_of", lambda pid: cg)
    monkeypatch.setattr(agent_runner._MemoryWatch, "INTERVAL_S", 0)  # noqa: SLF001

    seen: list[str] = []

    def on_event(event, lines):
        seen.append(lines[0])

    watch = agent_runner._MemoryWatch(os.getpid(), 7, on_event)  # noqa: SLF001

    async def drive():
        task = asyncio.create_task(watch.poll_forever())
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(drive())

    assert watch.oom_killed is True
    assert watch.peak_bytes == 999
    # Announced into the live log exactly once, however many polls happened.
    assert len(seen) == 1
    # Which of the two ceilings the note blames depends on whether this machine
    # can pool at all (kill_note reads the peak - 999 bytes here - against the
    # per-run cap), so assert only what both wordings must say. The wordings
    # themselves are pinned below.
    assert "was killed" in seen[0]
    assert "memory" in seen[0]


def test_watcher_says_nothing_on_a_healthy_run(tmp_path, monkeypatch):
    cg = _fake_cgroup(tmp_path, runlimit.scope_name(7), oom_kills=0, peak=42)
    monkeypatch.setattr(runlimit, "_cgroup_of", lambda pid: cg)
    watch = agent_runner._MemoryWatch(os.getpid(), 7, lambda e, l: None)  # noqa: SLF001
    watch.read_once()
    assert watch.oom_killed is False
    assert watch.peak_bytes == 42


def test_a_broken_watch_never_kills_the_run(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("cgroupfs went away")

    monkeypatch.setattr(runlimit, "cgroup_for", boom)
    watch = agent_runner._MemoryWatch(1, 7, None)  # noqa: SLF001
    asyncio.run(asyncio.wait_for(watch.poll_forever(), timeout=5))
    assert watch.oom_killed is False


def test_run_result_carries_the_memory_facts():
    r = agent_runner.RunResult(ok=True, oom_killed=True, peak_memory_bytes=5)
    assert r.oom_killed and r.peak_memory_bytes == 5
    # Default off, so every existing construction of a RunResult is unchanged.
    assert agent_runner.RunResult(ok=True).oom_killed is False


def test_a_dead_run_says_memory_rather_than_no_output():
    from app import worker

    db.set_setting("run_memory_max", "3G")
    result = agent_runner.RunResult(ok=False, oom_killed=True)
    assert "memory cap" in worker._failure_detail(result, 400)  # noqa: SLF001
    # ...and without the OOM the old wording is untouched.
    assert worker._failure_detail(agent_runner.RunResult(ok=False), 400) == "(no output)"  # noqa: SLF001


def test_a_successful_run_still_journals_the_kill():
    """The cap kills only the greedy process, so this is the case that would
    otherwise vanish: the agent works around a mysteriously `Killed` command
    and reports success, and nobody ever learns the machine is too small."""
    from app import worker

    db.set_setting("run_memory_max", "3G")
    project = db.create_project("Memory Hog", kind="software")
    worker._note_memory_kill(  # noqa: SLF001
        project, "build",
        agent_runner.RunResult(ok=True, oom_killed=True, peak_memory_bytes=3 * 1024**3),
    )
    entries = [e["content_md"] for e in db.list_journal(project["id"])]
    assert any("memory cap" in e and "3.0 GiB" in e for e in entries)


def test_nothing_is_journalled_when_the_cap_never_fired():
    from app import worker

    project = db.create_project("Well Behaved", kind="software")
    worker._note_memory_kill(project, "build", agent_runner.RunResult(ok=True))  # noqa: SLF001
    assert db.list_journal(project["id"]) == []


def test_the_settings_field_stores_only_sizes_it_will_honor():
    """A stored typo would sit on the page looking like a cap that is in force
    while runlimit ignores it, and the field is the only place anyone looks to
    find out what the cap is."""
    from app import settings_form

    clean = settings_form.REGISTRY["run_memory_max"].clean
    assert clean(" 6G ") == "6G"
    assert clean("") == ""
    assert clean("OFF") == "off"
    assert clean("lots") == ""
    assert clean("-3G") == ""


# --- the whole thing, against the real kernel -----------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _HAVE_SCOPES,
    reason="no user systemd manager to make a transient scope in",
)
async def test_a_greedy_tool_dies_and_the_run_carries_on(tmp_path, monkeypatch):
    """End to end, against the real cgroup: a command inside a run that asks
    for far more memory than the cap is killed by the kernel, the rest of the
    run keeps going and reports normally, and the portal knows it happened.

    This is the incident in miniature. Before the cap, the same allocation went
    to the machine's global OOM killer and took the portal down with it.
    """
    import json
    import stat
    import textwrap

    db.set_setting("run_memory_max", "256M")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    events = [
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
         "total_cost_usd": 0.0, "session_id": "sess-oom", "result": "survived"},
    ]
    script = bindir / "claude"
    script.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        # The runaway tool: allocate past the 256M cap until the cgroup kills us.
        #
        # Bounded at 1 GiB, and the bound is load-bearing. `runlimit.wrap` fails
        # OPEN by design, so if the scope is ever not applied - a probe that
        # passes but a spawn that refuses, a machine without cgroup v2 - this
        # allocation runs against the machine's own memory instead. An unbounded
        # `while True` there walks straight into the global OOM killer, which is
        # the exact incident this file exists to prevent: a test must not be able
        # to cause the bug it guards. (It could, and did, on 2026-07-26 - two of
        # that day's seven OOM events were the portal's own runs writing this
        # feature, `python3` at 17 GB, not the project under investigation.)
        #
        # 1 GiB is four times the cap, so the kill is certain when the scope is
        # there, and harmless when it is not - the test then fails its
        # `oom_killed` assertion below, which is the correct way to find out.
        python3 -c 'b=[]
        for _ in range(64): b.append(bytearray(16*1024*1024))' 2>/dev/null
        # ...and the agent carries on working for a while, which is the whole
        # point of OOMPolicy=continue - only the greedy process died.
        sleep 1
        # ...and reports as usual.
        cat <<'PORTAL_EOF'
        {json.dumps(events[0])}
        PORTAL_EOF
    """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(
        agent_runner, "_extra_env",
        lambda: {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
    )
    monkeypatch.setattr(agent_runner._MemoryWatch, "INTERVAL_S", 0.05)  # noqa: SLF001

    result = await agent_runner.run_claude(
        "prompt", tmp_path / "ws", "opus", timeout_min=2, run_id=4242,
    )

    assert result.ok, result.raw_stderr
    assert result.result_text == "survived"
    assert result.oom_killed is True, "the kernel kill was not noticed"
    assert result.peak_memory_bytes is not None
    assert result.peak_memory_bytes <= 256 * 1024**2


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _HAVE_SCOPES,
    reason="no user systemd manager to make a transient scope in",
)
async def test_canceling_a_scoped_run_still_kills_all_of_it():
    """The cancel button and the timeout both go through `_kill_group`, which
    SIGKILLs the spawn's *process group*. Wrapping the spawn in `systemd-run`
    puts another process between the portal and the CLI, so this checks the kill
    still reaches everything - a silently-broken cancel would leave runs
    unstoppable from the UI, and the timeout path would hang on descendants
    holding stdout open.

    Two properties, and both were nearly wrong:

    - `--scope` execs the command in place, so the spawn's pid *is* the command
      and its descendants inherit the process group. (`--service` would detach
      it and break this, which is why `wrap` does not use it.)
    - `start_new_session=True` on the spawn is load-bearing: without it
      `getpgid(child)` is the *portal's own* group and the portal would SIGKILL
      itself on every cancel. Writing this test without that flag did exactly
      that - the check killed its own interpreter.

    Membership is read from the scope's `cgroup.procs` rather than with
    `pgrep -f`, because `pgrep -f` matches the harness running the test as well
    as the processes under test.
    """
    unit = f"portal-killtest-{os.getpid()}.scope"
    cg = runlimit.CGROUP_ROOT / (
        f"user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice/{unit}"
    )

    def in_scope() -> list[str]:
        try:
            return (cg / "cgroup.procs").read_text(encoding="utf-8").split()
        except OSError:
            return []

    proc = await asyncio.create_subprocess_exec(
        "systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}",
        "-p", "MemoryMax=512M", "-p", "OOMPolicy=continue", "--",
        "/bin/sh", "-c", "sleep 120 & sleep 121 & sleep 122",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        await asyncio.sleep(1.5)
        assert os.getpgid(proc.pid) != os.getpgid(os.getpid()), (
            "the spawn shares the portal's process group; killpg would kill the portal"
        )
        before = in_scope()
        assert len(before) >= 2, f"expected the shell and its sleeps in the scope, got {before}"

        await agent_runner._kill_group(proc)  # noqa: SLF001

        await asyncio.sleep(1.0)
        assert in_scope() == [], f"survivors after cancel: {in_scope()}"
    finally:
        if proc.returncode is None:
            proc.kill()
        subprocess.run(
            ["systemctl", "--user", "stop", unit], capture_output=True, timeout=10
        )


# --- the unit file, which is half the fix ---------------------------------


def test_the_service_unit_refuses_to_die_with_its_children():
    """systemd's default OOMPolicy is `stop`: the kernel OOM-killing ANY
    process in the unit's cgroup stops the whole unit. Every agent run is a
    descendant, so without this line one project's runaway test command takes
    the portal and every other run down with it - which is exactly what
    happened, seven times."""
    unit = (Path(__file__).resolve().parent.parent / "deploy" / "project-portal.service")
    assert "OOMPolicy=continue" in unit.read_text(encoding="utf-8")


def test_the_spawn_isolates_its_process_group():
    """The flag the cancel test above proves is load-bearing. Pinned separately
    because it lives in `run_claude`, far from anything about memory, and its
    removal would present as the portal killing itself on cancel."""
    import inspect

    src = inspect.getsource(agent_runner.run_claude)
    assert "start_new_session=True" in src
