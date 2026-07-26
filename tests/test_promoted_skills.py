"""#226, the last piece of the memory overhaul: procedure-shaped learnings get
promoted to executable skills, and the compaction agent is handed the freshness
signal (added / last-confirmed dates, observation counts) to choose its cuts.

Grouped by the claim they defend: where promoted skills live (beside the
agents' cwd, never inside it), how they reach workspaces and prompts (the same
two channels the built-in skills use, built-in winning a name collision), what
the compaction prompt now says (the promotion licence with an absolute
destination, and the entry-ages digest stalest-first), and the /memory window
with its traversal-guarded view/delete - because Wes distrusts memory he
cannot see or prune.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config, memory, worker


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _write_skill(root, name: str, description: str = "When to use it.", body: str = "Steps.") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def _write_learnings(*bullets: str) -> None:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.LEARNINGS_MD.write_text(
        "\n".join(f"- {b}" for b in bullets) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------
# Where they live
# --------------------------------------------------------------------------

def test_promoted_dir_is_beside_memory_never_inside(temp_data_dir):
    """MEMORY_DIR is the reflect/compaction agents' cwd; a skills pile inside
    it would be exactly the noise those jobs launch to remove."""
    root = memory.promoted_skills_dir()
    assert root == temp_data_dir / "skills"
    assert config.MEMORY_DIR not in root.parents
    assert root != config.MEMORY_DIR


def test_listing_reads_names_and_descriptions(temp_data_dir):
    _write_skill(memory.promoted_skills_dir(), "render-a-page", "Render a page headlessly.")
    _write_skill(memory.promoted_skills_dir(), "extract-pdf-text", "Pull text from a PDF.")
    skills = memory.promoted_skills()
    assert [s.name for s in skills] == ["extract-pdf-text", "render-a-page"]
    assert skills[1].description == "Render a page headlessly."
    assert skills[0].lines > 0 and skills[0].size > 0


def test_listing_skips_dirs_without_a_skill_md(temp_data_dir):
    root = memory.promoted_skills_dir()
    _write_skill(root, "real-skill")
    (root / "half-written").mkdir(parents=True)
    (root / "half-written" / "notes.txt").write_text("wip", encoding="utf-8")
    assert [s.name for s in memory.promoted_skills()] == ["real-skill"]


def test_listing_is_empty_when_the_dir_is_missing(temp_data_dir):
    assert memory.promoted_skills() == []


# --------------------------------------------------------------------------
# Reading and deleting are traversal-guarded (the name comes from a URL)
# --------------------------------------------------------------------------

def test_read_returns_the_skill_text(temp_data_dir):
    _write_skill(memory.promoted_skills_dir(), "render-a-page", body="ssh and screenshot.")
    text = memory.read_promoted_skill("render-a-page")
    assert text is not None and "ssh and screenshot." in text


@pytest.mark.parametrize("bad", ["../secret", "a/b", "..", ".", "", ".hidden", "-dash-first"])
def test_read_refuses_path_shaped_names(temp_data_dir, bad):
    _write_skill(memory.promoted_skills_dir(), "real-skill")
    assert memory.read_promoted_skill(bad) is None


def test_read_missing_skill_is_none(temp_data_dir):
    assert memory.read_promoted_skill("nope") is None


def test_delete_removes_the_directory(temp_data_dir):
    root = memory.promoted_skills_dir()
    _write_skill(root, "old-recipe")
    assert memory.delete_promoted_skill("old-recipe") is True
    assert not (root / "old-recipe").exists()


@pytest.mark.parametrize("bad", ["../secret", "a/b", "..", "", "nope"])
def test_delete_refuses_bad_or_missing_names(temp_data_dir, bad):
    _write_skill(memory.promoted_skills_dir(), "real-skill")
    assert memory.delete_promoted_skill(bad) is False
    assert (memory.promoted_skills_dir() / "real-skill").exists()


# --------------------------------------------------------------------------
# They reach workspaces (worker._sync_skills) and prompts (_skills_section)
# --------------------------------------------------------------------------

@pytest.fixture
def builtin_skills(temp_data_dir, monkeypatch):
    root = temp_data_dir / "builtin-skills"
    _write_skill(root, "render-box", "The built-in eyes.")
    monkeypatch.setattr(config, "SKILLS_DIR", root)
    return root


def test_sync_ships_promoted_skills_alongside_builtins(temp_data_dir, builtin_skills):
    _write_skill(memory.promoted_skills_dir(), "render-recipe", "Promoted.")
    ws = temp_data_dir / "ws"
    worker._ensure_workspace(ws)
    assert (ws / ".claude" / "skills" / "render-box" / "SKILL.md").is_file()
    assert (ws / ".claude" / "skills" / "render-recipe" / "SKILL.md").is_file()


def test_sync_skips_a_promoted_dir_without_skill_md(temp_data_dir, builtin_skills):
    junk = memory.promoted_skills_dir() / "half-written"
    junk.mkdir(parents=True)
    (junk / "notes.txt").write_text("wip", encoding="utf-8")
    ws = temp_data_dir / "ws"
    worker._ensure_workspace(ws)
    assert not (ws / ".claude" / "skills" / "half-written").exists()


def test_builtin_wins_a_name_collision(temp_data_dir, builtin_skills):
    """The repo's copy is the curated one - a promoted skill must never shadow
    it in workspaces or in the prompt index."""
    _write_skill(memory.promoted_skills_dir(), "render-box", "An impostor.", body="impostor body")
    ws = temp_data_dir / "ws"
    worker._ensure_workspace(ws)
    shipped = (ws / ".claude" / "skills" / "render-box" / "SKILL.md").read_text(encoding="utf-8")
    assert "The built-in eyes." in shipped and "impostor" not in shipped
    section = agent_runner._skills_section()
    assert section.count("**render-box**") == 1
    assert "The built-in eyes." in section and "An impostor." not in section


def test_deleting_a_promoted_skill_sweeps_it_from_workspaces(temp_data_dir, builtin_skills):
    _write_skill(memory.promoted_skills_dir(), "render-recipe")
    ws = temp_data_dir / "ws"
    worker._ensure_workspace(ws)
    assert (ws / ".claude" / "skills" / "render-recipe").is_dir()
    memory.delete_promoted_skill("render-recipe")
    worker._sync_skills(ws)
    assert not (ws / ".claude" / "skills" / "render-recipe").exists()
    assert (ws / ".claude" / "skills" / "render-box").is_dir()  # built-in survives the sweep


def test_sync_works_with_only_promoted_skills(temp_data_dir, monkeypatch):
    """A missing built-in root must not stop promoted skills from shipping."""
    monkeypatch.setattr(config, "SKILLS_DIR", temp_data_dir / "no-such-dir")
    _write_skill(memory.promoted_skills_dir(), "render-recipe")
    ws = temp_data_dir / "ws"
    worker._ensure_workspace(ws)
    assert (ws / ".claude" / "skills" / "render-recipe" / "SKILL.md").is_file()


def test_prompt_index_lists_promoted_skills(temp_data_dir, builtin_skills):
    _write_skill(memory.promoted_skills_dir(), "render-recipe", "Promoted one-liner.")
    section = agent_runner._skills_section()
    assert "**render-recipe**" in section
    assert "Promoted one-liner." in section
    assert ".claude/skills/render-recipe/SKILL.md" in section


# --------------------------------------------------------------------------
# The compaction prompt: the promotion licence + the entry-ages digest
# --------------------------------------------------------------------------

def test_compact_prompt_licenses_promotion_with_an_absolute_path(temp_data_dir):
    prompt = agent_runner.build_prompt("compact", None)
    assert "Promote procedures to skills" in prompt
    assert str(memory.promoted_skills_dir()) in prompt
    assert "DROP the bullet" in prompt


def test_compact_prompt_carries_entry_ages_stalest_first(temp_data_dir):
    _write_learnings(
        "Fresh fact about render-box rendering.",
        "Stale fact about an old port.",
    )
    meta = {
        worker._learning_key("Fresh fact about render-box rendering."): {
            "added": "2026-07-01", "confirmed": "2026-07-20", "count": 6,
        },
        worker._learning_key("Stale fact about an old port."): {
            "added": "2026-03-01", "confirmed": "2026-03-01", "count": 1,
        },
    }
    memory.save_learnings_meta(meta)
    prompt = agent_runner.build_prompt("compact", None)
    assert "Entry ages" in prompt
    assert "[added 2026-03-01 / confirmed 2026-03-01 / seen 1x]" in prompt
    assert "[added 2026-07-01 / confirmed 2026-07-20 / seen 6x]" in prompt
    # Stalest (oldest last-confirmed) first - that ordering is the hint.
    assert prompt.index("2026-03-01") < prompt.index("Fresh fact")


def test_entry_ages_counts_undated_lines_without_listing_them(temp_data_dir):
    _write_learnings("Dated fact.", "Hand-written line nobody dated.")
    memory.save_learnings_meta({
        worker._learning_key("Dated fact."): {
            "added": "2026-06-01", "confirmed": "2026-06-01", "count": 1,
        },
    })
    section = agent_runner._entry_ages_section()
    assert "Dated fact." in section
    assert "Hand-written line nobody dated." not in section
    assert "1 further live lines are undated" in section


def test_entry_ages_truncates_long_bullets(temp_data_dir):
    long = "A very long procedure line " + "x" * 200
    _write_learnings(long)
    memory.save_learnings_meta({
        worker._learning_key(long): {"added": "2026-06-01", "confirmed": "2026-06-01", "count": 1},
    })
    section = agent_runner._entry_ages_section()
    assert "..." in section
    assert long not in section


def test_compact_prompt_survives_missing_sidecar_and_learnings(temp_data_dir):
    """Fails open: no freshness data just means no ages section - compaction
    must run exactly as it always has."""
    prompt = agent_runner.build_prompt("compact", None)
    assert "Entry ages" not in prompt
    assert "COMPACT THE LEARNINGS" in prompt


# --------------------------------------------------------------------------
# The /memory window
# --------------------------------------------------------------------------

def test_memory_page_lists_promoted_skills(client):
    _write_skill(memory.promoted_skills_dir(), "render-recipe", "Promoted one-liner.")
    body = client.get("/memory").text
    assert "Promoted skills" in body
    assert "render-recipe" in body
    assert "Promoted one-liner." in body


def test_memory_page_shows_empty_state_without_skills(client):
    body = client.get("/memory").text
    assert "No promoted skills yet" in body


def test_skill_view_route_shows_the_file(client):
    _write_skill(memory.promoted_skills_dir(), "render-recipe", body="exact commands here")
    r = client.get("/memory/skill/render-recipe")
    assert r.status_code == 200
    assert "exact commands here" in r.text


def test_skill_view_refuses_traversal(client):
    (memory.promoted_skills_dir()).mkdir(parents=True, exist_ok=True)
    secret = memory.promoted_skills_dir().parent / "portal.db"
    assert secret.exists()  # the thing a traversal would go for
    assert client.get("/memory/skill/%2e%2e%2fportal.db").status_code == 404
    # A literal ".." path segment is collapsed by clients before it ever
    # reaches the route, so the encoded form is the one that tests the guard.
    assert client.get("/memory/skill/%2e%2e").status_code == 404


def test_delete_route_removes_the_skill(client):
    _write_skill(memory.promoted_skills_dir(), "old-recipe")
    r = client.post("/memory/skill/old-recipe/delete", follow_redirects=False)
    assert r.status_code == 303
    assert not (memory.promoted_skills_dir() / "old-recipe").exists()


def test_delete_route_404s_on_missing(client):
    assert client.post("/memory/skill/nope/delete").status_code == 404
