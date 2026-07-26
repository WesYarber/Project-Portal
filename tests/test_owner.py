"""The prompts name nobody in particular.

Step 2 of open-sourcing (todo #254). Step 1 took this installation's *machine*
out of the tree; this takes the *person* out - and the pronouns with them,
which is the half that is easy to miss. The agent contract is written about a
human being ("a hostname they can reach", "only Ada can decide this"), so a
tree that templates the name but leaves "he" behind would misgender every
other owner in every prompt its agents ever write.

The load-bearing idea in here is `render_as`: every prompt is re-substituted
under a *foreign* identity and then checked. Scanning the source text for the
string "Wes" cannot work - the tree has 281 legitimate mentions in comments
explaining why decisions were made, and those are honest history worth
keeping. Rendering under somebody else separates the two perfectly: a name
baked into a prompt survives the swap and shows up, a name in a comment never
reaches the rendered string at all.
"""
from __future__ import annotations

import re
from string import Template

import pytest

from app import agent_runner, ask, config, db, nl, notes, oneoff, site, todos

# Deliberately nothing like the author's name, and a different pronoun set, so
# a leak is unmissable rather than a subtle near-match.
# Built by overriding the shipped defaults rather than by listing every field,
# so adding a field to Site cannot break this file - it did exactly that when
# `render_host` arrived, and a fixture that fails to *construct* takes the whole
# module's leak checks down with it, which is the worst way for this particular
# guard to stop working.
OTHER = site.Site(**{
    **site.defaults(),
    "owner": "Ada Lovelace",
    "pronouns": "she",
    "host": "demo-box",
    "ssh_user": "ada",
})

# A gendered pronoun in a prompt's SOURCE can only be referring to the owner -
# there is no other human in the text - so finding one means a hard-coded
# person. `they/them/their` deliberately are NOT here: they are the correct
# default *and* ordinary English for a plural thing ("list them" = the
# options), so flagging them would be all false positives. Word-boundaried,
# or "the" would match "he" and "this" would match "his".
GENDERED = re.compile(r"\b(?:he|him|his|she|her|hers)\b", re.IGNORECASE)


def render_as(template: str, resolved: site.Site = OTHER) -> str:
    return Template(template).safe_substitute(**resolved.template_vars())


# --- the pronoun table ------------------------------------------------------


@pytest.mark.parametrize(
    "written,subject,obj,possessive",
    [
        ("they", "they", "them", "their"),
        ("he", "he", "him", "his"),
        ("she", "she", "her", "her"),
        # The shorthand people actually type into a config file.
        ("he/him", "he", "him", "his"),
        ("She/Her", "she", "her", "her"),
        ("they/them/theirs", "they", "them", "their"),
        # Nothing recognisable must fall back, never raise: a typo in a config
        # file misgendering somebody is a worse failure than a boot error, and
        # a boot error is a worse failure than either.
        ("xe", "they", "them", "their"),
        ("", "they", "them", "their"),
        ("   ", "they", "them", "their"),
    ],
)
def test_pronouns_resolve(written, subject, obj, possessive):
    resolved = site.Site(**{**OTHER.__dict__, "pronouns": written})
    assert (resolved.they, resolved.them, resolved.their) == (subject, obj, possessive)


def test_the_default_is_they():
    """A name implies nothing about pronouns, and the machine does not know."""
    assert site.defaults()["pronouns"] == "they"
    assert site.load(env={}, use_file=False).they == "they"


def test_pronouns_come_from_the_config_file_and_the_environment(tmp_path):
    path = tmp_path / "portal.toml"
    path.write_text('pronouns = "she"\n')
    assert site.load(env={}, path=path).they == "she"
    assert site.load(env={"PORTAL_PRONOUNS": "he"}, path=path).they == "he"


@pytest.mark.parametrize(
    "name,possessive",
    [
        ("Wes", "Wes's"),
        ("Ada Lovelace", "Ada Lovelace's"),
        # Chicago, and every line of prose already in this tree, writes
        # "Wes's" not "Wes'". Only classical names take the bare apostrophe,
        # and no config field can tell which is which from the spelling.
        ("Jess", "Jess's"),
        ("Charles", "Charles's"),
        ("", ""),
    ],
)
def test_the_possessive_is_english_not_string_concatenation(name, possessive):
    assert site.Site(**{**OTHER.__dict__, "owner": name}).owners == possessive


def test_template_vars_covers_every_placeholder_the_tree_uses():
    """A placeholder with no variable behind it renders as a literal `$WORD`."""
    keys = set(OTHER.template_vars())
    used = set()
    for path in (config.APP_ROOT / "app").rglob("*"):
        if path.suffix not in {".py", ".md"}:
            continue
        used |= set(re.findall(r"\$([A-Z][A-Z_]+)\b", path.read_text(encoding="utf-8")))
    # Only the ones we own; a shell snippet's $HOME is not ours to fill.
    assert {"OWNER", "OWNERS", "THEY", "THEIR", "HOST", "BASE_URL"} <= keys
    assert (used & {"OWNERS", "THEY", "THEM", "THEIR", "THEIRS", "PORTAL_ROOT"}) <= keys


# --- no prompt has a person baked into it -----------------------------------

PROMPT_TEMPLATES = {
    "agent contract": agent_runner._AGENT_CONTRACT_TEMPLATE,
    "one-off contract": oneoff._ONEOFF_CONTRACT_TEMPLATE,
    "ask instructions": ask._ASK_INSTRUCTIONS_TEMPLATE,
    "telegram router": nl._SYSTEM_PROMPT_TEMPLATE,
    **{f"guidance:{k}": v for k, v in agent_runner._TASK_GUIDANCE_TEMPLATES.items()},
}


def test_the_scan_actually_covers_the_prompts():
    """A lookup bug here would make every check below vacuously pass."""
    assert len(PROMPT_TEMPLATES) >= 10
    assert all(len(t) > 200 for t in PROMPT_TEMPLATES.values())


@pytest.mark.parametrize("name", sorted(PROMPT_TEMPLATES))
def test_no_prompt_has_a_person_written_into_it(name):
    """The source must name nobody; only the substitution may."""
    template = PROMPT_TEMPLATES[name]
    assert "Wes" not in template, f"{name} has a name baked into it"
    leaked = GENDERED.findall(template)
    assert not leaked, (
        f"{name} hard-codes the pronoun(s) {sorted(set(leaked))} - use $THEY/"
        "$THEM/$THEIR, or rephrase if the sentence needs verb agreement"
    )


@pytest.mark.parametrize("name", sorted(PROMPT_TEMPLATES))
def test_the_rendered_prompt_addresses_whoever_actually_owns_the_install(name):
    rendered = render_as(PROMPT_TEMPLATES[name])
    assert "Wes" not in rendered
    if "$OWNER" in PROMPT_TEMPLATES[name]:
        assert "Ada Lovelace" in rendered


# The trap this whole design exists to avoid, and one I fell into while
# writing it: `$THEY own` and `$THEY are` read correctly for they/them and
# become "he own" / "he are" for anybody else. There is no conjugation table,
# so a template must never put a verb straight after a pronoun placeholder -
# it has to use a modal ("$THEY will read") or a noun phrase instead.
#
# Singular pronouns take the -s form and `they` takes the bare one, so a
# template is only safe if BOTH renders are clean. Checking one would miss
# exactly half of these.
BAD_AGREEMENT = re.compile(
    r"\b(?:he|she)\s+(?:are|were|have|own|work|prefer|read|want|need|say|do)\b"
    r"|\bthey\s+(?:is|was|has|owns|works|prefers|reads|wants|needs|says|does)\b",
    re.IGNORECASE,
)


@pytest.mark.parametrize("name", sorted(PROMPT_TEMPLATES))
@pytest.mark.parametrize("pronouns", ["he", "she", "they"])
def test_no_prompt_breaks_verb_agreement_for_any_owner(name, pronouns):
    rendered = render_as(
        PROMPT_TEMPLATES[name], site.Site(**{**OTHER.__dict__, "pronouns": pronouns})
    )
    broken = BAD_AGREEMENT.findall(rendered)
    assert not broken, (
        f"{name} rendered with {pronouns}/... reads {broken} - a placeholder is "
        "followed by a conjugated verb; use a modal or a noun phrase"
    )


@pytest.mark.parametrize("name", sorted(PROMPT_TEMPLATES))
def test_every_prompt_substitutes_completely(name):
    """An unresolved `$OWNER` reaching a model is worse than a hard-coded name."""
    rendered = render_as(PROMPT_TEMPLATES[name])
    assert not re.search(r"\$(?:OWNER|OWNERS|THEY|THEM|THEIR|THEIRS|HOST|BASE_URL)\b", rendered)


def test_the_contract_still_reads_as_the_owners_own(monkeypatch):
    """The point of all this is that nothing changes for the current owner."""
    rendered = render_as(
        agent_runner._AGENT_CONTRACT_TEMPLATE,
        site.Site(**{**OTHER.__dict__, "owner": "Wes", "pronouns": "he"}),
    )
    assert "working on behalf of Wes" in rendered
    assert "a hostname he can reach" in rendered
    assert "blocked on\n  Wes's intent or a decision only he can make" in rendered


def test_the_json_shape_of_the_contract_survives_substitution():
    """`string.Template` and not `.format()` precisely so braces are safe."""
    rendered = render_as(agent_runner._AGENT_CONTRACT_TEMPLATE)
    assert '"todo_updates": {"add": [{"text": "...", "owner": "agent|user"' in rendered
    assert rendered.count("{") == agent_runner._AGENT_CONTRACT_TEMPLATE.count("{")


# --- the prompt fragments built at call time --------------------------------
#
# These read config.SITE when they run rather than at import, so they are
# checked by swapping the site out underneath them.


@pytest.fixture
def as_ada(monkeypatch):
    monkeypatch.setattr(config, "SITE", OTHER)
    return OTHER


def test_the_todo_list_heading_follows_the_owner(as_ada):
    project = db.create_project(title="P", description="d")
    db.add_todo(project["id"], "something only the owner can do", owner="user")
    text = todos.prompt_section(project["id"])
    assert "### Ada Lovelace's (only she can do these)" in text
    assert "Wes" not in text


def test_the_notes_block_follows_the_owner(as_ada):
    project = db.create_project(title="P", description="d")
    db.add_journal(project["id"], "user", "note", "a note")
    block = notes.render(notes.pending(project["id"]))
    assert "Ada Lovelace" in block and "Wes" not in block
