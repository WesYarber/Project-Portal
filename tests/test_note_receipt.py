"""The 303 an agent's `curl` gets back now says what happened.

On 2026-09-20 two agents (mtg-proxy-forge at 12:43Z, SimpleClickTrack at
13:15Z) each posted a one-word probe to `POST /project/<slug>/note` to check
the endpoint had heard them, because `curl -sS` printed nothing, and each then
had to file a third note apologizing for the stray. The endpoint had heard
them both: a 303 See Other has no body, so a successful post and a no-op look
identical from the other end.

The fix is a line of plain text on the redirect that was already being sent.
So the claims worth defending are: the line is TRUE in every branch (an empty
note says nothing was filed, a refused parallel run is not reported as
started, a run chained onto a transcription is not reported as queued yet),
and the browsers and `fetch` callers that were working still are - which means
the status, the Location header and the side effects must all be exactly what
they were before, and the content-length must match the body or the line never
leaves the process.
"""
from __future__ import annotations

import asyncio
import io

import pytest
from starlette.testclient import TestClient

from app import db, main, receipt, worker


@pytest.fixture
def client(temp_data_dir):
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _clean_worker_state():
    def reset():
        worker._inflight.clear()
        worker._wake = asyncio.Event()
        while not worker.manual_queue.empty():
            worker.manual_queue.get_nowait()

    reset()
    yield
    reset()


def _project(title="Proxy Forge", slug="mtg-proxy-forge", stage="active"):
    p = db.create_project(title, description="cards", slug=slug)
    db.update_project(p["id"], stage=stage)
    return db.get_project(p["id"])


def post(client, slug, **data):
    """The agent's call: curl does NOT follow redirects unless asked."""
    return client.post(f"/project/{slug}/note", data=data, follow_redirects=False)


# --------------------------------------------------------------------------
# The response itself
# --------------------------------------------------------------------------

def test_the_receipt_is_still_a_303_to_the_project_page():
    r = receipt.Receipt("/project/x", "ok: something happened.")
    assert r.status_code == 303
    assert r.headers["location"] == "/project/x"


def test_the_receipt_carries_the_line_as_its_body():
    r = receipt.Receipt("/project/x", "ok: something happened.")
    assert r.body == b"ok: something happened.\n"


def test_the_receipt_declares_the_length_it_actually_sends():
    # Starlette computes content-length in __init__, from the empty body a
    # RedirectResponse is built with. A body attached afterwards without
    # rewriting this header is sent as zero bytes and the line never arrives.
    r = receipt.Receipt("/project/x", "ok: something happened.")
    assert r.headers["content-length"] == str(len(r.body))


def test_the_receipt_is_plain_text_not_html():
    r = receipt.Receipt("/project/x", "ok: hi")
    assert r.headers["content-type"] == "text/plain; charset=utf-8"


def test_the_receipt_is_a_redirect_response():
    # Every caller in main.py is annotated `-> RedirectResponse`, and the
    # middleware stack treats a redirect specially; this must remain one.
    from starlette.responses import RedirectResponse

    assert isinstance(receipt.Receipt("/project/x", "ok"), RedirectResponse)


def test_the_line_ends_in_exactly_one_newline():
    r = receipt.Receipt("/project/x", "  ok: hi  \n\n")
    assert r.body == b"ok: hi\n"


# --------------------------------------------------------------------------
# The line: it must be true in every branch
# --------------------------------------------------------------------------

def test_a_filed_note_names_the_project():
    assert receipt.note_line("kvk", filed=True) == (
        "ok: note filed on kvk; the agent reads it on its next run."
    )


def test_an_empty_note_says_nothing_was_filed():
    line = receipt.note_line("kvk", filed=False)
    assert "nothing filed" in line
    assert "nothing was started" in line


def test_an_empty_note_that_still_started_a_run_says_both():
    line = receipt.note_line("kvk", filed=False, then="run")
    assert "nothing filed" in line
    assert "a run is queued" in line


def test_then_run_reports_the_queued_run():
    assert "a run is queued" in receipt.note_line("kvk", filed=True, then="run")


def test_a_plain_note_that_woke_a_run_says_so():
    assert "a run is queued" in receipt.note_line("kvk", filed=True, ran=True)


def test_a_plain_note_that_woke_nothing_does_not_claim_a_run():
    line = receipt.note_line("kvk", filed=True, ran=False)
    assert "queued" not in line
    assert "next run" in line


def test_a_refused_parallel_run_is_not_reported_as_started():
    line = receipt.note_line("kvk", filed=True, then="parallel", ran=False)
    assert "no parallel run started" in line


def test_an_accepted_parallel_run_is_reported_as_started():
    assert "a parallel run started" in receipt.note_line(
        "kvk", filed=True, then="parallel", ran=True
    )


def test_queue_note_says_it_waits():
    line = receipt.note_line("kvk", filed=True, then="queue")
    assert "waits for the next run" in line
    assert "queued" not in line


def test_a_mid_run_note_says_it_will_be_delivered():
    assert "delivery mid-run" in receipt.note_line("kvk", filed=True, then="hear")


def test_a_mid_run_note_that_also_woke_a_run_says_both():
    line = receipt.note_line("kvk", filed=True, then="hear", ran=True)
    assert "delivery mid-run" in line
    assert "a run is queued" in line


@pytest.mark.parametrize("then", ["run", "parallel", ""])
def test_a_voice_memo_defers_whatever_it_chained(then):
    # The run is chained onto the transcription and has NOT started yet.
    line = receipt.note_line("kvk", filed=True, then=then, transcribing=True)
    assert "once the voice memo is transcribed" in line
    assert "a parallel run started" not in line


def test_one_file_is_singular_and_two_are_plural():
    assert "with 1 file;" in receipt.note_line("kvk", filed=True, files=1)
    assert "with 2 files;" in receipt.note_line("kvk", filed=True, files=2)


def test_no_files_are_not_mentioned_at_all():
    # "note filed" contains "file", so the claim is about the count clause.
    assert "with" not in receipt.note_line("kvk", filed=True, files=0)


def test_rejected_files_are_counted_separately():
    line = receipt.note_line("kvk", filed=True, files=1, rejected=2)
    assert "with 1 file" in line
    assert "(2 files rejected)" in line


def test_nothing_rejected_is_not_mentioned():
    assert "rejected" not in receipt.note_line("kvk", filed=True, files=1)


def test_every_line_is_one_sentence_on_one_line():
    for kwargs in (
        {"filed": True},
        {"filed": False},
        {"filed": True, "then": "run"},
        {"filed": True, "then": "parallel", "ran": True},
        {"filed": True, "then": "hear", "ran": True},
        {"filed": True, "files": 3, "rejected": 1, "then": "queue"},
        {"filed": True, "transcribing": True},
    ):
        line = receipt.note_line("kvk", **kwargs)
        assert "\n" not in line
        assert line.startswith("ok: ")
        assert line.endswith(".")


# --------------------------------------------------------------------------
# Through the real route
# --------------------------------------------------------------------------

def test_a_posted_note_answers_with_a_body(client):
    p = _project()
    r = post(client, p["slug"], note="An agent on site-wide-tools filed this.")
    assert r.status_code == 303
    assert r.text.strip() == (
        "ok: note filed on mtg-proxy-forge; a run is queued."
    )


def test_the_note_still_lands_in_the_journal(client):
    p = _project()
    post(client, p["slug"], note="the real note", then="queue")
    assert [n["content_md"] for n in db.pending_notes(p["id"])] == ["the real note"]


def test_then_run_still_queues_the_run_and_says_so(client):
    p = _project(stage="review")
    r = post(client, p["slug"], note="go", then="run")
    assert list(worker.manual_queue._queue) == [p["id"]]
    assert "a run is queued" in r.text


def test_an_empty_post_answers_that_it_did_nothing(client):
    p = _project()
    r = post(client, p["slug"], note="   ")
    assert r.status_code == 303
    assert "nothing filed" in r.text
    assert "nothing was started" in r.text
    assert db.pending_notes(p["id"]) == []


def test_an_empty_post_with_run_still_runs(client):
    p = _project()
    r = post(client, p["slug"], note="", then="run")
    assert list(worker.manual_queue._queue) == [p["id"]]
    assert "a run is queued" in r.text


def test_queue_note_answers_that_it_waits(client):
    p = _project()
    r = post(client, p["slug"], note="later", then="queue")
    assert list(worker.manual_queue._queue) == []
    assert "waits for the next run" in r.text


def test_an_attachment_is_counted_in_the_line(client):
    p = _project()
    r = client.post(
        f"/project/{p['slug']}/note",
        data={"note": "see this", "then": "queue"},
        files={"files": ("shot.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 64), "image/png")},
        follow_redirects=False,
    )
    assert "with 1 file" in r.text


def test_the_browser_still_lands_on_the_project_page(client):
    # The body is invisible to anything that follows the redirect, which is
    # every caller that was already working.
    p = _project()
    r = client.post(
        f"/project/{p['slug']}/note", data={"note": "hi", "then": "queue"}
    )
    assert r.status_code == 200
    assert r.url.path == f"/project/{p['slug']}"
    assert "<!doctype html" in r.text[:200].lower() or "<html" in r.text[:400].lower()


def test_the_receipt_is_not_html_on_the_wire(client):
    p = _project()
    r = post(client, p["slug"], note="hi", then="queue")
    assert r.headers["content-type"].startswith("text/plain")
    assert int(r.headers["content-length"]) == len(r.content)


# --------------------------------------------------------------------------
# The branches whose outcome is genuinely in doubt, through the real route
# --------------------------------------------------------------------------

def test_a_refused_parallel_run_says_so_on_the_wire(client, monkeypatch):
    # start_parallel_run refuses when it cannot add a second agent, and the
    # reason goes in the journal. The line must agree with the journal.
    p = _project()

    async def refuse(project):
        return False, "not with a run already in flight"

    monkeypatch.setattr(worker, "start_parallel_run", refuse)
    r = post(client, p["slug"], note="beside the one working", then="parallel")
    assert "no parallel run started" in r.text
    assert "a parallel run started" not in r.text


def test_an_accepted_parallel_run_says_so_on_the_wire(client, monkeypatch):
    p = _project()

    async def accept(project):
        return True, ""

    monkeypatch.setattr(worker, "start_parallel_run", accept)
    r = post(client, p["slug"], note="beside the one working", then="parallel")
    assert "a parallel run started" in r.text


def test_an_empty_post_that_was_refused_a_parallel_run_says_so(client, monkeypatch):
    p = _project()

    async def refuse(project):
        return False, "no"

    monkeypatch.setattr(worker, "start_parallel_run", refuse)
    r = post(client, p["slug"], note="", then="parallel")
    assert "no parallel run started" in r.text


def test_a_voice_memo_is_not_reported_as_already_running(client, monkeypatch):
    # The run is chained onto the transcription inside transcribe.kick, so at
    # the moment this line is written nothing has started. kick is stubbed
    # because this test is about the sentence, not the job - and the
    # continuation is closed rather than dropped, or Python warns that a
    # coroutine was never awaited.
    p = _project()
    kicked: list[list[int]] = []

    def fake_kick(ids, after=None):
        kicked.append(list(ids))
        if after is not None:
            after.close()

    monkeypatch.setattr(main.transcribe, "kick", fake_kick)
    r = client.post(
        f"/project/{p['slug']}/note",
        data={"note": "listen to this", "then": "run"},
        files={"files": ("memo.m4a", io.BytesIO(b"\x00\x00\x00\x20ftypM4A " + b"0" * 64), "audio/mp4")},
        follow_redirects=False,
    )
    assert kicked, "the memo was never handed to transcription"
    assert "once the voice memo is transcribed" in r.text


def test_the_line_reports_the_button_that_was_actually_pressed(client):
    # `then` decides everything downstream, so passing the wrong one into the
    # line would describe a different press than the one that happened.
    p = _project()
    assert "waits for the next run" in post(
        client, p["slug"], note="a", then="queue"
    ).text
    assert "a run is queued" in post(client, p["slug"], note="b", then="run").text
