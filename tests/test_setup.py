"""The bootstrap an agent runs on a machine nobody has configured.

`deploy/setup.py` is the first thing that executes on a fresh clone, which
makes its failure modes unusually expensive: nobody is watching, and a script
that reports success because `pip` exited 0 hands back a portal that does not
serve. So the tests here are about the two claims it makes - "this is
idempotent" and "I asked it and it answered" - rather than about its output.
"""
from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    """Import deploy/setup.py by path.

    It is a script rather than a package member - it has to run under the
    SYSTEM python before a virtualenv exists, so it cannot be `from app import
    ...`-able and cannot import anything from `app` at module scope.
    """
    spec = importlib.util.spec_from_file_location("portal_setup", ROOT / "deploy" / "setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = _load()


# --- the promise that it can be re-run --------------------------------------

def test_it_imports_nothing_from_the_app():
    """The trap this script is one careless line away from. It runs BEFORE the
    virtualenv exists, under whatever python the machine has, so an
    `from app import config` at the top would make the setup script fail with
    ModuleNotFoundError on exactly the machine it exists to set up."""
    text = (ROOT / "deploy" / "setup.py").read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith(("import app", "from app ")):
            pytest.fail(f"deploy/setup.py:{lineno} imports the app at module scope: {stripped}")


def test_the_app_is_only_reached_through_the_virtualenv():
    """The corollary: it still has to ASK the app about itself. It does that by
    running the venv's python as a subprocess, which is the only interpreter
    that has the dependencies."""
    text = (ROOT / "deploy" / "setup.py").read_text(encoding="utf-8")
    assert "from app import config" in text  # inside a -c string, not an import
    assert "venv_python()" in text


def test_check_mode_never_boots_the_portal():
    """`--check` promises to change nothing, and booting the app creates data/
    and the database on a fresh clone. Guarding only on `--no-smoke-test` was
    the first version, and it wrote a database on every 'read-only' run."""
    source = (ROOT / "deploy" / "setup.py").read_text(encoding="utf-8")
    guard = [ln for ln in source.splitlines() if "smoke_test(report)" in ln and "def " not in ln]
    assert guard, "the smoke test is no longer called where this test can read its guard"
    # The call is inside the `if` block; find the condition above it.
    lines = source.splitlines()
    at = lines.index(guard[0])
    condition = "\n".join(lines[max(0, at - 4):at])
    assert "args.check" in condition


# --- the virtualenv, and the machine that cannot make one -------------------
#
# Both tests below exist because running this script on a genuinely fresh clone
# failed in a way none of the others could see. `python3 -m venv` lays the
# directory and the interpreter symlink down FIRST and bootstraps pip second,
# so a box without ensurepip - Debian and Ubuntu, where it is the separate
# python3-venv package - leaves behind an interpreter that exists and runs and
# cannot install anything.


def _fake_venv(root: Path, *, with_pip: bool) -> Path:
    """A venv-shaped directory whose python answers `import pip` or does not."""
    binaries = root / ("Scripts" if os.name == "nt" else "bin")
    binaries.mkdir(parents=True, exist_ok=True)
    python = binaries / "python"
    python.write_text("#!/bin/sh\nexit %d\n" % (0 if with_pip else 1))
    python.chmod(0o755)
    return python


def test_a_half_bootstrapped_virtualenv_does_not_count_as_one(tmp_path, monkeypatch):
    """The file test this replaced said "already" on every re-run of a setup
    that had never worked, then failed further down naming neither cause nor
    cure."""
    monkeypatch.setattr(setup, "VENV", tmp_path / "venv")
    _fake_venv(tmp_path / "venv", with_pip=False)
    assert setup.venv_usable() is False
    _fake_venv(tmp_path / "venv", with_pip=True)
    assert setup.venv_usable() is True


def test_a_missing_virtualenv_is_not_usable_either(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "VENV", tmp_path / "nothing-here")
    assert setup.venv_usable() is False


def test_the_venv_failure_says_what_the_tool_said_on_stdout(tmp_path, monkeypatch):
    """The line that was thrown away. `python -m venv` prints its ensurepip
    failure - the one naming the apt package to install - on STDOUT, so a
    message built from stderr alone came back as `FAILED  could not create a
    virtualenv:` and nothing after the colon. On the machine where it matters
    that is the difference between one apt command and an afternoon."""
    fake_python = tmp_path / "python-that-cannot"
    fake_python.write_text(
        "#!/bin/sh\n"
        "echo 'ensurepip is not available. apt install python3.14-venv'\n"
        "exit 1\n"
    )
    fake_python.chmod(0o755)
    monkeypatch.setattr(setup, "VENV", tmp_path / "venv")
    monkeypatch.setattr(sys, "executable", str(fake_python))

    report = setup.Report()
    assert setup.make_venv(report, check_only=False) is False
    assert report.failed
    assert any("apt install python3.14-venv" in m for m in report.failures), report.failures


def test_an_unusable_virtualenv_is_cleared_rather_than_reused(tmp_path, monkeypatch):
    """Left in place it is indistinguishable from a good one to everything
    downstream, so a second run of the script could never repair the first."""
    venv = tmp_path / "venv"
    _fake_venv(venv, with_pip=False)
    (venv / "marker").write_text("from the failed attempt")

    maker = tmp_path / "python-that-can"
    maker.write_text(
        "#!/bin/sh\n"
        f"mkdir -p {venv}/bin\n"
        f"printf '#!/bin/sh\\nexit 0\\n' > {venv}/bin/python\n"
        f"chmod 755 {venv}/bin/python\n"
    )
    maker.chmod(0o755)
    monkeypatch.setattr(setup, "VENV", venv)
    monkeypatch.setattr(sys, "executable", str(maker))

    report = setup.Report()
    assert setup.make_venv(report, check_only=False) is True
    assert not report.failed
    assert not (venv / "marker").exists(), "the failed attempt was reused rather than cleared"


def test_the_configuration_is_not_reported_from_a_broken_virtualenv(tmp_path, monkeypatch, capsys):
    """A half-bootstrapped interpreter runs, and `app/config` imports cleanly
    under it - so this printed a confident "reachable at http://..." line
    directly beneath the FAILED saying there was no environment. Two lines
    contradicting each other is worse than either alone."""
    monkeypatch.setattr(setup, "VENV", tmp_path / "venv")
    _fake_venv(tmp_path / "venv", with_pip=False)

    report = setup.Report()
    setup.check_config(report)
    assert "reachable at" not in capsys.readouterr().out
    assert not report.failed


def test_check_mode_never_deletes_a_virtualenv(tmp_path, monkeypatch):
    """--check changes nothing, and "nothing" has to include the broken
    directory it is reporting on."""
    venv = tmp_path / "venv"
    _fake_venv(venv, with_pip=False)
    monkeypatch.setattr(setup, "VENV", venv)

    report = setup.Report()
    assert setup.make_venv(report, check_only=True) is False
    assert venv.exists()
    assert any("unusable" in item for item in report.human)


# --- the smoke test, which is the only claim that matters -------------------

def test_a_free_port_is_actually_free():
    """Picking a number and hoping is how a smoke test fails on a busy box and
    reads as a broken checkout."""
    port = setup.free_port()
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))  # raises if something took it


def test_two_calls_do_not_hand_back_the_same_port():
    """Two smoke tests in a row must not collide with each other."""
    assert setup.free_port() != setup.free_port()


def test_the_ping_it_asks_for_is_the_one_the_app_serves():
    """The endpoint is named in two files, and a rename in app/main.py would
    otherwise leave the setup script waiting 45 seconds for a 404 and then
    reporting a broken install."""
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert f'@app.get("{setup.PING_PATH}")' in main
    assert f'PlainTextResponse("{setup.PING_EXPECTED}")' in main


def test_the_smoke_test_fails_when_the_portal_will_not_start(tmp_path, monkeypatch):
    """The check that the check can fail. A smoke test that reports OK against
    a process that died is worse than no smoke test, because it converts an
    obvious failure into a confident one.

    Asserting the *message* and the elapsed time, not just the False. Without
    the `proc.poll()` check inside the loop a dead process still fails - by
    timing out after 45 seconds and reporting "did not answer", which reads as
    a slow machine rather than as a crash and throws away the traceback that
    says why. A mutation sweep found that this test could not tell those apart.
    """
    fake_venv = tmp_path / "venv" / "bin"
    fake_venv.mkdir(parents=True)
    python = fake_venv / "python"
    python.write_text("#!/bin/sh\necho 'ImportError: no such module'\nexit 3\n")
    python.chmod(0o755)
    monkeypatch.setattr(setup, "venv_python", lambda: python)
    monkeypatch.setattr(setup, "ROOT", tmp_path)

    report = setup.Report()
    started = time.monotonic()
    assert setup.smoke_test(report) is False
    assert report.failed
    assert time.monotonic() - started < 20, "it waited out the timeout instead of noticing the exit"
    assert any("exited during startup" in m for m in report.failures), report.failures
    assert any("ImportError" in m for m in report.failures), "the output that says why is dropped"


def test_the_smoke_test_rejects_the_wrong_answer(tmp_path, monkeypatch):
    """A process that binds the port and serves something else is not the
    portal. Waiting for a connection to succeed is not the same as waiting for
    the right application to be behind it."""
    script = tmp_path / "impostor.py"
    script.write_text(
        "import sys\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200); self.end_headers(); self.wfile.write(b'nope')\n"
        "    def log_message(self, *a): pass\n"
        "port = int(sys.argv[sys.argv.index('--port') + 1])\n"
        "HTTPServer(('127.0.0.1', port), H).serve_forever()\n"
    )
    launcher = tmp_path / "bin"
    launcher.mkdir()
    python = launcher / "python"
    # The setup script runs `<venv python> -m uvicorn app.main:app --host ... --port N`.
    # This stands in for that, keeping the trailing arguments so --port survives.
    python.write_text(f'#!/bin/sh\nshift 2\nexec {sys.executable} {script} "$@"\n')
    python.chmod(0o755)
    monkeypatch.setattr(setup, "venv_python", lambda: python)
    monkeypatch.setattr(setup, "ROOT", tmp_path)

    report = setup.Report()
    assert setup.smoke_test(report) is False
    assert report.failed


def test_the_smoke_test_passes_against_something_that_answers_pong(tmp_path, monkeypatch):
    """And the other direction, so the two tests above are evidence rather than
    a scanner that always says no."""
    script = tmp_path / "honest.py"
    script.write_text(
        "import sys\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200); self.end_headers(); self.wfile.write(b'pong')\n"
        "    def log_message(self, *a): pass\n"
        "port = int(sys.argv[sys.argv.index('--port') + 1])\n"
        "HTTPServer(('127.0.0.1', port), H).serve_forever()\n"
    )
    launcher = tmp_path / "bin"
    launcher.mkdir()
    python = launcher / "python"
    python.write_text(f'#!/bin/sh\nshift 2\nexec {sys.executable} {script} "$@"\n')
    python.chmod(0o755)
    monkeypatch.setattr(setup, "venv_python", lambda: python)
    monkeypatch.setattr(setup, "ROOT", tmp_path)

    report = setup.Report()
    assert setup.smoke_test(report) is True
    assert not report.failed


# --- what it hands back -----------------------------------------------------

def test_a_missing_claude_cli_is_a_job_for_a_person_not_a_failure():
    """The portal serves, holds ideas and answers questions with no CLI at all;
    what it cannot do is start a run. Exiting 1 there would tell an unattended
    setup that it had failed when it had in fact succeeded."""
    report = setup.Report()
    import shutil as _shutil

    original = _shutil.which
    try:
        _shutil.which = lambda name: None if name == "claude" else original(name)
        setup.check_claude_cli(report)
    finally:
        _shutil.which = original
    assert not report.failed
    assert any("Claude Code CLI" in item for item in report.human)


def test_check_mode_runs_clean_on_this_checkout():
    """The end-to-end one: it is a script, and a script that no longer parses
    is only discovered when somebody runs it on a fresh machine."""
    done = subprocess.run(
        [sys.executable, str(ROOT / "deploy" / "setup.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "already virtualenv" in done.stdout
