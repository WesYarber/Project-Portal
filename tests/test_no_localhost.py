"""No user-facing localhost, anywhere (the owner's 2026-07-22 04:56 note,
paraphrased here so it names no particular machine):

    "Never tell me to run something on localhost when it runs on the server.
    I will always be connecting with a different device. replace localhost
    with the server's own hostname."

The owner reads everything - pages, summaries, journal entries, agent replies -
from another machine, so a localhost URL is always a dead link. The portal's
own templates must never print one, and both agent contracts must carry the
rule, because the agents are where most of the URLs the owner sees actually
come from.
"""
from __future__ import annotations

import re

from app import agent_runner, config, oneoff

TEMPLATES = config.APP_ROOT / "app" / "templates"
SKILLS = config.APP_ROOT / "app" / "skills"

# URL-shaped uses only: `http://localhost`, `localhost:8500` and the
# 127.0.0.1 equivalents. The bare words stay legal so a file can state the
# rule ("never localhost") without tripping the check that enforces it.
LOOPBACK_URL = re.compile(r"(https?://)?(localhost|127\.0\.0\.1)[:/]")


def test_no_template_prints_a_localhost_url():
    for path in TEMPLATES.glob("*.html"):
        assert not LOOPBACK_URL.search(path.read_text()), path.name


def test_no_shipped_skill_hands_out_a_localhost_url():
    """Skills are copied into every workspace; a localhost URL in one ends up
    pasted into an agent's reply to the owner."""
    for path in SKILLS.rglob("SKILL.md"):
        assert not LOOPBACK_URL.search(path.read_text()), path


def test_the_project_contract_carries_the_rule():
    # Both contracts are templated from the site config (app/site.py), so the
    # hostname they hand agents is whatever *this* installation resolves to.
    # Asserting on config.HOST_LABEL is the same check as naming one machine,
    # minus the machine.
    contract = agent_runner.AGENT_CONTRACT
    assert config.HOST_LABEL and config.HOST_LABEL in contract
    assert "never `localhost`" in contract


def test_the_oneoff_contract_carries_the_rule():
    contract = oneoff.ONEOFF_CONTRACT
    assert config.HOST_LABEL and config.HOST_LABEL in contract
    assert "never `localhost`" in contract
