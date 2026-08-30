"""The page changes on the press, not two round trips later.

Wes, 2026-08-29: "Apply UI actions on the client immediately instead of waiting
on the server and a reload - acknowledge on the 'since you last checked in'
banner, add note, run agent - and let the next real page load correct any
mismatch."

The press-feedback work (#560, tests/test_press_feedback.py) made the WAIT
honest: a pressed button dims, says it is working, and swallows a second press.
It did not make the wait short. Until the POST comes back and the refetch it
patches from lands, the page still shows the old state - the banner still there,
the note still in the box, the run button still offering to start a run.

So these three change the page at press time and reconcile afterwards. Nothing
the client draws is authoritative: the forced patch that follows overwrites it,
and a real page load renders the truth from scratch, which is the "correct any
mismatch" half of what he asked for.

Four things can break, and each is checked its own way:

- The BEHAVIOR, run for real under bun (tests/js/optimistic.mjs) against a stub
  DOM, because every question here is about WHEN rather than about what the file
  contains. The stub holds the POST open so "the page changed before the server
  answered" is measured rather than assumed.
- The ORDERING, which is the sharpest trap in the feature: the note effect
  empties the very textarea the payload is built from. A line out of place and
  every note posts blank while the page shows it going out perfectly.
- The MARKUP: which forms opt in, and that each names an effect this file
  actually implements.
- The ROUTES: still ordinary form posts that redirect, so with scripting off the
  feature degrades to exactly what it replaced.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "static" / "app.js"
STYLE_CSS = ROOT / "app" / "static" / "style.css"
PROJECT_HTML = ROOT / "app" / "templates" / "project.html"


@pytest.fixture(scope="module")
def ran():
    bun = shutil.which("bun")
    if not bun:
        pytest.skip("bun is not on PATH")
    harness = Path(__file__).parent / "js" / "optimistic.mjs"
    out = subprocess.run(
        [bun, str(harness), str(APP_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# --------------------------------------------------------------------------
# Acknowledging the banner
# --------------------------------------------------------------------------

def test_the_banner_folds_away_on_the_press_not_on_the_answer(ran):
    """The feature itself, measured against a POST that has not come back.

    The banner is the first thing on the project page and the "since you last
    looked" summary is the thing you open a project to read. Pressing
    acknowledged used to leave it sitting there for two round trips.
    """
    ack = ran["acknowledge"]
    # The POST is in flight and has NOT been answered at this point, which is
    # what makes the first assertion mean anything.
    assert ack["postedYet"] == 1
    assert ack["hiddenBeforeTheServerAnswered"] is True
    # ...and it stays gone once the real patch lands.
    assert ack["hiddenAfter"] is True
    assert ack["patched"] == 1
    assert ack["alerts"] == []


def test_a_refused_post_puts_the_banner_back(ran):
    """An optimistic change is a guess, and a guess the server rejects has to
    come off.

    postBody() alerts the route's own reason. Leaving the page showing the
    change beside an alert saying it did not happen is worse than the wait this
    replaced, because now the page is actively lying rather than merely slow.
    """
    refused = ran["acknowledgeRefused"]
    assert refused["hiddenAfter"] is False
    assert refused["alerts"] == ["a run is already in flight"]
    # No patch on a refusal - there is nothing new to render.
    assert refused["patched"] == 0
    # And the busy mark comes off, or the button stays dead until a reload.
    assert refused["stillBusy"] is False


def test_a_banner_the_server_already_hid_is_left_alone(ran):
    """The guard that stops an undo from doing something nobody asked for.

    Without the "is it already hidden" check the effect would report an undo it
    did not earn - and a refused post would then REVEAL a banner the reader had
    never seen, which is a change appearing out of a failure.
    """
    assert ran["acknowledgeAlreadyHidden"]["hiddenAfter"] is True


# --------------------------------------------------------------------------
# Adding a note
# --------------------------------------------------------------------------

def test_the_note_appears_in_the_journal_and_the_box_empties_on_the_press(ran):
    """Both halves of what "sent" looks like, before the server has confirmed
    either.

    The echo goes to the FRONT of the feed because the journal is newest-first
    (db._JOURNAL_ORDER). Appended, it would land at the bottom of a scrolling
    box the reader is not looking at, which is the same as not showing it.
    """
    note = ran["note"]
    assert note["boxEmptiedBeforeTheServerAnswered"] is True
    assert note["echoIsFirst"] is True

    feed = note["feedBeforeTheServerAnswered"]["children"]
    assert len(feed) == 2
    echo, older = feed
    assert "optimistic-echo" in echo["className"]
    # It wears the same classes the server's own unsent note does, so it does
    # not read as a different kind of thing for the moment it is on screen.
    assert "journal-entry" in echo["className"]
    assert "note-unsent" in echo["className"]
    assert older["text"] == "an older entry"

    content = [c for c in echo["children"] if c["className"] == "content"]
    assert content and content[0]["text"] == "the printer jammed again"
    badge = echo["children"][0]["children"][0]
    assert badge["text"] == "sending..."

    # And the box stays empty once the post is accepted.
    assert note["boxAfter"] == ""
    assert note["patched"] == 1


def test_the_post_still_carries_the_note_the_effect_just_erased(ran):
    """The ordering trap, and the reason this test file exists at all.

    optimisticNote() empties the textarea. `new FormData(form)` reads that same
    textarea. Run the effect before the payload is built - which is the natural
    place to put it, right beside markBusy - and every note posts blank while
    the page shows it going out perfectly: echoed into the journal, box cleared,
    button pulsing. The failure is completely invisible from the client.

    So the call sits AFTER the FormData is built and after the submitter's
    name/value is set onto it, and this is what pins that.
    """
    note = ran["note"]
    assert note["postCarriesTheText"] is True
    assert "note=the+printer+jammed+again" in note["postedBody"]


def test_a_refused_note_gives_back_what_was_typed(ran):
    """Losing a note he had already typed is the worst failure this feature
    could have, so the text itself is asserted rather than a length.

    It is also why the effect only clears the TEXT. Staged files and recorded
    takes have their object URLs revoked on the way out, so that clear cannot be
    undone - they keep the old timing and are cleared by clearComposeForm() once
    the post has actually been accepted.
    """
    refused = ran["noteRefused"]
    assert refused["boxAfter"] == "the printer jammed again"
    # The echo is gone and the feed is back to what it was.
    assert refused["entries"] == 1
    assert refused["feedAfter"]["children"][0]["text"] == "an older entry"
    assert refused["alerts"] == ["a run is already in flight"]


def test_the_echo_puts_typed_text_on_the_page_as_text(ran):
    """The one place in app.js where a person's typing goes back onto their own
    project page without passing the server's markdown renderer.

    Everywhere else a note is rendered it has been through markdown_media() on
    the way. Here it has not, so `textContent` is the whole defense - and
    "innerHTML was never assigned" is asserted from a write-trap on the stub
    node rather than by reading the source, so a future rewrite that reaches for
    it fails here instead of shipping.
    """
    markup = ran["noteMarkup"]
    assert markup["asText"] == markup["sent"]
    assert markup["innerHTMLWrites"] == []
    # Belt and braces for the ordinary path too.
    assert ran["note"]["innerHTMLWrites"] == []


def test_an_empty_box_is_not_a_note(ran):
    """Whitespace is not something to echo, and with nothing echoed there must
    be nothing to give back on a refusal either."""
    blank = ran["noteBlank"]
    assert blank["entries"] == 1
    assert blank["boxBeforeTheServerAnswered"] == "   \n  "


def test_a_project_with_no_journal_box_still_sends_its_first_note(ran):
    """An empty project renders no `#journal` at all - the template guards it
    with `{% if journal %}`. The echo has nowhere to go, and that must cost the
    echo only: the box still empties and the post still goes."""
    none = ran["noteNoFeed"]
    assert none["boxEmptiedBeforeTheServerAnswered"] is True
    assert none["posted"] == 1
    assert none["alerts"] == []


# --------------------------------------------------------------------------
# Starting a run
# --------------------------------------------------------------------------

def test_the_run_button_takes_its_running_label_on_the_press(ran):
    """The slowest of the three, and so the one where the old wait read as
    nothing having happened: the route takes the work lock and spawns a process
    before it answers.

    Disabled as well as relabeled - "agent running..." over a button that still
    depresses is an invitation to press it again.
    """
    run = ran["run"]
    assert run["labelBeforeTheServerAnswered"] == "agent running..."
    assert run["disabledBeforeTheServerAnswered"] is True
    assert run["labelAfter"] == "agent running..."


def test_disabling_the_button_does_not_drop_what_it_was_carrying(ran):
    """The same ordering trap as the note's, on the other form.

    markBusy() sets aria-busy and deliberately NOT `disabled`, because disabling
    a submit button from inside its own submit event drops its name/value from
    the payload the browser is still serializing. This effect DOES disable it,
    which is safe only because the in-place path has already prevented the
    browser's submission and read the submitter itself - and only if it runs
    after that read.
    """
    assert "then=run" in ran["run"]["postedBody"]


def test_a_refused_run_stops_claiming_an_agent_is_running(ran):
    """The route refuses when a run is already in flight, and that refusal is
    the concrete reason the undo exists rather than a hypothetical one."""
    refused = ran["runRefused"]
    assert refused["label"] == "run agent now"
    assert refused["disabled"] is False
    assert refused["alerts"] == ["a run is already in flight"]


def test_a_browser_that_names_no_submitter_loses_the_effect_and_nothing_else(ran):
    """Safari before 15.4 reports no submitter. There is then no button to
    relabel, so the effect does nothing - and the post still goes, which is the
    old behavior rather than a broken one."""
    assert ran["runNoSubmitter"]["posted"] == 1
    assert ran["runNoSubmitter"]["alerts"] == []


def test_a_button_with_nothing_to_say_is_left_alone(ran):
    """The label comes off the button's own `data-optimistic-label`. With no
    label there is nothing to promise, so the effect must decline rather than
    press on - relabeling to nothing and disabling would leave a dead, wordless
    control where the run button was.
    """
    none = ran["pendingNoLabel"]
    assert none["label"] == "run agent now"
    assert none["disabled"] is False
    assert none["posted"] == 1


# --------------------------------------------------------------------------
# When the server is not there at all
# --------------------------------------------------------------------------

def test_a_post_that_never_comes_back_undoes_the_change_too(ran):
    """Not a hypothetical on this install: the portal restarts itself whenever
    an agent modifies its own source, so a POST sent in that window never gets a
    status back - the fetch rejects.

    postBody() has always alerted for that. What it did not do was distinguish
    it from success, and "the server never answered" is exactly the case where
    the page is left showing a change that certainly did not happen, on a server
    that is not there to correct it.
    """
    gone = ran["serverGone"]
    assert gone["hiddenAfter"] is False
    assert gone["alerts"] == ["The portal didn't answer - is it restarting?"]
    assert gone["stillBusy"] is False


def test_a_note_survives_the_portal_restarting_under_it(ran):
    """The same case, on the one form where a lost undo loses something the
    reader cannot get back by pressing again.

    The offline overlay catches this a moment later and holds the page until the
    service answers - but the note has to still be in the box when it does.
    """
    gone = ran["serverGoneNote"]
    assert gone["boxAfter"] == "the printer jammed again"
    assert gone["entries"] == 1


# --------------------------------------------------------------------------
# The clock behind the hold
# --------------------------------------------------------------------------

def test_pressing_starts_the_clock_the_hold_measures_against(ran):
    """What connects markBusy() to pressBlocked().

    They sit three thousand lines apart in app.js and `pressStartedAt` is the
    only thing between them, so deleting the stamp is a change that breaks the
    hold entirely while looking like tidying. The hold's own tests set the clock
    themselves - by design, since they are about the decision - which means none
    of them can see this.
    """
    clock = ran["pressClock"]
    assert clock["beforeAnyPress"] == 0
    assert clock["startedOnThePress"] is True
    # Stamped from the same clock the hold measures against. A wildly different
    # epoch would mean the hold either never applies or never expires.
    assert 0 <= clock["ageMs"] < 10_000


# --------------------------------------------------------------------------
# Everything that did not opt in
# --------------------------------------------------------------------------

def test_an_ordinary_in_place_form_is_untouched(ran):
    """`data-optimistic` is opt-in per form for the same reason `data-inplace`
    is: an effect is only safe where this file can predict the server's answer.

    A ticked todo, a swept list, a deleted file - all still post and patch with
    nothing drawn ahead of the server.
    """
    plain = ran["plainForm"]
    assert plain["posted"] == 1
    assert plain["patched"] == 1
    assert plain["bannerHidden"] is False
    assert plain["entries"] == 1


# --------------------------------------------------------------------------
# The markup
# --------------------------------------------------------------------------

def _forms(html: str) -> list[str]:
    return re.findall(r"<form\b[^>]*>", html)


def test_the_three_forms_wes_named_are_the_ones_that_opted_in():
    """Which forms carry the flag, so the feature cannot quietly spread to a
    route whose answer this file cannot predict."""
    html = PROJECT_HTML.read_text()
    opted = [f for f in _forms(html) if "data-optimistic=" in f]
    actions = sorted(re.search(r'action="([^"]+)"', f).group(1) for f in opted)
    assert actions == [
        "/project/{{ project.slug }}/acknowledge",
        "/project/{{ project.slug }}/note",
        "/project/{{ project.slug }}/run",
    ]


def test_every_declared_effect_is_one_app_js_implements():
    """A typo in the attribute value is silent - optimisticEffect() falls off
    the end and returns null, so the form simply goes back to waiting and
    nothing anywhere says why."""
    html = PROJECT_HTML.read_text()
    declared = set(re.findall(r'data-optimistic="([a-z]+)"', html))
    js = APP_JS.read_text()
    implemented = set(re.findall(r'if \(kind === "([a-z]+)"\)', js))
    assert declared == {"hide", "pending", "note"}
    assert declared <= implemented


def test_an_optimistic_form_is_also_an_in_place_form():
    """The effect is undone from the in-place handler's own `.then`. On a form
    that navigates instead there is no such hook - the page is already on its
    way out - so the two flags cannot be separated."""
    html = PROJECT_HTML.read_text()
    for form in _forms(html):
        if "data-optimistic=" in form:
            assert "data-inplace" in form, form


def test_the_hide_effect_names_a_target_that_exists_on_the_page():
    """`data-optimistic-target` is a selector resolved against the document. A
    stale one returns null and the effect silently does nothing."""
    html = PROJECT_HTML.read_text()
    targets = re.findall(r'data-optimistic-target="#([\w-]+)"', html)
    assert targets == ["work-summary"]
    for ident in targets:
        assert f'id="{ident}"' in html


def test_the_run_button_promises_the_label_the_server_will_render():
    """The optimistic label is a prediction of the server's own render. If the
    two disagree the button visibly changes wording under the reader's finger
    when the patch lands, which is the flicker this whole feature is meant to
    remove."""
    html = PROJECT_HTML.read_text()
    assert 'data-optimistic-label="agent running..."' in html
    # The same words the template renders once the run is actually in flight.
    assert "{% if active_run.active %}agent running..." in html


# --------------------------------------------------------------------------
# The look
# --------------------------------------------------------------------------

def test_the_echo_is_marked_by_a_class_so_the_morph_can_strip_it():
    """Load-bearing, and in the opposite direction to the busy mark.

    preservedAttr() refuses to REMOVE a data-* attribute the server did not
    render, which is exactly what keeps the busy mark alive across a background
    patch. An echo marked the same way would keep its half-sent look forever.
    `class` is synced straight from the server's render, so the morph turns the
    echo into the real entry and takes the marker off in the same patch.
    """
    js = APP_JS.read_text()
    assert 'entry.className = "journal-entry from-user note-unsent optimistic-echo"' in js
    # Nothing about the echo may be a data-* attribute, and it must not be in
    # MORPH_KEEP either - everything in that list is client-only state the
    # server knows nothing about, whereas this is a placeholder for something
    # the server is about to render.
    morph_keep = re.search(r"var MORPH_KEEP =(.+?);", js, re.S).group(1)
    assert "optimistic-echo" not in morph_keep
    assert ".optimistic-echo" in STYLE_CSS.read_text()
