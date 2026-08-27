"""The ask conversation: one place, and a memory of its own.

Wes, 2026-08-16:

  "For 'Asking' the project stuff, it should maintain the context of previously
  asked questions. I also want the questions to be asked and answered all up at
  the 'Ask' area instead of in line in the journal."

Two halves, and they are separate mechanisms:

**Where it is shown.** A question and its answer were journal entries like any
other, so a conversation arrived interleaved with agent progress reports - ask
on Tuesday, three runs, answer, two more runs. `db.ask_thread` reads the same
rows back as one thread and the project page draws it inside the ask box; the
journal excludes them in SQL. Nothing is deleted or moved: they are still
journal rows, still the permanent record, still skipped by a run's prompt.

**What the model sees.** `ask.build_prompt` used to get the thread only by
accident - it read the last twenty journal entries *without* excluding the side
thread, so on a busy project the question asked yesterday had already been
pushed out by run reports and a follow-up was answered as if nothing had been
said before. The thread is now its own block, whole, under its own byte budget.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from app import ask, db


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


@pytest.fixture
def project(temp_data_dir):
    return db.create_project("Fridge Board", slug="fridge", stage="active")


@pytest.fixture(autouse=True)
def _clear_pending():
    ask._PENDING.clear()  # noqa: SLF001
    yield
    ask._PENDING.clear()  # noqa: SLF001


def _exchange(project_id, question="why that panel?", answer="Because it reads at 2m."):
    db.add_journal(project_id, "user", "ask", question)
    db.add_journal(project_id, "agent", "answer", answer)


# --- reading the thread back -------------------------------------------------

def test_the_thread_is_the_asks_and_their_answers_and_nothing_else(project):
    db.add_journal(project["id"], "agent", "progress", "shipped the thing")
    db.add_journal(project["id"], "user", "note", "do the other thing")
    # Wes answering one of the PORTAL's questions is user/answer, which is an
    # instruction rather than an ask - the pair is matched, not the kind.
    db.add_journal(project["id"], "user", "answer", "yes, merge it")
    _exchange(project["id"])

    rows = db.ask_thread(project["id"])

    assert [(r["author"], r["kind"]) for r in rows] == [("user", "ask"), ("agent", "answer")]


def test_the_thread_reads_oldest_first(project):
    _exchange(project["id"], "first question", "first answer")
    _exchange(project["id"], "second question", "second answer")

    bodies = [r["content_md"] for r in db.ask_thread(project["id"])]

    assert bodies == ["first question", "first answer", "second question", "second answer"]


def test_a_long_thread_loses_its_beginning_not_its_newest_exchange(project):
    for i in range(12):
        _exchange(project["id"], f"question {i}", f"answer {i}")

    bodies = [r["content_md"] for r in db.ask_thread(project["id"], limit=4)]

    assert bodies == ["question 10", "answer 10", "question 11", "answer 11"]


def test_a_project_with_no_asks_has_no_thread(project):
    db.add_journal(project["id"], "agent", "progress", "shipped the thing")

    assert db.ask_thread(project["id"]) == []


def test_one_projects_thread_is_not_anothers(project, temp_data_dir):
    other = db.create_project("Other", slug="other", stage="active")
    _exchange(project["id"], "mine", "mine answered")
    _exchange(other["id"], "theirs", "theirs answered")

    assert [r["content_md"] for r in db.ask_thread(other["id"])] == ["theirs", "theirs answered"]


# --- the journal it no longer runs through -----------------------------------

def test_the_journal_feed_excludes_the_side_thread(project):
    db.add_journal(project["id"], "agent", "progress", "shipped the thing")
    _exchange(project["id"])

    kinds = [r["kind"] for r in db.list_journal(project["id"], exclude=db.SIDE_THREAD)]

    assert kinds == ["progress"]


def test_the_exclusion_happens_before_the_limit(project):
    """Filtering after the fetch would let a chatty thread eat the slots of the
    journal it is no longer shown in - the same rule list_journal_asc follows."""
    for i in range(5):
        _exchange(project["id"], f"question {i}", f"answer {i}")
    db.add_journal(project["id"], "agent", "progress", "the report he wants to read")

    rows = db.list_journal(project["id"], limit=3, exclude=db.SIDE_THREAD)

    assert [r["content_md"] for r in rows] == ["the report he wants to read"]


def test_excluding_nothing_still_returns_everything(project):
    _exchange(project["id"])

    assert len(db.list_journal(project["id"])) == 2


def test_the_board_wide_feed_can_exclude_too(project):
    db.add_journal(None, "system", "status", "the service restarted")
    _exchange(project["id"])
    db.add_journal(project["id"], "agent", "progress", "shipped the thing")

    rows = db.list_journal(
        limit=50, only_projects={project["id"]}, exclude=db.SIDE_THREAD
    )

    assert [r["kind"] for r in rows] == ["progress", "status"]


def test_the_exclusion_reaches_the_rows_that_belong_to_no_project(project):
    """The scope clause is an OR - `project_id IS NULL OR project_id IN (...)`
    - so a bracket dropped from around it lets AND bind to its right half
    alone, and every install-wide row sails past the exclusions. Asked with a
    pair that a project-less row really does carry, because nothing without a
    project is ever an ask."""
    db.add_journal(None, "system", "status", "the service restarted")
    db.add_journal(project["id"], "agent", "progress", "shipped the thing")

    rows = db.list_journal(
        limit=50, only_projects={project["id"]}, exclude=(("system", "status"),)
    )

    assert [r["kind"] for r in rows] == ["progress"]


def test_a_scoped_feed_still_drops_other_peoples_projects(project, temp_data_dir):
    other = db.create_project("Other", slug="other", stage="active")
    db.add_journal(other["id"], "agent", "progress", "not yours")
    db.add_journal(project["id"], "agent", "progress", "yours")

    rows = db.list_journal(limit=50, only_projects={project["id"]}, exclude=db.SIDE_THREAD)

    assert [r["content_md"] for r in rows] == ["yours"]


# --- what the model is told --------------------------------------------------

def test_the_prompt_carries_the_whole_thread_however_busy_the_project_is(project):
    """The bug this replaces: the thread rode in on the journal's last-20
    window, so twenty run reports - about two days here - pushed yesterday's
    question out and a follow-up was answered cold."""
    _exchange(project["id"], "WHY-THAT-PANEL", "BECAUSE-IT-READS-AT-2M")
    for i in range(25):
        db.add_journal(project["id"], "agent", "progress", f"report {i}")

    prompt = ask.build_prompt(db.get_project(project["id"]), "and the other one?")

    assert "WHY-THAT-PANEL" in prompt
    assert "BECAUSE-IT-READS-AT-2M" in prompt


def test_the_journal_half_of_the_prompt_drops_the_thread(project):
    """Otherwise the exchange arrives twice, once in each block - and the
    journal tail's slots go to a conversation that has its own section."""
    _exchange(project["id"], "WHY-THAT-PANEL", "BECAUSE-IT-READS-AT-2M")

    prompt = ask.build_prompt(db.get_project(project["id"]), "and the other one?")
    journal_block = prompt.split("## Recent journal")[1].split("## ")[0]

    assert "WHY-THAT-PANEL" not in journal_block


def test_the_question_being_asked_is_not_also_in_the_thread(project):
    """`ask.start` journals the question before the answer runs, so by the time
    the prompt is built the newest thread entry IS this question. It is asked
    once, at the end, under its own heading."""
    db.add_journal(project["id"], "user", "ask", "THE-LIVE-QUESTION")

    prompt = ask.build_prompt(db.get_project(project["id"]), "THE-LIVE-QUESTION")

    assert prompt.count("THE-LIVE-QUESTION") == 1
    assert "This ask thread so far" not in prompt


def test_an_earlier_identical_question_still_shows_as_history(project):
    """Only the TRAILING entry is the live one. Asking the same thing twice is
    a real thing to do - "did that ever get done?" - and the first time round,
    question and answer both, is the context for the second."""
    _exchange(project["id"], "SAME-QUESTION", "the first answer")

    prompt = ask.build_prompt(db.get_project(project["id"]), "SAME-QUESTION")
    # Bounded at the next heading. Everything after this block includes the
    # live question under its own heading, so an unbounded split would find
    # SAME-QUESTION there and pass whatever the thread said.
    thread = prompt.split("## This ask thread so far")[1].split("\n## ")[0]

    assert "SAME-QUESTION" in thread
    assert "the first answer" in thread


def test_no_thread_means_no_section(project):
    prompt = ask.build_prompt(db.get_project(project["id"]), "first question here")

    assert "This ask thread so far" not in prompt


def test_the_thread_section_is_bounded(project):
    """An answer can be long. Past the budget the oldest exchanges degrade to
    their opening paragraph rather than the block growing without limit."""
    for i in range(10):
        _exchange(project["id"], f"question {i}", f"answer {i}\n\n" + ("x" * 4000))

    section = ask.thread_section(db.ask_thread(project["id"]))

    assert len(section) < ask.THREAD_BUDGET_BYTES + 2000
    assert "question 9" in section


# --- whether the box arrives open --------------------------------------------

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def _rows(minutes_ago):
    stamp = (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    return [{"ts": stamp, "author": "agent", "content_md": "an answer"}]


def test_the_box_is_open_while_a_question_is_in_flight():
    assert ask.opens([], True, NOW) is True


def test_the_box_is_open_just_after_an_answer_lands():
    """He asks, goes off, comes back to a push notification. The answer is on
    the page rather than behind a click - the whole point of his note."""
    assert ask.opens(_rows(5), False, NOW) is True


def test_the_box_folds_again_once_the_thread_is_history():
    assert ask.opens(_rows(60 * 24), False, NOW) is False


def test_the_boundary_is_the_window_itself():
    minutes = int(ask.OPEN_WITHIN.total_seconds() // 60)

    assert ask.opens(_rows(minutes), False, NOW) is True
    assert ask.opens(_rows(minutes + 1), False, NOW) is False


def test_no_thread_and_nothing_pending_means_folded():
    assert ask.opens([], False, NOW) is False


def test_an_unreadable_timestamp_counts_as_old():
    """Treating it as "now" would prise the box open on every page load
    forever, which is a thing that could never be got rid of."""
    assert ask.opens([{"ts": "sometime last week"}], False, NOW) is False


def test_a_naive_timestamp_is_read_as_utc():
    stamp = (NOW - timedelta(minutes=5)).replace(tzinfo=None).isoformat(timespec="seconds")

    assert ask.opens([{"ts": stamp}], False, NOW) is True


# --- the page ----------------------------------------------------------------

def test_the_page_draws_the_thread_in_the_ask_box(client, project):
    _exchange(project["id"], "WHY-THAT-PANEL", "BECAUSE-IT-READS-AT-2M")

    html = client.get(f"/project/{project['slug']}").text
    box = html.split('id="ask"')[1].split("</details>")[0]

    assert "WHY-THAT-PANEL" in box
    assert "BECAUSE-IT-READS-AT-2M" in box
    assert 'id="ask-thread"' in box


def test_the_thread_is_scroll_capped_so_the_form_stays_in_view(client, project):
    _exchange(project["id"])

    html = client.get(f"/project/{project['slug']}").text

    assert "scroll-cap scroll-cap-ask" in html
    assert ".scroll-cap-ask" in (client.get("/static/style.css").text)


def test_a_fresh_thread_arrives_unfolded(client, project):
    _exchange(project["id"])

    html = client.get(f"/project/{project['slug']}").text
    tag = html.split('id="ask"')[1].split(">")[0]

    assert "open" in tag


def test_an_old_thread_arrives_folded_with_a_count_on_the_button(client, project):
    _exchange(project["id"])
    db.get_conn().execute(
        "UPDATE journal SET ts = ? WHERE project_id = ?",
        ("2026-08-01T09:00:00+00:00", project["id"]),
    )

    html = client.get(f"/project/{project['slug']}").text
    box = html.split('id="ask"')[1].split("</details>")[0]

    assert "open" not in box.split(">")[0]
    assert '<span class="ask-count">2</span>' in box


def test_the_empty_box_says_nothing_about_a_thread(client, project):
    html = client.get(f"/project/{project['slug']}").text
    box = html.split('id="ask"')[1].split("</details>")[0]

    assert "ask-count" not in box
    assert 'id="ask-thread"' not in box


def test_asking_lands_you_back_at_the_ask_box(client, project, monkeypatch):
    monkeypatch.setattr(ask, "start", lambda pid, q, **kw: 1)

    resp = client.post(
        f"/project/{project['slug']}/ask",
        data={"question": "why that panel?"},
        follow_redirects=False,
    )

    assert resp.headers["location"] == f"/project/{project['slug']}#ask"


def test_the_answer_appears_in_the_box_the_question_was_asked_from(client, project, monkeypatch):
    """End to end, which is the only way to catch the two halves disagreeing
    about which rows are the thread."""
    async def canned(prompt, cwd, model):
        return "BECAUSE-IT-READS-AT-2M"

    monkeypatch.setattr(ask, "run_ask", canned)
    db.add_journal(project["id"], "agent", "progress", "shipped the thing")

    async def scenario():
        ask.start(project["id"], "WHY-THAT-PANEL")
        await asyncio.gather(*list(ask._TASKS))  # noqa: SLF001

    asyncio.run(scenario())
    box = client.get(f"/project/{project['slug']}").text.split('id="ask"')[1].split("</details>")[0]
    journal = client.get(f"/project/{project['slug']}").text.split('id="journal"')[1]

    assert "WHY-THAT-PANEL" in box and "BECAUSE-IT-READS-AT-2M" in box
    assert "BECAUSE-IT-READS-AT-2M" not in journal
