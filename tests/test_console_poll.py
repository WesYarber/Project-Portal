"""Pollers that stop, and that go quiet in a tab nobody is looking at.

Wes, 2026-08-13, relaying a diagnosis an agent on his Mac had already done:

    "This project portal here seems to be making my computer run super hot and
    use up battery quickly. [...] the console poller is the real leak. Its
    interval handle is never stored, so clearInterval is impossible. When the
    run finishes it sets live = false and calls liveReload() - but that does an
    in-place DOM patch, not a navigation, so the page never unloads and the
    2-second poller runs forever, against a finished run, in a hidden tab. One
    open portal tab = permanent traffic 24/7."

That was right in every particular. app.js had three pollers and
`document.hidden` appeared exactly once in the whole file (initLiveRefresh, the
2.5s version poll). The other two - /api/active-run every 5s and
/api/run/N/log every 2s - polled a hidden tab at full rate, and the console one
had no way to ever stop.

Every part of this is about the LIFETIME of a timer, which no assertion about
the source text can see: `clearInterval` was already in app.js before the fix,
in a different poller. So the behavior is driven for real under bun
(tests/js/console_poll.mjs) against a stub DOM and a clock the harness owns,
and the questions asked are "is a timer still armed" and "did a fetch happen".

The one thing left to the source is the wiring: reinit() has to call
startConsolePoll, because the console box is REUSED - project.html morphs
#agent-console to whichever run is current, and a poller that stopped when its
own run ended has to be replaced by one watching the new run.
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
    if not bun:
        pytest.skip("bun is not on PATH")
    harness = Path(__file__).parent / "js" / "console_poll.mjs"
    out = subprocess.run(
        [bun, str(harness), str(APP_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# --------------------------------------------------------------------------
# The leak itself
# --------------------------------------------------------------------------

def test_a_live_run_polls_every_two_seconds(ran):
    """The feature the leak was hiding inside still has to work."""
    started = ran["liveRunStops"]["afterStart"]
    assert started["fetches"] == 1, "the transcript is fetched once on load"
    assert started["timers"] == [2000]

    running = ran["liveRunStops"]["whileRunning"]
    assert running["fetches"] == 3, "one fetch per tick while the run is live"
    assert running["timers"] == [2000]


def test_the_poller_stops_when_its_run_finishes(ran):
    """The bug, in one assertion.

    Before the fix the interval handle was discarded at the moment it was
    created, so this timer could not be cleared by anything short of leaving
    the page - and the end of a run does not leave the page, it patches it."""
    finished = ran["liveRunStops"]["atFinish"]
    assert finished["timers"] == [], "no timer survives the run it was watching"
    assert finished["liveReloads"] == 1, "the page is still patched once at the end"


def test_nothing_is_still_fetching_a_finished_run_much_later(ran):
    """What Wes actually felt: a tab left open overnight.

    100 ticks is a little over three minutes of the old cadence, and the real
    complaint was 28 days of it."""
    assert ran["liveRunStops"]["fetchesInTheNext100Ticks"] == 0


def test_opening_a_finished_run_arms_no_timer(ran):
    """Reading yesterday's transcript should cost exactly one request."""
    done = ran["finishedRun"]
    assert done["fetches"] == 1
    assert done["timers"] == []
    # Nothing "finished" here - the run was already over when the page opened,
    # so patching the page would be a reload nobody asked for.
    assert done["liveReloads"] == 0


# --------------------------------------------------------------------------
# The hidden tab
# --------------------------------------------------------------------------

def test_a_hidden_tab_does_not_fetch_the_transcript(ran):
    hidden = ran["hiddenTab"]
    assert hidden["whileHidden"] == 0, "a tab nobody is looking at holds no connection open"
    # The timer stays armed on purpose: a callback that checks one boolean and
    # returns costs nothing, and it is what lets the run be picked back up
    # without re-registering anything. What costs is the request.
    assert hidden["stillArmedWhileHidden"] == [2000]


def test_coming_back_to_the_tab_fetches_at_once(ran):
    """Otherwise the fix would trade heat for a stale transcript: up to two
    seconds is nothing, but the gate has to not wait for the next tick when the
    reader is already looking at it."""
    assert ran["hiddenTab"]["onReturning"] == 1


def test_the_active_run_poller_is_gated_the_same_way(ran):
    """The quieter half of the leak: this one runs on EVERY page, and it never
    stops on its own because there is always a next run to notice."""
    strip = ran["activeRunPoller"]
    assert strip["onLoad"] == 1
    assert strip["interval"] == [5000]
    assert strip["whileHidden"] == 0
    assert strip["onReturning"] == 1


# --------------------------------------------------------------------------
# Restarting without stacking
# --------------------------------------------------------------------------

def test_repeated_starts_do_not_stack_pollers(ran):
    """reinit() calls startConsolePoll on every live patch, and a page being
    patched is exactly when a leak would compound."""
    again = ran["repeatedStarts"]
    assert again["timers"] == [2000], "one poller, however many times it is started"
    assert again["visibilityListeners"] == 1, (
        "the visibilitychange listener is registered at module scope; inside "
        "startConsolePoll it would be the same leak in a different coat"
    )


def test_restarting_the_same_run_does_not_refetch_the_transcript(ran):
    """A restart resets the byte offset to 0, which re-renders the whole
    transcript and drops the reader at the bottom of it. On a page that patches
    itself every few seconds that would be unreadable."""
    assert ran["repeatedStarts"]["extraFetches"] == 0


def test_the_poller_follows_the_box_to_a_new_run(ran):
    """#agent-console is reused: project.html morphs it to whichever run is
    current, so this is not only about stopping - a new run has to be picked
    up, and by exactly one poller."""
    moved = ran["runChanged"]
    assert moved["timers"] == [2000], "the old run's timer went with it"
    assert moved["urls"] == ["/api/run/13/log?offset=0"]


def test_the_same_run_going_live_re_arms_the_timer(ran):
    """Found by the delete-the-fix sweep: guarding on the run id ALONE passed
    every other test in this file.

    project.html renders #agent-console for the newest run whether or not it is
    running, so the id can stay put while data-live flips. A page rendered a
    beat before the run's status row commits shows data-live="0" and is
    corrected by the very next morph - and under an id-only guard that page
    would sit on a frozen console forever."""
    lit = ran["sameRunGoesLive"]
    assert lit["whileIdle"] == [], "a run the server calls finished arms nothing"
    assert lit["afterGoingLive"] == [2000]
    assert lit["urls"] == ["/api/run/12/log?offset=0"]


def test_the_console_leaving_the_page_takes_its_poller_with_it(ran):
    """Also found by the sweep. #agent-console sits inside a fold the morph can
    remove outright, and a timer whose box is gone is the original leak
    exactly - with nothing on screen to hint that it is still running."""
    gone = ran["boxRemoved"]
    assert gone["beforeItWent"] == [2000]
    assert gone["timers"] == []
    assert gone["fetches"] == 0


def test_a_superseded_reply_does_not_paint_or_tear_down_the_new_run(ran):
    """The race the run-id switch opens: run 12's request is still in flight
    when run 13 takes over. Its reply says running:false, and left unguarded it
    would paint run 12's tail into run 13's console and then stop run 13's
    poller for being 'finished'."""
    race = ran["supersededMidFlight"]
    assert race["paintedBefore"] == ["run 13\n"]
    assert race["paintedAfter"] == ["run 13\n"], "the stale tail was dropped"
    assert race["timers"] == [2000], "run 13 is still being polled"
    assert race["liveReloads"] == 0


# --------------------------------------------------------------------------
# The wiring, which behavior alone cannot pin
# --------------------------------------------------------------------------

def test_reinit_restarts_the_console_poller():
    """Without this the poller stops at the end of a run and never comes back,
    so a new run started from the project page would show an empty console
    until the reader reloaded by hand."""
    src = APP_JS.read_text()
    start = src.index("function reinit() {")
    end = src.index("\n}", start)
    body = src[start:end]
    assert "startConsolePoll()" in body


def test_every_poller_in_the_app_is_gated_on_document_hidden():
    """The regression that would undo all of this: a new poller added later
    with no gate. Every setInterval whose callback fetches has to consult
    document.hidden first, so the check is that the two counts agree.

    A timer that genuinely is not a poller says so with a `// no-poll-gate:`
    comment and its reason, which is cheaper than a parser and leaves the
    exemption where the next reader will see it.

    This check is worth more than the rest of the file put together: it is what
    found watchForOffline, which the original diagnosis missed entirely. That
    one pings /api/ping every 3 SECONDS on every page of the portal and never
    stops - more traffic than the console poller that was reported, on far more
    pages."""
    sources = {
        "app.js": APP_JS.read_text(),
        "oneoff.html": (ROOT / "app" / "templates" / "oneoff.html").read_text(),
    }
    ungated = {}
    for name, src in sources.items():
        polls = src.count("setInterval(")
        gates = src.count("if (document.hidden) return;")
        exempt = src.count("// no-poll-gate:")
        if polls != gates + exempt:
            ungated[name] = {"setInterval": polls, "gated": gates, "exempt": exempt}
    assert not ungated, f"pollers without a document.hidden gate: {ungated}"

    # The other half of the bargain. A gate with no wake-up trades heat for
    # staleness: the tab comes back and shows whatever it had when it was
    # hidden until the next tick comes round.
    asleep = {
        name: (src.count("if (document.hidden) return;"), src.count("visibilitychange"))
        for name, src in sources.items()
        if src.count("if (document.hidden) return;") != src.count("visibilitychange")
    }
    assert not asleep, f"gated pollers with no visibilitychange wake-up: {asleep}"
