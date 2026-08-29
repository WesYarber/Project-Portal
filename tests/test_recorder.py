"""The voice recorder's choreography, run for real under bun.

Wes, 2026-08-04: "improve the interface when recording a voice note with the
'record audio' button on here so that it shows something responding to the
audio coming in in real time, shows the current length of the recording, and
allows for pausing, resuming, playback, and deleting a recorded voice note."

The waveform is visual-only and verified by screenshot; everything with logic
in it runs here against the real initRecorder out of app.js
(tests/js/recorder.mjs): the clock that must exclude pauses, the take that
must both attach and get a playback row, discard attaching nothing, delete
pulling the file back out of the input, and the mic being released on every
path. The first assertion below exists because the browser test caught it: a
take that was never paused banked no time at all and every row read
"0:00 voice note".
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "static" / "app.js"


@pytest.fixture(scope="module")
def ran():
    bun = shutil.which("bun")
    if not bun:  # pragma: no cover - bun is present on the machines that matter
        pytest.skip("bun is not installed")
    proc = subprocess.run(
        [bun, str(Path(__file__).parent / "js" / "recorder.mjs"), str(APP_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_take_that_was_never_paused_still_knows_its_length(ran):
    """The browser-caught bug: recordedMs() leaned on recorder.state, which is
    already 'inactive' inside onstop, so an unpaused take banked nothing and
    its row read 0:00. The recorder tracks running-ness itself now."""
    assert ran["micToggle"]["rowText"] == "1:01 voice note"


def test_the_clock_runs_while_recording(ran):
    assert ran["started"]["time"] == "0:00"
    assert ran["after5s"] == "0:05"
    assert ran["longTakeClock"] == "1:01"


def test_pause_holds_the_clock_and_resume_restarts_it(ran):
    """5s recorded, 3s paused, 2s more: the length is 0:07, not 0:10."""
    assert ran["pausedState"] == "paused"
    assert ran["pauseLabel"] == "resume"
    assert ran["timeWhilePaused"] == "0:05"
    assert ran["timeAfterResume"] == "0:07"


def test_the_take_row_carries_the_paused_arithmetic(ran):
    assert ran["take"]["rowText"] == "0:07 voice note"


def test_starting_opens_the_panel_and_sleeps_the_submits(ran):
    """A navigation mid-take would lose the recording silently, so the form's
    submit buttons sleep while the mic is hot."""
    assert ran["buttonRevealed"] is True
    assert ran["started"]["panelShown"] is True
    assert ran["started"]["recordingClass"] is True
    assert ran["started"]["submitsDisabled"] is True


def test_done_attaches_the_file_and_builds_a_playback_row(ran):
    take = ran["take"]
    assert take["panelHidden"] is True
    assert take["recordingClass"] is False
    assert take["submitsWoken"] is True
    assert take["files"] == 1
    assert take["name"].startswith("voice-memo-") and take["name"].endswith(".webm")
    assert take["rowHasAudio"] is True
    assert take["micReleased"] == 1


def test_delete_pulls_the_file_back_out_of_the_input(ran):
    after = ran["afterDelete"]
    assert after["files"] == 0
    assert after["rows"] == 0
    assert after["urlRevoked"] == 1


def test_discard_attaches_nothing_but_still_releases_the_mic(ran):
    d = ran["discard"]
    assert d["files"] == 0
    assert d["rows"] == 0
    assert d["panelHidden"] is True
    assert d["micReleased"] == 2


def test_the_mic_button_doubles_as_done_while_a_take_is_open(ran):
    assert ran["micToggle"]["files"] == 1


def test_a_denied_mic_disables_the_button_and_says_so(ran):
    assert ran["denied"]["disabled"] is True
    assert ran["denied"]["status"] == "microphone unavailable"


def test_the_recorder_claims_the_file_it_just_attached(ran):
    """So the staged-file shelf leaves the memo to this shelf.

    A voice memo goes into the same <input type=file> as any dropped
    screenshot, so the shelf added on 2026-08-28 would list it as an ordinary
    file - a second row for one recording, with a delete button that knows
    nothing about the playback row above it. The recorder registers the name it
    used and the shelf skips it.

    A registration rather than a guess from the name: "starts with voice-memo-"
    would also hide a file the user happened to call that, and there would be
    no playback row for it either, so it would be unremovable.

    A mutation sweep found this uncovered - the shelf's own harness writes the
    claim by hand to build its fixture, so nothing there can see whether the
    recorder ever makes one."""
    take = ran["take"]

    assert take["claimed"] == [take["name"]]


def test_deleting_a_take_releases_its_claim(ran):
    """Left behind, the name is a permanent instruction to hide any file called
    that - so attaching a real file under the deleted memo's name would show
    nothing on the shelf and give no way to take it back off."""
    assert ran["afterDelete"]["claimed"] == []
