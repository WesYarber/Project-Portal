"""A run whose report was recovered from the CLI transcript also gets its cost,
its token counts and its undo button (app/pricing.py, transcript usage
accounting, runs.ws_head_start, worker._note_recovered_run)."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import agent_runner, climemory, config, db, pricing, revert, runlimit, transcript, worker


@pytest.fixture(autouse=True)
def _clean_worker_state():
    worker._inflight.clear()
    worker._lease_free_since.clear()
    yield
    worker._inflight.clear()
    worker._lease_free_since.clear()


@pytest.fixture
def project():
    return db.create_project("Alpha", stage="active", build_approved=True, slug="alpha")


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def workspace():
    """The project's workspace as a real git repo with one commit."""
    ws = config.PROJECTS_DIR / "alpha"
    ws.mkdir(parents=True, exist_ok=True)
    git(ws, "init", "-q", "-b", "main")
    git(ws, "config", "user.email", "t@t")
    git(ws, "config", "user.name", "T")
    (ws / "app.txt").write_text("original\n")
    git(ws, "add", "-A")
    git(ws, "commit", "-qm", "base")
    return ws


def commit(ws: Path, name: str, text: str, message: str) -> str:
    (ws / name).write_text(text)
    git(ws, "add", "-A")
    git(ws, "commit", "-qm", message)
    return git(ws, "rev-parse", "HEAD")


REPORT = {
    "summary": ["the thing was built"],
    "journal_entry_md": "## Built\n\nDone.",
    "new_stage": None, "request_build": False, "blocked_on": None, "kind": None,
    "title": None, "description": None, "questions": [],
    "todo_updates": {"add": [], "done": [], "tags": {}},
    "preview_url": None, "learnings": [], "suggestion": None,
}

# Run 1444's counts, as the CLI recorded them: every cache write was a 1-hour
# entry. The CLI priced the run at $6.185; list prices give $6.155.
RUN_1444 = {
    "input_tokens": 2246, "output_tokens": 45381,
    "cache_read_input_tokens": 4325772, "cache_creation_input_tokens": 139123,
    "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 139123},
}
FABLE = "claude-fable-5-1"


def usage(inp=0, out=0, read=0, write=0, split=True, w5=None, w1=None):
    """A CLI usage record. With `split`, the cache writes are all 1-hour
    entries unless the lifetimes are given."""
    u = {
        "input_tokens": inp, "output_tokens": out,
        "cache_read_input_tokens": read, "cache_creation_input_tokens": write,
    }
    if split:
        u["cache_creation"] = {
            "ephemeral_5m_input_tokens": w5 if w5 is not None else 0,
            "ephemeral_1h_input_tokens": w1 if w1 is not None else write,
        }
    return u


def _stamp(base: datetime, seconds: float) -> str:
    return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def assistant(session, base, seconds, msg_id, block, *, model=FABLE, usage=None, sidechain=False):
    event = {
        "type": "assistant", "sessionId": session, "timestamp": _stamp(base, seconds),
        "message": {"id": msg_id, "role": "assistant", "model": model, "content": [block]},
    }
    if usage is not None:
        event["message"]["usage"] = usage
    if sidechain:
        event["isSidechain"] = True
    return event


def report_block(report=REPORT):
    return {"type": "tool_use", "id": "t9", "name": "StructuredOutput", "input": report}


def write_transcript(cwd: Path, session: str, events) -> Path:
    directory = config.cli_projects_dir() / climemory.encode_cwd(cwd)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")
    return path


def default_events(session, base, *, report=True, model=FABLE):
    """Two turns, the first split across two lines the way the CLI writes a
    message with two content blocks, each line repeating the same usage."""
    u1 = usage(inp=10, out=100, read=1000, write=200)
    u2 = usage(inp=5, out=50, read=2000, write=0)
    events = [
        {"type": "user", "sessionId": session, "timestamp": _stamp(base, 2),
         "message": {"role": "user", "content": "Task: BUILD."}},
        assistant(session, base, 30, "msg_1", {"type": "text", "text": "Starting."},
                  model=model, usage=u1),
        assistant(session, base, 31, "msg_1",
                  {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
                  model=model, usage=u1),
        assistant(session, base, 500, "msg_2", {"type": "text", "text": "All done."},
                  model=model, usage=u2),
    ]
    if report:
        events.append(assistant(session, base, 501, "msg_2", report_block(), model=model, usage=u2))
    return events


def adopted_run(project_id, workspace, session=None) -> int:
    run_id = db.create_run(project_id, "build", FABLE)
    db.set_run_scope(run_id, f"portal-run-{run_id}-{os.getpid() + 1}-1.scope")
    if workspace is not None:
        db.set_run_lease(run_id, str(workspace))
    if session:
        db.set_run_session(run_id, session)
    return run_id


def started(run_id) -> datetime:
    return datetime.fromisoformat(db.get_run(run_id)["started_at"])


def scope_gone(monkeypatch):
    monkeypatch.setattr(runlimit, "scope_is_active", lambda unit: False)


def last_status(project_id) -> str:
    row = db.get_conn().execute(
        "SELECT content_md FROM journal WHERE project_id = ? AND kind = 'status' "
        "ORDER BY id DESC LIMIT 1", (project_id,),
    ).fetchone()
    return row["content_md"] if row else ""


# --- the price table ---------------------------------------------------------


def test_fable_5_1_prices_a_real_run_within_a_percent_of_the_cli():
    totals = pricing.totals_from_usage(RUN_1444)
    est = pricing.price(FABLE, totals)
    assert est == pytest.approx(6.155413, abs=1e-6)
    assert abs(est - 6.185085) / 6.185085 < 0.01


def test_cache_reads_on_fable_5_1_are_a_flat_quarter_dollar():
    # 1M cache-read tokens and nothing else.
    assert pricing.price(FABLE, {"cache_read": 1_000_000}) == pytest.approx(0.25)
    # The rest of the family reads at a tenth of input.
    assert pricing.price("claude-opus-5", {"cache_read": 1_000_000}) == pytest.approx(0.5)


def test_cache_writes_are_priced_by_lifetime():
    five = pricing.price("claude-sonnet-5", {"cache_write": 1_000_000, "cache_write_5m": 1_000_000})
    hour = pricing.price("claude-sonnet-5", {"cache_write": 1_000_000, "cache_write_1h": 1_000_000})
    assert five == pytest.approx(2.5)
    assert hour == pytest.approx(4.0)


def test_writes_without_a_recorded_lifetime_price_as_five_minute_entries():
    unsplit = pricing.price("claude-sonnet-5", {"cache_write": 1_000_000})
    assert unsplit == pytest.approx(2.5)


def test_input_and_output_at_list_price():
    assert pricing.price("claude-haiku-4-5", {"input": 1_000_000}) == pytest.approx(1.0)
    assert pricing.price("claude-haiku-4-5", {"output": 1_000_000}) == pytest.approx(5.0)


def test_an_unknown_model_that_spent_tokens_is_not_priced():
    assert pricing.price("claude-someday-9", {"output": 10}) is None
    assert pricing.rates_for("opus") is None
    # A family is a prefix up to a dash, not a prefix of characters: sonnet-50
    # is not sonnet-5, and a run on it stays unpriced rather than mispriced.
    assert pricing.rates_for("claude-sonnet-50") is None
    assert pricing.rates_for(None) is None


def test_a_model_that_spent_nothing_costs_nothing_whatever_its_name():
    assert pricing.price("<synthetic>", {"input": 0, "output": 0}) == 0.0


def test_a_dated_id_prices_as_its_family_by_the_longest_match():
    assert pricing.rates_for("claude-opus-4-6-20260212") is pricing.RATES["claude-opus-4-6"]
    assert pricing.rates_for("claude-opus-4-20250514") is pricing.RATES["claude-opus-4"]
    assert pricing.rates_for("CLAUDE-SONNET-5") is pricing.RATES["claude-sonnet-5"]


def test_an_estimate_sums_the_models_and_is_none_if_any_is_unpriced():
    both = {FABLE: {"output": 1_000_000}, "claude-haiku-4-5": {"output": 1_000_000}}
    assert pricing.estimate(both) == pytest.approx(55.0)
    with_unknown = dict(both, **{"claude-someday-9": {"output": 1}})
    assert pricing.estimate(with_unknown) is None
    assert pricing.estimate({}) == 0.0


def test_totals_from_a_usage_record_read_missing_fields_as_zero():
    assert pricing.totals_from_usage(None) == {k: 0 for k in pricing.USAGE_KEYS}
    t = pricing.totals_from_usage({"input_tokens": 3, "cache_creation": "not a dict"})
    assert t["input"] == 3 and t["cache_write_5m"] == 0


# --- the reader --------------------------------------------------------------


def test_usage_is_counted_once_per_message_however_many_lines_it_spans(tmp_path):
    base = datetime(2026, 9, 2, 17, 0)
    path = write_transcript(tmp_path, "s1", default_events("s1", base))
    rec = transcript.read(path)
    assert rec.turns == 2
    assert rec.totals() == {
        "input": 15, "output": 150, "cache_read": 3000, "cache_write": 200,
        "cache_write_5m": 0, "cache_write_1h": 200,
    }


def test_a_subagents_tokens_count_toward_the_cost_but_not_the_turns(tmp_path):
    base = datetime(2026, 9, 2, 17, 0)
    events = default_events("s2", base)
    events.insert(3, assistant("s2", base, 100, "msg_side", {"type": "text", "text": "sub"},
                               usage=usage(out=1_000_000), sidechain=True))
    rec = transcript.read(write_transcript(tmp_path, "s2", events))
    assert rec.turns == 2
    assert rec.totals()["output"] == 1_000_150
    assert rec.cost() == pytest.approx(pricing.price(FABLE, rec.totals()))


def test_usage_is_kept_per_model(tmp_path):
    base = datetime(2026, 9, 2, 17, 0)
    events = default_events("s3", base)
    events.append(assistant("s3", base, 600, "msg_3", {"type": "text", "text": "and"},
                            model="claude-haiku-4-5", usage=usage(out=7)))
    rec = transcript.read(write_transcript(tmp_path, "s3", events))
    assert set(rec.usage_by_model) == {FABLE, "claude-haiku-4-5"}
    assert rec.usage_by_model["claude-haiku-4-5"]["output"] == 7
    assert rec.totals()["output"] == 157
    assert rec.cost() == pytest.approx(
        pricing.price(FABLE, rec.usage_by_model[FABLE])
        + pricing.price("claude-haiku-4-5", {"output": 7})
    )


def test_a_message_without_usage_or_without_billable_tokens_adds_no_model(tmp_path):
    base = datetime(2026, 9, 2, 17, 0)
    events = default_events("s4", base)
    events.append(assistant("s4", base, 600, "msg_syn", {"type": "text", "text": "…"},
                            model="<synthetic>", usage=usage()))
    events.append(assistant("s4", base, 601, "msg_nou", {"type": "text", "text": "…"},
                            model="claude-someday-9"))
    rec = transcript.read(write_transcript(tmp_path, "s4", events))
    assert set(rec.usage_by_model) == {FABLE}
    assert rec.cost() is not None


def test_a_message_without_an_id_is_counted_every_time(tmp_path):
    base = datetime(2026, 9, 2, 17, 0)
    events = default_events("s5", base)
    for n in range(2):
        events.append(assistant("s5", base, 700 + n, None, {"type": "text", "text": "x"},
                                usage=usage(out=1)))
    rec = transcript.read(write_transcript(tmp_path, "s5", events))
    assert rec.turns == 4
    assert rec.totals()["output"] == 152


def test_the_cost_is_none_when_a_model_that_spent_tokens_is_unpriced(tmp_path):
    base = datetime(2026, 9, 2, 17, 0)
    rec = transcript.read(write_transcript(
        tmp_path, "s6", default_events("s6", base, model="claude-someday-9")))
    assert rec.cost() is None
    assert rec.totals()["output"] == 150


# --- the spawn writes down where the repo stood ---------------------------------


def test_a_watched_run_records_the_start_head_before_the_agent_runs(
    project, workspace, monkeypatch
):
    seen = {}

    async def fake_claude(prompt, cwd, model, timeout_min, **kwargs):
        seen["start_head"] = db.get_run(kwargs["run_id"])["ws_head_start"]
        commit(Path(cwd), "made.txt", "x\n", "the run's commit")
        return agent_runner.RunResult(ok=True, result_text="", report=None)

    monkeypatch.setattr(agent_runner, "run_claude", fake_claude)
    monkeypatch.setattr(worker, "_sync_skills", lambda ws: None)
    before = git(workspace, "rev-parse", "HEAD")
    run_id = db.create_run(project["id"], "build", FABLE)

    asyncio.run(worker.run_project_task(project, "build", run_id=run_id, model=FABLE))

    assert seen["start_head"] == before
    row = db.get_run(run_id)
    assert row["ws_head_start"] == before
    assert row["ws_head_before"] == before


def test_no_start_head_is_written_for_a_workspace_that_is_not_a_repo(project):
    run_id = db.create_run(project["id"], "build", FABLE)
    db.set_run_start_head(run_id, None)
    assert db.get_run(run_id)["ws_head_start"] is None
    db.set_run_start_head(run_id, "abc123")
    assert db.get_run(run_id)["ws_head_start"] == "abc123"
    # And a later empty reading does not blank a recorded one.
    db.set_run_start_head(run_id, "")
    assert db.get_run(run_id)["ws_head_start"] == "abc123"


# --- the settle ------------------------------------------------------------------


def test_a_recovered_run_carries_its_cost_and_token_counts(project, workspace, monkeypatch):
    run_id = adopted_run(project["id"], workspace)
    write_transcript(workspace, "sess-a", default_events("sess-a", started(run_id)))
    scope_gone(monkeypatch)

    worker._reap_adopted()

    row = db.get_run(run_id)
    assert row["status"] == "ok"
    expected = pricing.price(FABLE, {
        "input": 15, "output": 150, "cache_read": 3000, "cache_write": 200,
        "cache_write_5m": 0, "cache_write_1h": 200,
    })
    assert row["cost_usd"] == pytest.approx(expected)
    assert (row["input_tokens"], row["output_tokens"], row["cache_write_tokens"],
            row["cache_read_tokens"]) == (15, 150, 200, 3000)
    assert last_status(project["id"]) == worker.RECOVERED_NOTE


def test_a_recovered_run_on_an_unpriced_model_says_why_its_cost_is_blank(
    project, workspace, monkeypatch
):
    run_id = adopted_run(project["id"], workspace)
    write_transcript(workspace, "sess-b",
                     default_events("sess-b", started(run_id), model="claude-someday-9"))
    scope_gone(monkeypatch)

    worker._reap_adopted()

    row = db.get_run(run_id)
    assert row["status"] == "ok"
    assert row["cost_usd"] is None
    assert row["output_tokens"] == 150
    assert last_status(project["id"]) == worker.RECOVERED_NOTE + " " + worker.UNPRICED_NOTE


def test_a_recovered_run_gets_its_undo_button_from_the_start_head(
    project, workspace, monkeypatch
):
    """The spawn wrote the start head; the agent committed; the portal
    restarted; the settle pairs that start with the repo's HEAD now."""
    run_id = adopted_run(project["id"], workspace)
    before = git(workspace, "rev-parse", "HEAD")
    db.set_run_start_head(run_id, before)
    after = commit(workspace, "made.txt", "x\n", "what the lost run built")
    write_transcript(workspace, "sess-c", default_events("sess-c", started(run_id)))
    scope_gone(monkeypatch)

    worker._reap_adopted()

    row = db.get_run_with_project(run_id)
    assert (row["ws_head_before"], row["ws_head_after"]) == (before, after)
    plan = revert.landed(row)
    assert plan is not None
    assert [c.subject for c in plan.commits] == ["what the lost run built"]


def test_a_recovered_run_that_committed_nothing_has_no_undo_button(
    project, workspace, monkeypatch
):
    run_id = adopted_run(project["id"], workspace)
    db.set_run_start_head(run_id, git(workspace, "rev-parse", "HEAD"))
    write_transcript(workspace, "sess-d", default_events("sess-d", started(run_id)))
    scope_gone(monkeypatch)

    worker._reap_adopted()

    row = db.get_run_with_project(run_id)
    assert row["ws_head_before"] is None and row["ws_head_after"] is None
    assert revert.landed(row) is None


def test_a_run_from_before_the_start_head_keeps_no_undo_button(project, workspace, monkeypatch):
    """No start head on the row: the range cannot be reconstructed, so
    neither head is written, however much the repo moved."""
    run_id = adopted_run(project["id"], workspace)
    commit(workspace, "made.txt", "x\n", "somebody's commit")
    write_transcript(workspace, "sess-e", default_events("sess-e", started(run_id)))
    scope_gone(monkeypatch)

    worker._reap_adopted()

    row = db.get_run_with_project(run_id)
    assert row["ws_head_before"] is None and row["ws_head_after"] is None
    assert revert.landed(row) is None


def test_a_recovered_run_without_a_report_still_gets_its_cost_and_heads(
    project, workspace, monkeypatch
):
    """An agent killed mid-work spent tokens and may have committed; the row
    says error, but what it cost and what it changed are still recorded."""
    run_id = adopted_run(project["id"], workspace)
    before = git(workspace, "rev-parse", "HEAD")
    db.set_run_start_head(run_id, before)
    after = commit(workspace, "half.txt", "x\n", "half done")
    write_transcript(workspace, "sess-f",
                     default_events("sess-f", started(run_id), report=False))
    scope_gone(monkeypatch)

    worker._reap_adopted()

    row = db.get_run_with_project(run_id)
    assert row["status"] == "error"
    assert row["cost_usd"] is not None and row["cost_usd"] > 0
    assert row["output_tokens"] == 150
    assert (row["ws_head_before"], row["ws_head_after"]) == (before, after)


def test_recording_usage_that_raises_does_not_lose_the_settle(project, workspace, monkeypatch):
    run_id = adopted_run(project["id"], workspace)
    write_transcript(workspace, "sess-g", default_events("sess-g", started(run_id)))
    scope_gone(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "record_run_usage", boom)
    worker._reap_adopted()

    row = db.get_run(run_id)
    assert row["status"] == "ok"
    assert row["cost_usd"] is not None


def test_a_recovered_one_off_reply_carries_its_cost_too(monkeypatch):
    from app import oneoff

    task_id = int(db.create_oneoff("what is the time")["id"])
    cwd = oneoff.workspace(task_id)
    cwd.mkdir(parents=True, exist_ok=True)
    run_id = db.create_run(None, "what is the time", FABLE, oneoff_id=task_id)
    db.set_run_scope(run_id, f"portal-run-{run_id}-{os.getpid() + 1}-1.scope")
    db.set_run_lease(run_id, str(cwd))
    write_transcript(cwd, "sess-h", default_events("sess-h", started(run_id), report=False))
    scope_gone(monkeypatch)

    worker._reap_adopted()

    row = db.get_run(run_id)
    assert row["status"] == "ok"
    assert row["cost_usd"] == pytest.approx(pricing.price(FABLE, {
        "input": 15, "output": 150, "cache_read": 3000, "cache_write": 200,
        "cache_write_1h": 200,
    }))
    assert row["output_tokens"] == 150


# --- the figure says it is an estimate ----------------------------------------
# `finish_run` writes a recovered run's list-price estimate into the same
# column the CLI's own figure goes in. `runs.cost_source` says which it is, and
# the run page and the activity table mark an estimate with a tilde.

from starlette.testclient import TestClient  # noqa: E402

from app import usage as usage_mod  # noqa: E402


@pytest.fixture
def client():
    from app import main

    return TestClient(main.app)


def test_a_recovered_runs_figure_is_marked_as_an_estimate(project, workspace, monkeypatch):
    run_id = adopted_run(project["id"], workspace)
    write_transcript(workspace, "sess-m", default_events("sess-m", started(run_id)))
    scope_gone(monkeypatch)
    worker._reap_adopted()
    row = db.get_run(run_id)
    assert row["cost_source"] == db.COST_SOURCE_TRANSCRIPT
    assert db.cost_is_estimated(row)


def test_an_unpriced_recovered_run_is_not_marked(project, workspace, monkeypatch):
    run_id = adopted_run(project["id"], workspace)
    write_transcript(workspace, "sess-n",
                     default_events("sess-n", started(run_id), model="claude-someday-9"))
    scope_gone(monkeypatch)
    worker._reap_adopted()
    row = db.get_run(run_id)
    assert row["cost_usd"] is None and row["cost_source"] is None
    assert not db.cost_is_estimated(row)


def test_a_recovered_run_without_a_report_is_marked_too(project, workspace, monkeypatch):
    run_id = adopted_run(project["id"], workspace)
    write_transcript(workspace, "sess-o",
                     default_events("sess-o", started(run_id), report=False))
    scope_gone(monkeypatch)
    worker._reap_adopted()
    row = db.get_run(run_id)
    assert row["status"] == "error" and row["cost_usd"] is not None
    assert db.cost_is_estimated(row)


def test_a_watched_runs_figure_is_not_marked(project):
    run_id = db.create_run(project["id"], "build", FABLE)
    db.record_run_usage(run_id, input_tokens=10, output_tokens=20,
                        cache_write_tokens=0, cache_read_tokens=0)
    db.finish_run(run_id, "ok", "sess-w", 1.25, 3, "done")
    row = db.get_run(run_id)
    assert row["cost_usd"] == 1.25 and row["cost_source"] is None
    assert not db.cost_is_estimated(row)


def test_a_row_without_the_column_is_not_an_estimate():
    assert not db.cost_is_estimated({"cost_usd": 1.0})


def test_the_figure_is_rendered_with_a_tilde_and_labeled_as_an_estimate():
    estimated = {"cost_usd": 6.9538, "cost_estimated": True}
    measured = {"cost_usd": 6.9538, "cost_estimated": False}
    blank = {"cost_usd": None, "cost_estimated": True}
    assert usage_mod.format_run_cost(estimated, units="weight") == "~6.954w"
    assert usage_mod.format_run_cost(estimated, units="usd") == "~$6.954"
    assert usage_mod.format_run_cost(measured, units="weight") == "6.954w"
    assert usage_mod.format_run_cost(blank, units="weight") == "-"
    assert usage_mod.cost_label(estimated, units="weight") == "estimated weight"
    assert usage_mod.cost_label(estimated, units="usd") == "estimated cost"
    assert usage_mod.cost_label(measured, units="weight") == "weight"
    assert usage_mod.cost_label(blank, units="weight") == "weight"


def test_the_run_page_and_the_activity_table_mark_a_recovered_runs_figure(
    project, workspace, monkeypatch, client
):
    run_id = adopted_run(project["id"], workspace)
    write_transcript(workspace, "sess-p", default_events("sess-p", started(run_id)))
    scope_gone(monkeypatch)
    worker._reap_adopted()
    figure = usage_mod.format_cost(db.get_run(run_id)["cost_usd"])

    page = client.get(f"/run/{run_id}").text
    assert f"<em>~{figure}</em>estimated weight" in page
    assert usage_mod.ESTIMATE_TITLE in page

    table = client.get("/activity").text
    assert f">~{figure}</td>" in table
    assert usage_mod.ESTIMATE_TITLE in table


def test_the_run_page_shows_a_watched_runs_figure_plain(project, client):
    run_id = db.create_run(project["id"], "build", FABLE)
    db.finish_run(run_id, "ok", "sess-q", 1.25, 3, "done")
    page = client.get(f"/run/{run_id}").text
    assert "<em>1.250w</em>weight" in page
    assert "~1.250w" not in page and usage_mod.ESTIMATE_TITLE not in page
