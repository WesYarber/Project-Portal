"""Skills the portal ships into every project workspace.

From the owner's note of 2026-07-21: set up a skill, and put something in
memory, that tells an agent working on a project that there is a desktop
machine it may use - and how to connect to it - so it can check web pages and
such visually, or do anything else a headless agent here cannot do for itself.

Two halves, and both are needed. The skill file goes into the workspace so the
detail is at hand; a one-line index goes into the run prompt, because a skill
nobody knows about is exactly the situation that left that machine unused while
every project shipped a UI no agent had ever looked at.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import agent_runner, config, db, site, worker


@pytest.fixture
def project():
    return db.create_project("Fridge Board", slug="fridge", stage="active", build_approved=True)


# --- the shipped skill ------------------------------------------------------
#
# A skill about one particular desktop on one particular LAN is 100% personal,
# so as of todo #254 that kind of skill lives in gitignored `data/skills/`
# rather than in the publishable tree. It is still shipped into every
# workspace and still indexed in every prompt, by exactly the same code path
# that carries a promoted skill; nothing about it changed for an install that
# has one. What changed is that a clone of this repo does not get somebody
# else's machine - and neither do these tests, which read whatever personal
# skill the install happens to have instead of naming one.

PERSONAL_SKILLS_DIR = site.BASE_DIR / "data" / "skills"


def _personal_skill_names() -> set[str]:
    return {p.parent.name for p in PERSONAL_SKILLS_DIR.glob("*/SKILL.md")}


def _render_skill() -> Path | None:
    """This install's personal screenshot-capable machine skill, if it has one.

    Matched on the screenshot WRAPPER rather than on the word "screenshot":
    this install has grown two more skills that mention taking one in passing
    (driving a browser, checking a phone layout), and sorted() was handing back
    the first of those instead of the machine skill - so the facts below were
    being looked for in a file that was never meant to carry them. The wrapper
    path is the artifact all of those facts hang off, which is why it is the
    selector here and no longer an assertion in the test below.
    """
    for path in sorted(PERSONAL_SKILLS_DIR.glob("*/SKILL.md")):
        if "deploy/screenshot.sh" in path.read_text(encoding="utf-8"):
            return path
    return None


personal_skill = pytest.mark.skipif(
    _render_skill() is None,
    reason="this install ships no personal machine skill (they are not published)",
)


def test_the_shipped_skills_dir_holds_no_personal_machine():
    """The whole point of the move: a clone gets no one else's hardware.

    What counts as personal is read off this installation - the host name from
    app.site, the skill names from the gitignored directory - rather than
    written out here, which is the same reason the move happened at all."""
    host = site.SITE.host
    personal = _personal_skill_names()
    for skill in config.SKILLS_DIR.rglob("SKILL.md"):
        text = skill.read_text(encoding="utf-8")
        # A loopback name is not this machine's identity and does appear in the
        # shipped prose, so it is the one host string that proves nothing.
        if host and host not in site.LOOPBACK_HOSTS:
            assert host not in text, skill
        for name in personal:
            assert name not in text, skill
    for name in personal:
        assert not (config.SKILLS_DIR / name).exists()


def test_the_example_skill_still_ships():
    skill = config.SKILLS_DIR / "terminal-style" / "SKILL.md"
    assert skill.is_file()
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "name: terminal-style" in text
    assert "description:" in text


@personal_skill
def test_the_machine_skill_carries_the_facts_that_took_four_attempts_to_learn():
    """Each of these is a failure a previous run burned time rediscovering. If
    one is dropped from the skill, the next agent pays for it again.

    The two addresses are matched by shape, not spelled out: whose machine this
    install talks to is nobody else's business, but a skill that has stopped
    saying how to reach the machine - or how the machine reaches back - is
    still the bug this test was written for."""
    text = _render_skill().read_text(encoding="utf-8")
    addresses = set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text))
    addresses.discard("0.0.0.0")              # a bind address, not a reachable one
    assert len(addresses) >= 2                # the machine, and the way back here
    assert "Firefox" in text                  # hangs under --headless
    assert "confined to `$HOME`" in text      # snap chromium, silent no-op to /tmp
    assert "--headless=new" in text           # never fires with a stream open
    assert "--virtual-time-budget" in text    # the flag that makes it work
    # deploy/screenshot.sh is what _render_skill matches on, so asserting it
    # here would only be asserting the selector back at itself.


# --- syncing into a workspace ----------------------------------------------


def test_ensure_workspace_installs_the_skills(temp_data_dir):
    ws = temp_data_dir / "projects" / "fridge"
    worker._ensure_workspace(ws)  # noqa: SLF001
    assert (ws / ".claude" / "skills" / "terminal-style" / "SKILL.md").is_file()


def test_skills_are_refreshed_on_every_run_not_just_at_creation(temp_data_dir):
    """Editing a skill in the portal repo has to reach workspaces that already
    exist, or a fix only helps projects created after it."""
    ws = temp_data_dir / "projects" / "fridge"
    worker._ensure_workspace(ws)  # noqa: SLF001
    installed = ws / ".claude" / "skills" / "terminal-style" / "SKILL.md"
    installed.write_text("clobbered", encoding="utf-8")

    worker._ensure_workspace(ws)  # noqa: SLF001
    assert installed.read_text(encoding="utf-8") != "clobbered"
    assert "terminal-style" in installed.read_text(encoding="utf-8")


def test_a_removed_skill_does_not_linger_in_a_workspace(temp_data_dir):
    ws = temp_data_dir / "projects" / "fridge"
    worker._ensure_workspace(ws)  # noqa: SLF001
    stale = ws / ".claude" / "skills" / "retired-skill"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("old", encoding="utf-8")

    worker._ensure_workspace(ws)  # noqa: SLF001
    assert not stale.exists()
    assert (ws / ".claude" / "skills" / "terminal-style").is_dir()


def test_syncing_leaves_the_rest_of_dot_claude_alone(temp_data_dir):
    """.claude is also the agent's own scratch space - settings, local state.
    Only the skills directory is the portal's to manage."""
    ws = temp_data_dir / "projects" / "fridge"
    ws.mkdir(parents=True)
    (ws / ".claude").mkdir()
    (ws / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")

    worker._ensure_workspace(ws)  # noqa: SLF001
    assert (ws / ".claude" / "settings.local.json").exists()


def test_skills_are_excluded_from_the_project_s_git_repo(temp_data_dir):
    """They are the portal's files, not the project's. Without this they show
    up in every `git status` an agent runs and get committed into 17 unrelated
    repos."""
    ws = temp_data_dir / "projects" / "fridge"
    worker._ensure_workspace(ws)  # noqa: SLF001
    exclude = ws / ".git" / "info" / "exclude"
    assert ".claude/" in exclude.read_text(encoding="utf-8").split()

    # And not appended again on every single run.
    before = exclude.read_text(encoding="utf-8")
    worker._ensure_workspace(ws)  # noqa: SLF001
    assert exclude.read_text(encoding="utf-8") == before


def test_git_exclude_keeps_what_was_already_there(temp_data_dir):
    ws = temp_data_dir / "projects" / "fridge"
    worker._ensure_workspace(ws)  # noqa: SLF001
    exclude = ws / ".git" / "info" / "exclude"
    exclude.write_text("*.tmp\n", encoding="utf-8")

    worker._ensure_workspace(ws)  # noqa: SLF001
    text = exclude.read_text(encoding="utf-8")
    assert "*.tmp" in text
    assert ".claude/" in text.split()


def test_a_missing_skills_dir_is_not_fatal(temp_data_dir, monkeypatch):
    monkeypatch.setattr(config, "SKILLS_DIR", temp_data_dir / "nope")
    ws = temp_data_dir / "projects" / "fridge"
    worker._ensure_workspace(ws)  # noqa: SLF001
    assert ws.is_dir()
    assert not (ws / ".claude" / "skills").exists()


# --- the index in the run prompt -------------------------------------------


def test_the_prompt_lists_the_skill_and_where_to_read_it(project):
    prompt = agent_runner.build_prompt("BUILD.", project)
    assert "## Skills available to you" in prompt
    assert "**terminal-style**" in prompt
    assert ".claude/skills/terminal-style/SKILL.md" in prompt


def test_the_prompt_carries_the_skill_description_not_its_whole_body(project):
    prompt = agent_runner.build_prompt("BUILD.", project)
    assert "terminal" in prompt.lower()
    # The recipe stays in the file; the prompt is an index.
    assert "terminal-theme.css" not in prompt


def test_no_skills_means_no_heading(project, monkeypatch, temp_data_dir):
    empty = temp_data_dir / "empty-skills"
    empty.mkdir()
    monkeypatch.setattr(config, "SKILLS_DIR", empty)
    assert "## Skills available to you" not in agent_runner.build_prompt("BUILD.", project)


def test_a_directory_without_a_skill_file_is_skipped(project, monkeypatch, temp_data_dir):
    root = temp_data_dir / "skills"
    (root / "notes").mkdir(parents=True)
    (root / "real").mkdir()
    (root / "real" / "SKILL.md").write_text(
        "---\nname: real\ndescription: does a thing\n---\n", encoding="utf-8"
    )
    monkeypatch.setattr(config, "SKILLS_DIR", root)

    prompt = agent_runner.build_prompt("BUILD.", project)
    assert "**real**" in prompt
    assert "**notes**" not in prompt


@personal_skill
def test_the_skill_points_at_the_password_rather_than_carrying_it():
    """Asked whether the machine's sudo password should stay in a file that is
    copied into every workspace, the owner said to leave a pointer instead, in
    case it ever changes.

    Seventeen copies of a secret is a secret that cannot be rotated. The file
    it points at lives on the server only and is gitignored - and so is the
    real secret this reads, which is why the check compares against `secrets/`
    rather than naming a password here."""
    text = _render_skill().read_text(encoding="utf-8")
    for path in sorted((config.BASE_DIR / "secrets").glob("*")):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            value = line.strip()
            if len(value) >= 6:  # short lines are labels, not credentials
                assert value not in text, path
    assert "secrets/" in text  # a pointer to where the real thing lives


def test_the_secrets_directory_is_not_in_git():
    ignore = (config.BASE_DIR / ".gitignore").read_text(encoding="utf-8")
    assert "secrets/" in ignore.splitlines()
