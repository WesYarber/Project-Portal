"""The budgets reach a real prompt, and a run's cost is written down.

`test_promptbudget.py` covers the arithmetic. This covers the wiring: that
`build_prompt` actually calls it, that the settings are honored, and that the
numbers needed to check any of this later are recorded rather than parsed and
thrown away.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config, db


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


def _project():
    """`create_project` hands back the row, not an id."""
    return db.create_project("Budget", "d", stage="active")["id"]


def _write_learnings(sections: int = 6, per: int = 40) -> None:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    text = "# Learnings\n\nDurable facts only.\n"
    for s in range(sections):
        text += f"\n## Section {s}\n\n"
        for e in range(per):
            text += f"- Section {s} entry {e}. " + "padding " * 30 + "\n"
    config.LEARNINGS_MD.write_text(text, encoding="utf-8")


def test_the_learnings_block_obeys_its_budget():
    pid = _project()
    _write_learnings()
    project = db.get_project(pid)

    db.set_setting("prompt_learnings_kb", "4")
    small = agent_runner.build_prompt("build", project)
    db.set_setting("prompt_learnings_kb", "32")
    big = agent_runner.build_prompt("build", project)

    assert len(big) > len(small)
    block = small.split("## Memory: learnings.md\n", 1)[1]
    assert len(block) < 6 * 1024


def test_the_general_sections_at_the_top_are_the_ones_that_survive():
    """The regression that started all this: the old `lines[-100:]` kept the
    END of the file, which is where the one-off domain trivia lives, and lost
    every heading above it."""
    pid = _project()
    _write_learnings()
    db.set_setting("prompt_learnings_kb", "4")
    prompt = agent_runner.build_prompt("build", db.get_project(pid))
    # Section 0 is at the top of the file, so it is the section the budget is
    # spent on - and within it the newest entries win.
    assert "## Section 0" in prompt
    assert "Section 0 entry 39." in prompt
    # The last section's content does not fit, but it is still NAMED, so an
    # agent can see it exists and go and read it rather than concluding the
    # subject was never written down.
    assert "Section 5 entry 39." not in prompt
    assert "Section 5" in prompt
    # And the trimming is announced rather than silent.
    assert "trimmed" in prompt


def test_a_journal_of_long_entries_stays_inside_its_budget():
    pid = _project()
    for i in range(20):
        db.add_journal(pid, "agent", "progress",
                       f"## Run {i}\n\nOpening summary {i}.\n\n" + ("detail " * 900))
    db.set_setting("prompt_journal_kb", "8")
    prompt = agent_runner.build_prompt("build", db.get_project(pid))
    block = prompt.split("## Recent journal", 1)[1].split("\n## ", 1)[0]
    # The newest entry is whole by design, so the bound is the budget plus it.
    assert len(block) < 8 * 1024 + 7000
    # Every run is still visible, and the newest is still whole.
    for i in range(20):
        assert f"## Run {i}" in block
    assert "Opening summary 0." in block


def test_a_bad_budget_setting_falls_back_instead_of_killing_the_run():
    pid = _project()
    _write_learnings()
    db.set_setting("prompt_learnings_kb", "not a number")
    db.set_setting("prompt_journal_kb", "")
    prompt = agent_runner.build_prompt("build", db.get_project(pid))
    assert "## Memory: learnings.md" in prompt


def test_a_run_records_what_it_cost():
    pid = _project()
    run_id = db.create_run(pid, "build", "claude-opus-5")
    agent_runner._record_usage(run_id, {
        "input_tokens": 11,
        "output_tokens": 22,
        "cache_creation_input_tokens": 33,
        "cache_read_input_tokens": 44,
    }, 105_000)
    row = db.get_run(run_id)
    assert row["input_tokens"] == 11
    assert row["output_tokens"] == 22
    assert row["cache_write_tokens"] == 33
    assert row["cache_read_tokens"] == 44
    assert row["prompt_bytes"] == 105_000


def test_a_run_with_no_usage_event_still_records_its_prompt_size():
    """A timed-out or crashed run has no `usage`, but the portal always knows
    how big a prompt it sent - and that is the number being budgeted."""
    pid = _project()
    run_id = db.create_run(pid, "build", "claude-opus-5")
    agent_runner._record_usage(run_id, None, 73_000)
    row = db.get_run(run_id)
    assert row["prompt_bytes"] == 73_000
    assert row["input_tokens"] is None


def test_recording_usage_never_takes_a_run_down_with_it(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db is on fire")
    monkeypatch.setattr(db, "record_run_usage", boom)
    agent_runner._record_usage(1, {"input_tokens": 1}, 10)  # must not raise


def test_the_prompt_size_reaches_the_supervisor():
    """`_supervise` is a different function from the one holding the prompt, so
    the byte count has to be handed over explicitly. It was not, at first, and
    every run raised NameError at the moment it finished."""
    import inspect
    sig = inspect.signature(agent_runner._supervise)
    assert "prompt_bytes" in sig.parameters
    src = inspect.getsource(agent_runner.run_claude)
    assert "_supervise(proc, cwd, run_id, on_event, timeout_min, len(prompt))" in src


def test_the_contract_asks_for_a_self_contained_opening_paragraph():
    """The journal budget shows an old entry's heading and first paragraph and
    nothing else, so that paragraph has to be written to carry the handover.
    The budget without this instruction is an amputation; with it, a digest."""
    contract = agent_runner.AGENT_CONTRACT
    assert "self-contained opening paragraph" in contract
    assert "landmine" in contract


def test_the_run_page_shows_what_a_run_cost(client):
    pid = _project()
    run_id = db.create_run(pid, "build", "claude-opus-5")
    agent_runner._record_usage(run_id, {
        "output_tokens": 4321, "cache_read_input_tokens": 98765,
    }, 74 * 1024)
    db.finish_run(run_id, "ok")
    html = client.get(f"/run/{run_id}").text
    assert "74K" in html and "prompt" in html
    assert "4,321" in html
    assert "98,765" in html


def test_a_run_from_before_the_columns_existed_shows_no_empty_stats(client):
    """A column of dashes across every historical run would say less than
    nothing, so the stats appear only on runs that actually have them."""
    pid = _project()
    run_id = db.create_run(pid, "build", "claude-opus-5")
    db.finish_run(run_id, "ok")
    html = client.get(f"/run/{run_id}").text
    assert ">prompt<" not in html
    assert ">cached<" not in html


def test_the_settings_page_offers_both_budgets(client):
    html = client.get("/settings").text
    assert 'name="prompt_learnings_kb"' in html
    assert 'name="prompt_journal_kb"' in html
    # And they are declared by the panel that owns them, or saving that panel
    # would blank them out.
    assert "prompt_learnings_kb,prompt_journal_kb" in html


def test_the_compactor_is_told_that_order_now_decides_what_survives():
    guidance = agent_runner.TASK_GUIDANCE["compact"]
    assert "ORDER IS NOW LOAD-BEARING" in guidance
    assert "byte budget" in guidance
