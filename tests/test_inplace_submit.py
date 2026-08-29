"""Ticking a todo does not move the page.

Wes, 2026-08-04: "Checking a todo task jumps to the top of the page, but it
shouldn't."

It did, and the stash-and-restore in app.js was never going to be enough. The
todo section is a `<details>` the server always renders CLOSED - initFoldMemory
reopens it from localStorage a moment later, because the server cannot know what
the reader last had open - so at the instant restoreScroll() asks for a position
the fresh document is short by the entire height of the list. The browser clamps
to as far as the page can reach and the list then unfolds under a scroll
position that has already been decided.

So the one-click actions on a row stopped navigating. `data-inplace` posts with
fetch and lets the live-refresh morph patch the page, which already holds the
reader's line of text still. Nothing sets the scroll position, so nothing can
set it wrong.

Three separate things can break, and each is checked its own way:

- The BEHAVIOR, run for real under bun (tests/js/inplace_submit.mjs) against a
  stub DOM, with the confirm handler, the scroll stash and the in-place handler
  registered in file order because the bugs here are all about order.
- The MARKUP: which forms carry the flag. The rule is that submitting CONSUMES
  the form - a ticked row, an answered question, a dismissed banner. A compose
  box you keep typing into stays an ordinary navigating form.
- The ROUTES: still ordinary form posts that redirect, so the feature degrades
  to exactly what it replaced when scripting is off.

2026-08-07 (#560): the same treatment reached the rest of the one-click
actions - the question card, acknowledging the work banner, sweeping completed
todos, deleting a file, unticking from the todo archive. Two things had to be
built for the question card, which is one form with three destinations: the
handler reads `ev.submitter` for the `formaction` it was aimed at and for its
own name/value (a tapped quick option), and it releases the focus inside the
form it posted, which is what retired the old "no text field" rule.

The line that sweep must not cross is in
`test_a_suggestion_dismisses_in_place_but_accepting_still_navigates`: a route
that redirects SOMEWHERE ELSE cannot be in place, because the patch refetches
the page you are on and the destination is silently thrown away.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import db

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "static" / "app.js"
PROJECT_HTML = ROOT / "app" / "templates" / "project.html"


# --------------------------------------------------------------------------
# The behavior, run for real under bun
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ran():
    bun = shutil.which("bun")
    if not bun:
        pytest.skip("bun is not on PATH")
    harness = Path(__file__).parent / "js" / "inplace_submit.mjs"
    out = subprocess.run(
        [bun, str(harness), str(APP_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_ticking_posts_over_fetch_and_never_navigates(ran):
    tick = ran["tick"]

    assert tick["prevented"] is True
    assert tick["plainSubmits"] == []
    assert [p["action"] for p in tick["posted"]] == ["/todo/7/toggle"]
    assert tick["posted"][0]["body"] == "done=1"
    # The page is patched, not reloaded - that is what leaves the scroll alone.
    assert tick["patched"] == 1


def test_ticking_stashes_no_scroll_position(ran):
    """The subtle way this change could have broken something it never touched.

    The scroll-stash listener is registered at the top of app.js and therefore
    runs BEFORE the in-place handler gets to call preventDefault. Left to
    itself it would write a position to sessionStorage that nothing ever
    consumes - and the next ordinary navigation to this same page would eat it
    and scroll a page the reader had only just opened."""
    assert ran["tick"]["stashed"] == []
    # ...while an ordinary form still gets the stash it has always had.
    assert ran["ordinary"]["stashedCount"] == 1
    assert ran["ordinary"]["prevented"] is False
    assert ran["ordinary"]["posted"] == 0


def test_a_checkbox_reaches_the_handler_at_all(ran):
    """form.submit() fires no submit event, so the templates' usual
    `onchange="this.form.submit()"` walks straight past every listener in
    app.js. A checkbox using it would have gone on doing a full navigation
    while the markup said data-inplace, and nothing would have said so."""
    assert ran["viaSubmitForm"]["requested"] == ["/todo/7/toggle"]
    assert ran["viaSubmitForm"]["posted"] == 1
    # Where requestSubmit does not exist (Safari before 16) the plain submit is
    # the old behavior: worse, not broken.
    assert ran["viaSubmitFormLegacy"]["plainSubmits"] == ["/todo/7/toggle"]


def test_a_canceled_delete_posts_nothing(ran):
    assert ran["canceledDelete"]["posted"] == 0
    assert ran["canceledDelete"]["stashedCount"] == 0


def test_without_fetch_it_falls_through_to_a_real_submit(ran):
    assert ran["noFetch"]["prevented"] is False
    assert ran["noFetch"]["stashedCount"] == 1


def test_a_refusal_is_surfaced_rather_than_swallowed(ran):
    assert ran["refused"]["alerts"] == ["a run is in flight"]
    assert ran["refused"]["patched"] == 0


def test_a_button_with_a_formaction_posts_where_it_points(ran):
    """A question card is one form with three destinations - answer, save for
    later, delete - hung off its buttons as `formaction`. Reading the form's
    own action would have sent every one of them to `answer`, so pressing
    delete would have silently answered the question with whatever was in the
    box instead of throwing it away."""
    assert ran["formaction"]["posted"] == ["/questions/4/delete"]


def test_the_pressed_button_carries_its_own_name_and_value(ran):
    """`new FormData(form)` does not include the submitter - only the browser's
    own submission does. Without this a tapped quick option posts an empty
    `choice`, and one-tap answers, the whole point of offering options, would
    answer against nothing."""
    body = ran["quickOption"]["body"]
    assert "choice=merge+it" in body
    # ...and what was already typed still rides along with the tap.
    assert "answer=and+here+is+why" in body


def test_a_multi_destination_form_navigates_when_the_submitter_is_unknown(ran):
    """Safari before 15.4 reports no `ev.submitter`. On a one-destination form
    that costs nothing. On a question card there is no way to tell a delete
    from an answer, so it falls back to a real navigation rather than guessing
    - and the scroll stash fires for it, because it really is navigating."""
    assert ran["noSubmitterSimple"]["prevented"] is True
    assert ran["noSubmitterSimple"]["posted"] == 1

    assert ran["noSubmitterMulti"]["prevented"] is False
    assert ran["noSubmitterMulti"]["posted"] == 0
    assert ran["noSubmitterMulti"]["stashedCount"] == 1


def test_the_posted_form_lets_go_of_its_own_focus(ran):
    """refreshBlocked() holds a live patch back while a text field has focus,
    so answering a question you had typed into would post and then appear to do
    nothing at all until you tapped somewhere else. Releasing the focus inside
    the form that just posted is what lets a form with a textarea in it be
    in-place; a field focused ELSEWHERE on the page is not ours to touch."""
    assert ran["releasedFocus"]["blurred"] == ["textarea"]
    assert ran["leftOtherFocusAlone"]["blurred"] == []


def test_the_morph_lets_a_hidden_input_change_but_still_guards_real_fields(ran):
    """The toggle posts its TARGET state out of a hidden input (`done=0|1`), so
    a preserved value went stale the moment the row was patched: unticking an
    item you had just ticked posted "done" a second time and the row would not
    come back. Nobody is mid-edit in a hidden input; everything a person can
    actually type into stays protected."""
    p = ran["preserved"]

    assert p["hiddenValue"] is False
    assert p["textValue"] is True
    assert p["checkboxChecked"] is True
    assert p["textareaValue"] is True
    assert p["optionSelected"] is True
    # Not a blanket exemption for hidden inputs - only their value.
    assert p["hiddenClass"] is False
    assert p["hiddenAttribute"] is True


# --------------------------------------------------------------------------
# Which forms carry the flag
# --------------------------------------------------------------------------

def _forms(html: str) -> list[str]:
    return re.findall(r"<form\b[^>]*>", html)


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


@pytest.fixture
def row(client, temp_data_dir):
    p = db.create_project("Portal", description="x", stage="active", slug="portal")
    tid = db.add_todo(p["id"], "wire the thing up", "agent")["id"]
    db.add_todo_tag(tid, "blocked")
    return client.get("/project/portal").text


def test_the_toggle_is_the_only_form_left_on_a_row(row):
    """Since 2026-08-04 the tag, re-file and delete actions live in the
    right-click menu (initTodoMenu in app.js), which posts over fetch through
    postForm - so the only form a row renders is the checkbox's toggle, and it
    is in place."""
    toggles = [f for f in _forms(row) if re.search(r'action="/todo/\d+/toggle', f)]
    assert toggles and all("data-inplace" in f for f in toggles)
    for action in ("/delete", "/person", "/tag"):
        assert not any(re.search(r'action="/todo/\d+' + action, f) for f in _forms(row)), (
            f"a row still renders a {action} form - that control moved into the menu"
        )


def test_a_compose_box_is_posted_in_place_and_then_emptied(row):
    """This test used to assert the opposite, and the reasoning was wrong.

    The old rule was that a compose box "is the other kind - the field is still
    yours after the post", so "Add a todo" had to keep navigating. Wes,
    2026-08-28: "When I click add note (and maybe other things now on the
    project page), it reloads the page now and puts me back at the top of the
    page. This is unacceptable."

    He is right, and the premise was backwards: what you typed went out with
    the post, so the field is exactly NOT still yours - which makes releasing
    the focus correct rather than rude, and on a phone it puts the keyboard
    away. What the old reasoning did get right is that the box has to be
    EMPTIED, since the morph preserves live field values on purpose; that is
    what data-compose is for, and it is asserted here beside data-inplace so
    the pair can never come apart.

    The +tag input beside it is deliberately left alone: it sits on a single
    row inside the list rather than at the bottom of the page, and it is the
    one field here whose value you may well want to reuse on the next row."""
    for f in _forms(row):
        if 'class="todo-add"' in f:
            assert "data-inplace" in f, f
            assert "data-compose" in f, f


def test_every_row_carries_an_id_so_the_morph_pairs_it_with_itself(client, temp_data_dir):
    """Only a bug once ticking patches the page instead of reloading it, and it
    was one: a ticked item sorts to the bottom of its list, so every row below
    moves up one. findMatch pairs id-less siblings by POSITION, and the morph
    deliberately preserves a checkbox's `checked` as the user's own state - so
    ticking item 13 left the tick sitting on item 14, which had slid into that
    slot. Caught by driving a browser; no DOM assertion would have shown it."""
    p = db.create_project("Portal", description="x", stage="active", slug="portal")
    ids = [db.add_todo(p["id"], f"item {i}", "agent")["id"] for i in range(3)]

    html = client.get("/project/portal").text

    for tid in ids:
        assert f'id="todo-{tid}"' in html


def test_the_checkbox_submits_through_the_helper_not_form_submit(row):
    assert 'onchange="submitForm(this.form)"' in row
    box = row.split('class="inline-form todo-toggle"')[1].split("</form>")[0]
    assert "this.form.submit()" not in box


# --------------------------------------------------------------------------
# The routes are unchanged
# --------------------------------------------------------------------------

def test_the_routes_still_answer_a_plain_form_post(client, temp_data_dir):
    """Nothing moved to a JSON API: the fetch posts the same form-encoded body
    to the same route the browser would, so scripting off costs the reader the
    scroll position and nothing else."""
    p = db.create_project("Portal", description="x", stage="active", slug="portal")
    tid = db.add_todo(p["id"], "tick me", "agent")["id"]

    r = client.post(f"/todo/{tid}/toggle", data={"done": "1"}, follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/project/portal"
    assert db.get_todo(tid)["done"]

    r = client.post(f"/todo/{tid}/toggle", data={"done": "0"}, follow_redirects=False)

    assert r.status_code == 303
    assert not db.get_todo(tid)["done"]


# --------------------------------------------------------------------------
# The rest of the one-click actions (#560)
#
# The todo row was the first to stop navigating; everything else that acts on
# one thing and leaves you on the same page followed. Each of these is a place
# where a single tap used to throw the reader back to the top of a page they
# were part-way down, which on a phone is most of the pages here.
# --------------------------------------------------------------------------

def _form_for(html: str, action_re: str) -> str:
    matches = [f for f in _forms(html) if re.search(action_re, f)]
    assert matches, f"no form posting to {action_re} on this page"
    return matches[0]


def test_answering_a_question_does_not_reload_the_page(client, temp_data_dir):
    """The one that mattered most: the questions page is a stack of cards, and
    answering the third one put you back at the header every time."""
    p = db.create_project("Portal", description="x", stage="active", slug="portal")
    db.file_question(p["id"], "Merge it or keep both?", quick_options=json.dumps(["merge it", "keep both"]))

    for page in ("/questions", "/project/portal"):
        html = client.get(page).text
        form = _form_for(html, r'action="/questions/\d+/answer')
        assert "data-inplace" in form, f"{page}: {form}"


def test_every_question_card_carries_an_id(client, temp_data_dir):
    """Same lesson the todo rows learned. An answered card leaves the list, so
    the ones below it move up - and the morph, which pairs id-less siblings by
    POSITION, preserves a textarea's value as the reader's own state. An answer
    half-typed into the third question would be left sitting on whichever card
    slid into that slot."""
    p = db.create_project("Portal", description="x", stage="active", slug="portal")
    ids = [
        db.file_question(p["id"], f"Question number {i}?").row["id"]
        for i in range(3)
    ]

    html = client.get("/questions").text

    for qid in ids:
        assert f'id="question-{qid}"' in html


def test_the_projects_one_click_actions_are_in_place(client, temp_data_dir):
    """Acknowledging the banner, sweeping the completed todos away and deleting
    a file are all "act on one thing, stay where you are" - and all three sit
    on the single longest page in the portal."""
    p = db.create_project("Portal", description="x", stage="active", slug="portal")
    tid = db.add_todo(p["id"], "already done", "agent")["id"]
    db.set_todo_done(tid, True)
    db.add_attachment(p["id"], "shot.png", "0001-shot.png", "image/png", 12)
    run_id = db.create_run(p["id"], "build", "opus")
    db.finish_run(run_id, "ok", summary="the console folds its errors away")

    html = client.get("/project/portal").text

    assert "data-inplace" in _form_for(html, r'action="/project/portal/acknowledge"')
    assert "data-inplace" in _form_for(html, r"todos/clear-completed")
    assert "data-inplace" in _form_for(html, r'action="/attachment/\d+/delete"')


def test_unticking_from_the_history_page_is_in_place(client, temp_data_dir):
    """The same route as the live list's toggle, on a page that is nothing but
    a long list - and its redirect goes to the PROJECT, so before this an
    untick did not merely move the scroll, it left the archive entirely."""
    p = db.create_project("Portal", description="x", stage="active", slug="portal")
    tid = db.add_todo(p["id"], "done ages ago", "agent")["id"]
    db.set_todo_done(tid, True)

    html = client.get("/project/portal/todos/history").text

    assert "data-inplace" in _form_for(html, r'action="/todo/\d+/toggle')
    assert f'id="todo-{tid}"' in html
    # The trap that would have made the flag decorative: form.submit() fires no
    # submit event, so the handler never sees it.
    assert 'onchange="submitForm(this.form)"' in html
    assert "this.form.submit()" not in html


def test_a_suggestion_dismisses_in_place_but_accepting_still_navigates(
    client, temp_data_dir
):
    """The line the sweep must not cross, and the memory page is where it is
    easiest to cross it: `dismiss` and `undo dismiss` come back to /memory, so
    they are in place; `accept` CREATES A PROJECT and redirects to it, which is
    the entire point of pressing it. In place, that button would have left the
    reader on the memory page while a project appeared somewhere they were not
    looking."""
    db.add_suggestion("A dice tower", "Parametric, prints in one piece")
    sid = db.add_suggestion("A cork engraver", "Power and speed tables")["id"]
    db.set_suggestion_status(sid, "dismissed")

    html = client.get("/memory").text

    assert "data-inplace" in _form_for(html, r'action="/suggestions/\d+/dismiss"')
    assert "data-inplace" in _form_for(html, r'action="/suggestions/\d+/restore"')
    assert "data-inplace" not in _form_for(html, r'action="/suggestions/\d+/accept"'), (
        "accepting a suggestion must navigate - it redirects to the new project"
    )
    assert f'id="suggestion-{sid}"' in html

    # And the redirects that justify each of those calls.
    r = client.post(f"/suggestions/{sid}/restore", follow_redirects=False)
    assert r.headers["location"] == "/memory"
    r = client.post(f"/suggestions/{sid}/dismiss", follow_redirects=False)
    assert r.headers["location"] == "/memory"


def test_the_swept_routes_still_redirect_the_way_they_did(client, temp_data_dir):
    """The flag changes nothing on the server. Every one of these is still an
    ordinary form post that redirects, so with scripting off the sweep costs
    the reader exactly the scroll position it was meant to save."""
    p = db.create_project("Portal", description="x", stage="active", slug="portal")
    q = db.file_question(p["id"], "Merge it?", quick_options=json.dumps(["merge it", "keep both"])).row
    tid = db.add_todo(p["id"], "already done", "agent")["id"]
    db.set_todo_done(tid, True)
    aid = db.add_attachment(p["id"], "shot.png", "0001-shot.png", "image/png", 12)

    cases = [
        (f"/questions/{q['id']}/answer", {"choice": "merge it", "next": "/questions"}, "/questions"),
        ("/project/portal/acknowledge", {}, "/project/portal"),
        ("/project/portal/todos/clear-completed", {}, "/project/portal"),
        (f"/attachment/{aid}/delete", {}, "/project/portal"),
    ]
    for url, data, destination in cases:
        r = client.post(url, data=data, follow_redirects=False)
        assert r.status_code == 303, url
        assert r.headers["location"] == destination, url


def test_an_answer_still_lands_when_the_option_is_the_whole_answer(client, temp_data_dir):
    """The end-to-end shape of a one-tap answer, which is what the submitter
    plumbing in app.js exists to keep working: `choice` alone, no typed text."""
    p = db.create_project("Portal", description="x", stage="active", slug="portal")
    q = db.file_question(p["id"], "Merge it?", quick_options=json.dumps(["merge it", "keep both"])).row

    client.post(
        f"/questions/{q['id']}/answer",
        data={"choice": "merge it", "answer": "", "next": "/questions"},
        follow_redirects=False,
    )

    assert db.get_question(q["id"])["answer"] == "merge it"


# --------------------------------------------------------------------------
# The note box, sent without leaving the page
#
# Wes, 2026-08-28: "When I click add note (and maybe other things now on the
# project page), it reloads the page now and puts me back at the top of the
# page. This is unacceptable - the tool should be seamless and should not throw
# the user's view around when they are on the page."
#
# The note form is the first in-place form carrying FILES and the first that
# has to be emptied afterwards, and both of those are decisions rather than
# strings, so they are driven under bun rather than matched in the source.
# --------------------------------------------------------------------------

def test_sending_a_note_posts_in_place_and_never_navigates(ran):
    sent = ran["noteSentInPlace"]

    assert sent["prevented"] is True
    assert sent["navigated"] == 0
    assert sent["patched"] == 1
    # Forced, so the patch does not sit in the queue behind some other field
    # the reader left focused elsewhere on the page.
    assert sent["forced"] == 1
    assert [p["action"] for p in sent["posted"]] == ["/project/p/note"]


def test_sending_a_note_stashes_no_scroll_position(ran):
    """The stash is the mechanism that used to put him back at the top.

    It is written by a listener registered ABOVE the in-place handler, so it
    fires before anything has prevented the submit; it has to exclude this form
    by name. An entry nothing consumes is not harmless - the next ordinary
    navigation to this page eats it and scrolls a page just opened."""
    assert ran["noteSentInPlace"]["stashed"] == []


def test_a_note_posts_as_multipart_with_no_content_type_header(ran):
    """Files only survive as a FormData, and only with the header left off.

    Flattening a FormData into urlencoded fields turns every File into the
    string "[object File]" and the upload arrives empty. Setting
    Content-Type: multipart/form-data by hand is the same failure by a
    different route: only the browser can write the `boundary` parameter that
    belongs beside it, and without one the server cannot split the body at all.
    Both failures are silent - a note that arrives with no attachment."""
    posted = ran["noteSentInPlace"]["posted"][0]

    assert posted["isFormData"] is True
    assert posted["headers"] == "absent"
    assert posted["contentType"] is None


def test_a_sent_note_leaves_the_box_empty(ran):
    """The morph will not do this, and is right not to.

    morphNode() returns early on a textarea (its live text is the user's
    typing), field `value` attributes are in preservedAttr(), and `.rec-row`
    and `.attach-row-item` are in MORPH_KEEP - all so a background patch cannot
    stomp a half-written note. After a send, every one of those protections is
    protecting stale text, and the note would sit in the box looking unsent
    beside its own copy in the journal."""
    after = ran["noteSentInPlace"]["after"]

    assert after["textarea"] == ""
    assert after["fileCount"] == 0
    assert after["shelfRowRemoved"] is True
    # The quoted passage went out with the note; left up, it rides along on the
    # NEXT note too.
    assert after["chipRemoved"] is True
    assert after["status"] == ""


def test_a_refused_note_keeps_every_word_of_it(ran):
    """The one way this feature could lose his work outright.

    A post the server refused delivered nothing, so the box still holds the
    only copy. Clearing on the way out rather than on success would throw away
    a note he had just typed, with an alert as the only trace."""
    refused = ran["noteRefusedKeepsTheText"]

    assert refused["alerted"] == 1
    assert refused["patched"] == 0
    assert refused["after"]["textarea"] == "a note I just typed"
    assert refused["after"]["fileCount"] == 1
    assert refused["after"]["shelfRowRemoved"] is False
    assert refused["after"]["chipRemoved"] is False


def test_an_in_place_form_with_no_files_still_posts_urlencoded(ran):
    """Multipart is opt-in on the form's own shape, not a change to all of them."""
    plain = ran["plainFormStillUrlencoded"]

    assert plain["isFormData"] is False
    assert plain["contentType"] == "application/x-www-form-urlencoded"
    assert plain["body"] == "done=1"


def test_which_note_button_was_pressed_rides_along(ran):
    """`then` decides whether a run starts, so losing it is not cosmetic.

    A FormData built from the form alone does not carry the submitter - only
    the browser's own submission does. Dropped, "queue note" arrives as a bare
    note and note_runs_now starts the agent he had just asked it not to."""
    assert ran["submitterRidesAlong"]["then"] == "queue"
    # set(), not append(): the form renders a `then` of its own, and a second
    # one leaves the server reading whichever it happens to see first.
    assert ran["submitterRidesAlong"]["thenCount"] == 1


def test_clearing_is_the_compose_markers_doing_and_nothing_elses(ran):
    """Without [data-compose] nothing is emptied.

    The settings page is full of in-place forms whose fields are meant to
    survive the post that saves them - emptying every in-place form's fields
    would blank them all."""
    kept = ran["withoutComposeMarkerNothingIsCleared"]

    assert kept["textarea"] == "a note I just typed"
    assert kept["fileCount"] == 1
    assert kept["shelfRowRemoved"] is False
    assert kept["chipRemoved"] is False


def test_the_note_form_actually_carries_both_markers():
    """The wiring, which no amount of driving app.js can see.

    Every test above proves what a [data-inplace][data-compose] form does. This
    is the one that proves the note box IS one - the whole feature is a
    two-attribute edit to a template, and losing it would leave the machinery
    correct and unreachable."""
    html = PROJECT_HTML.read_text(encoding="utf-8")
    form = re.search(r'<form[^>]*class="note-form"[^>]*>', html, re.S)
    assert form, "the note form is not in project.html any more"

    assert "data-inplace" in form.group(0)
    assert "data-compose" in form.group(0)
    # Both of these are what isMultipartForm() reads, and they are also the
    # no-script path: with scripting off the browser's own multipart submit is
    # what carries a dropped file.
    assert "multipart/form-data" in form.group(0)
    assert 'name="files"' in html


def test_a_declared_multipart_form_posts_multipart_with_no_file_in_it(ran):
    """The enctype half of isMultipartForm(), on its own.

    Two markers decide whether a form posts multipart - `enctype` and the
    presence of an <input type=file> - and the note form carries both, so a
    fixture that has both cannot tell which one is doing the work. A mutation
    sweep deleting the enctype check changed no behavior whatsoever, because
    the file input was quietly answering for it.

    The declaration has to be the one that wins: a form saying
    multipart/form-data is a form whose ROUTE was written to parse multipart,
    and that is true whether or not a file happens to be attached when it is
    submitted. Posting it urlencoded because the picker was empty would send a
    shape the route cannot read."""
    declared = ran["enctypeAloneIsEnough"]

    assert declared["isFormData"] is True
    assert declared["headers"] == "absent"


def test_the_other_forms_he_presses_most_are_in_place_too():
    """Wes, 2026-08-28: "When I click add note (and maybe other things now on
    the project page), it reloads the page now and puts me back at the top."

    "And maybe other things" is the half of that note the note box alone does
    not answer. These four are the ones pressed from partway down a long page,
    where a navigation is most jarring: adding a todo (which you do several of
    in a row), asking a question, starting a run and stopping one.

    Asserted per form rather than as "no form navigates", because plenty of
    them still should - deleting a project genuinely goes somewhere else, and
    the settings-style selects submit through `this.form.submit()`, which does
    not fire a submit event at all and so cannot be made in-place by an
    attribute."""
    html = PROJECT_HTML.read_text(encoding="utf-8")

    def form_tag(action: str) -> str:
        m = re.search(r'<form[^>]*action="' + re.escape(action) + r'"[^>]*>', html, re.S)
        assert m, f"no form posting to {action} any more"
        return m.group(0)

    slug = "{{ project.slug }}"
    # Compose boxes: emptied after the post, like the note box.
    for action in (f"/project/{slug}/todo", f"/project/{slug}/ask"):
        tag = form_tag(action)
        assert "data-inplace" in tag, action
        assert "data-compose" in tag, action

    # Buttons: nothing to empty, so no data-compose.
    for action in (f"/project/{slug}/run", "/run/{{ active_run.run_id }}/cancel"):
        tag = form_tag(action)
        assert "data-inplace" in tag, action
        assert "data-compose" not in tag, action


def test_deleting_a_project_still_navigates():
    """The one form on this page that MUST keep navigating.

    It destroys the page it is on. Patched in place it would refetch a project
    that no longer exists and paint a 404 into the middle of the layout."""
    html = PROJECT_HTML.read_text(encoding="utf-8")
    m = re.search(r'<form[^>]*action="/project/\{\{ project\.slug \}\}/delete"[^>]*>', html, re.S)

    assert m, "the delete form is gone"
    assert "data-inplace" not in m.group(0)
