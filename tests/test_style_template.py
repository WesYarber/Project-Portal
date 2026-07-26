"""The reusable style template (Wes's 2026-07-22 04:05 note).

    "would it be possible to package up and keep up-to-date a sort of
    graphical style template from this that I could choose to apply to things
    I build? I think it would simplify getting the right theme for a tool"

The package is app/static/terminal-theme.css - one self-contained file - with
a gallery at /style and a skill telling agents on other projects to use it.
Wes later renamed the branding (his 06:01 note): it is a *dark terminal theme*
that the portal happens to follow, not a portal theme - so nothing user-facing
calls it a portal theme any more, and the old file name redirects.

"Keep up-to-date" is this file's job: the token-sync test compares every
design token the template defines against the portal's own style.css, so a
theme change to the portal fails the suite until the template is synced to
match. That makes drift a red test instead of a slow divergence Wes notices
months later.
"""
from __future__ import annotations

import re

from string import Template

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


STATIC = config.APP_ROOT / "app" / "static"


def tokens(css: str) -> dict[str, str]:
    """Custom properties, values whitespace-normalised (--scanlines spans
    several lines in both files). First definition wins: style.css redefines
    --font-body under body.font-sans etc for the font-switch setting, and the
    comparable value is the :root default, not a scoped override."""
    found = {}
    for m in re.finditer(r"(--[a-z-]+)\s*:\s*(.*?);", css, re.DOTALL):
        found.setdefault(m.group(1), re.sub(r"\s+", " ", m.group(2)).strip())
    return found


def template_css() -> str:
    return (STATIC / "terminal-theme.css").read_text()


def portal_css() -> str:
    return (STATIC / "style.css").read_text()


# --------------------------------------------------------------------------
# The sync contract
# --------------------------------------------------------------------------

def test_every_template_token_matches_the_portal():
    """The whole point of the template is that it IS the portal's look. Any
    token both files define must agree exactly - if this fails, the portal's
    theme changed and terminal-theme.css needs the new value copied over."""
    theirs = tokens(portal_css())
    ours = tokens(template_css())
    mismatched = {
        name: (ours[name], theirs[name])
        for name in ours
        if name in theirs and ours[name] != theirs[name]
    }
    assert mismatched == {}, f"terminal-theme.css has drifted from style.css: {mismatched}"


def test_the_core_tokens_are_all_present():
    """A token quietly deleted from the template would pass the match test by
    absence - so the set that defines the look is pinned by name."""
    ours = tokens(template_css())
    for name in [
        "--terminal-fg", "--terminal-dim", "--bg-color", "--window-bg",
        "--window-border", "--header-bg", "--panel-bg", "--font-mono",
        "--font-body", "--ansi-blue", "--ansi-cyan", "--ansi-green",
        "--ansi-yellow", "--ansi-red", "--ansi-magenta", "--portal-blue",
        "--portal-orange", "--control-h", "--panel-max-h",
    ]:
        assert name in ours, f"terminal-theme.css lost {name}"


def test_template_is_self_contained():
    """Every var() the template uses must be defined in the template itself -
    a reference resolved only when style.css happens to be loaded too would
    work on the portal and silently break in the project that copies the one
    file."""
    css = template_css()
    defined = set(tokens(css))
    used = set(re.findall(r"var\((--[a-z-]+)", css))
    assert used - defined == set()


# --------------------------------------------------------------------------
# The gallery page
# --------------------------------------------------------------------------

def test_style_page_renders_from_the_template_alone(client):
    r = client.get("/style")
    assert r.status_code == 200
    body = r.text
    assert "terminal-theme.css" in body
    # The proof of self-containment: the portal's own stylesheet is not
    # loaded. (Substring-safe: 'terminal-theme.css' does not contain it.)
    assert "static/style.css" not in body
    # One-click download, and the fetchable URL spelled out.
    assert 'download="terminal-theme.css"' in body
    # The rebrand (his 06:01 note): it is a dark terminal theme, not the
    # portal's theme.
    assert "portal-theme" not in body
    assert "Dark terminal theme" in body
    # The starter skeleton is there to copy.
    assert "terminal-window" in body
    assert "scan-all" not in body.split("starter")[0][:100]  # sanity anchor only


def test_style_page_shows_the_components(client):
    body = client.get("/style").text
    for piece in ["badge accent-green", "btn", "checkbox-row", "banner-alert",
                  "scroll-cap", "dot ok", "nav-tabs", "window-dot close"]:
        assert piece in body, f"gallery lost {piece}"


# --------------------------------------------------------------------------
# Agents are told about it
# --------------------------------------------------------------------------

def test_the_skill_exists_and_reaches_prompts():
    skill = config.APP_ROOT / "app" / "skills" / "terminal-style" / "SKILL.md"
    assert skill.is_file()
    text = skill.read_text()
    assert text.startswith("---")
    assert "description:" in text
    # The path the skill tells agents to copy from must be real.
    assert (STATIC / "terminal-theme.css").is_file()
    assert "terminal-theme.css" in text
    # And the URLs it hands out are ones the owner can open from any device -
    # never localhost (his 04:56 note). Since #254 the skill carries the
    # placeholder rather than a hostname, and worker._localise_skill fills it
    # in as the file is copied into a workspace - so assert both halves, or a
    # skill that ships an unsubstituted "$BASE_URL/style" would pass.
    assert "$BASE_URL/style" in text
    assert "127.0.0.1:" not in text
    filled = Template(text).safe_substitute(**config.SITE.template_vars())
    assert f"{config.SITE.base_url}/style" in filled
    assert "$BASE_URL" not in filled
    # The old skill name is gone, so _sync_skills removes it from workspaces.
    assert not (config.APP_ROOT / "app" / "skills" / "portal-style").exists()


def test_skills_index_names_terminal_style(monkeypatch):
    monkeypatch.setattr(config, "SKILLS_DIR", config.APP_ROOT / "app" / "skills")
    section = agent_runner._skills_section()
    assert "terminal-style" in section


def test_the_old_theme_path_redirects(client):
    """Anything already built against /static/portal-theme.css keeps working
    across the rename."""
    r = client.get("/static/portal-theme.css", follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == "/static/terminal-theme.css"
    assert client.get("/static/portal-theme.css").status_code == 200
