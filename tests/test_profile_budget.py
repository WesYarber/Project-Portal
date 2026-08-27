"""The owner profile stops growing without anybody deciding to.

Measured on 2026-08-07 across the 24 active projects: the average build prompt
was 85.9 KB, of which **26.7 KB (31.1%) was profile.md** - byte-identical in
every one of them, read whole, with no bound of any kind. Ten days earlier the
same file was 16.6 KB. The daily reflect rewrites it every day and had only
ever been told to be "concise", which is a word, not a number.

The fix is at the WRITE end, not the read end: the reflect is handed the cap
and the current size and told to come back under it. The read end only has a
backstop, at twice the cap, which trims whole `## ` sections and names them -
because unlike learnings.md this is a coherent authored document, and half of
"How he wants things built" reads as the whole of it.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config, db, promptbudget as pb, worker


@pytest.fixture
def client(temp_data_dir):
    from app.main import app

    return TestClient(app)


PROFILE = """# Profile: Wes

Who Wes is and what he cares about. Durable, big-picture facts only.

## Who he is

- A self-hoster in Arkansas.
- {a}

## What he values

- Nothing fails quietly.
- {b}

## Working style

- Delegates end to end.
- {c}
""".format(a="pad " * 200, b="pad " * 200, c="pad " * 200)


# --- The trim itself ---------------------------------------------------------

def test_a_profile_inside_its_budget_is_returned_byte_for_byte():
    assert pb.profile_for_prompt(PROFILE, 64 * 1024, "/p.md") == PROFILE


def test_the_overflowing_section_is_dropped_whole_not_halved():
    """The reason this is not `learnings_for_prompt`. A learning is one bullet
    and losing old ones costs old bullets; half a profile section reads as the
    whole of it and an agent builds to a standard it cannot see."""
    out = pb.profile_for_prompt(PROFILE, 1500, "/p.md")
    assert "## Who he is" in out
    # Not one word of the sections that did not fit, headings included.
    assert "Nothing fails quietly" not in out
    assert "Delegates end to end" not in out


def test_every_dropped_section_is_named_with_the_path_to_read_it():
    out = pb.profile_for_prompt(PROFILE, 1500, "/data/memory/profile.md")
    assert "What he values" in out
    assert "Working style" in out
    assert "/data/memory/profile.md" in out
    assert "You may READ it" in out


def test_the_title_and_preamble_always_survive():
    """They say what the document is and whose it is, which is what makes the
    'these sections are missing' pointer legible at all."""
    out = pb.profile_for_prompt(PROFILE, 400, "/p.md")
    assert out.startswith("# Profile: Wes")
    assert "Durable, big-picture facts only." in out


def test_a_section_below_the_overflow_goes_too_even_if_it_would_have_fit():
    """A profile with a hole in the middle still reads as complete. One cut off
    at a stated point does not, so the order is the priority order and the trim
    is a single cut."""
    text = (
        "# Profile\n\nPreamble.\n\n"
        "## Big\n\n- " + "x" * 3000 + "\n\n"
        "## Tiny\n\n- one short line\n"
    )
    out = pb.profile_for_prompt(text, 900, "/p.md")
    assert "one short line" not in out
    assert "Tiny" in out  # named, not silently gone


def test_a_flat_profile_with_no_headings_is_cut_off_and_says_so():
    """The backstop's backstop: a file somebody flattened, or a reflect that
    ran away. Nothing structural to cut on, so it cuts and admits it."""
    text = "# Profile\n\n" + "\n\n".join(f"Paragraph {i}. " + "y" * 200 for i in range(40))
    out = pb.profile_for_prompt(text, 2000, "/p.md")
    assert len(out) < 2600
    assert "CUT OFF here" in out
    assert "/p.md" in out


# --- What the daily reflect is told ------------------------------------------

@pytest.fixture
def _profile(temp_data_dir):
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    def write(text: str) -> None:
        config.PROFILE_MD.write_text(text, encoding="utf-8")
    return write


def test_the_reflect_prompt_carries_the_cap_as_a_number(_profile):
    _profile(PROFILE)
    db.set_setting("profile_cap_kb", "16")
    prompt = agent_runner.build_prompt("reflect", None)
    assert "keep profile.md under 16 KB" in prompt
    assert "ORDER IS LOAD-BEARING" in prompt


def test_a_reflect_on_an_oversized_profile_is_told_to_shrink_it(_profile):
    _profile("# Profile\n\nP.\n\n## S\n\n- " + "z" * 40_000 + "\n")
    db.set_setting("profile_cap_kb", "16")
    prompt = agent_runner.build_prompt("reflect", None)
    assert "over its 16 KB cap" in prompt
    assert "must come back UNDER" in prompt
    # And it is told to merge before deleting - losing a duplicate is free.
    assert "merge before you delete" in prompt


def test_a_zero_cap_says_nothing_to_the_reflect_at_all(_profile):
    _profile(PROFILE)
    db.set_setting("profile_cap_kb", "0")
    prompt = agent_runner.build_prompt("reflect", None)
    assert "Hard target" not in prompt


# --- The backstop in a real build prompt -------------------------------------

def test_a_profile_at_the_cap_reaches_a_build_prompt_whole(_profile):
    """The day-to-day state. The reflect is what keeps this file small; a trim
    in the prompt is a symptom, so it must not be the normal case."""
    _profile(PROFILE)
    db.set_setting("profile_cap_kb", "2")   # PROFILE is ~2.6 KB: over, under 2x
    project = db.create_project("P", stage="active", build_approved=True, slug="p")
    prompt = agent_runner.build_prompt("build", db.get_project(project["id"]))
    assert "Delegates end to end" in prompt
    assert "left out WHOLE" not in prompt


def test_past_twice_the_cap_a_build_prompt_starts_dropping_sections(_profile):
    _profile(PROFILE)
    db.set_setting("profile_cap_kb", "1")
    project = db.create_project("P", stage="active", build_approved=True, slug="p")
    prompt = agent_runner.build_prompt("build", db.get_project(project["id"]))
    assert "Delegates end to end" not in prompt
    assert "left out WHOLE" in prompt
    assert "Working style" in prompt
    assert str(config.PROFILE_MD) in prompt


def test_a_zero_cap_leaves_the_profile_unbounded_in_a_prompt(_profile):
    _profile(PROFILE)
    db.set_setting("profile_cap_kb", "0")
    project = db.create_project("P", stage="active", build_approved=True, slug="p")
    prompt = agent_runner.build_prompt("build", db.get_project(project["id"]))
    assert "Delegates end to end" in prompt


def test_a_junk_cap_setting_falls_back_to_the_default(_profile):
    db.set_setting("profile_cap_kb", "sixteen")
    assert agent_runner.profile_cap_bytes() == 16 * 1024


# --- The nag ------------------------------------------------------------------

def test_over_cap_is_reported_to_the_memory_page(_profile):
    _profile("# Profile\n\nP.\n\n## S\n\n- " + "z" * 40_000 + "\n")
    db.set_setting("profile_cap_kb", "16")
    assert worker.profile_over_cap() is True
    _profile(PROFILE)
    assert worker.profile_over_cap() is False


def test_the_memory_page_shows_the_size_and_the_warning(_profile, client):
    _profile("# Profile\n\nP.\n\n## S\n\n- " + "z" * 40_000 + "\n")
    db.set_setting("profile_cap_kb", "16")
    body = client.get("/memory").text
    assert "cap 16 KB" in body
    assert "Over the 16 KB cap" in body


def test_a_profile_under_the_cap_gets_no_warning(_profile, client):
    _profile(PROFILE)
    db.set_setting("profile_cap_kb", "16")
    body = client.get("/memory").text
    assert "characters, whole, in every prompt" in body
    assert "Over the 16 KB cap" not in body


def test_the_reflect_journals_the_size_it_measured_off_disk(_profile, monkeypatch):
    """Not the size the agent claims it wrote. The agent describing its own
    edit is the one source that cannot be checked, and this file is 31% of
    every prompt - so the journal line is measured, before and after."""
    import asyncio

    from app import agent_runner as ar

    _profile("# Profile\n\nP.\n\n## S\n\n- " + "z" * 30_000 + "\n")
    db.set_setting("profile_cap_kb", "16")

    async def fake_run(*args, **kwargs):
        # What a reflect does: rewrite the file in place, then report.
        config.PROFILE_MD.write_text("# Profile\n\nP.\n\n## S\n\n- short now\n",
                                     encoding="utf-8")
        return ar.RunResult(ok=True, result_text="done", report={})

    monkeypatch.setattr(ar, "run_claude", fake_run)
    asyncio.run(worker.run_reflect())

    line = db.list_journal(project_id=None, limit=1)[0]["content_md"]
    assert "30024 -> 33 chars" in line
    assert "cap 16 KB" in line
    assert "Still over its cap" not in line


def test_a_reflect_that_did_not_shrink_it_enough_says_so(_profile, monkeypatch):
    import asyncio

    from app import agent_runner as ar

    _profile("# Profile\n\nP.\n\n## S\n\n- " + "z" * 30_000 + "\n")
    db.set_setting("profile_cap_kb", "16")

    async def fake_run(*args, **kwargs):
        config.PROFILE_MD.write_text("# Profile\n\nP.\n\n## S\n\n- " + "z" * 25_000 + "\n",
                                     encoding="utf-8")
        return ar.RunResult(ok=True, result_text="done", report={})

    monkeypatch.setattr(ar, "run_claude", fake_run)
    asyncio.run(worker.run_reflect())
    assert "Still over its cap" in db.list_journal(project_id=None, limit=1)[0]["content_md"]


def test_the_cap_is_settable_from_the_settings_form(client, temp_data_dir):
    client.post("/settings", data={"_fields": "profile_cap_kb", "profile_cap_kb": "24"})
    assert db.get_setting("profile_cap_kb") == "24"
    assert agent_runner.profile_cap_bytes() == 24 * 1024
