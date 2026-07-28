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

# Deliberately nothing like the author's name, and the other gender, so a leak
# is unmissable rather than a subtle near-match.
# Built by overriding the shipped defaults rather than by listing every field,
# so adding a field to Site cannot break this file - it did exactly that when
# `render_host` arrived, and a fixture that fails to *construct* takes the whole
# module's leak checks down with it, which is the worst way for this particular
# guard to stop working.
OTHER = site.Site(**{
    **site.defaults(),
    "owner": "Ada Lovelace",
    "gender": "female",
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


# --- one question, and the words that follow from it ------------------------


@pytest.mark.parametrize(
    "written,subject,obj,possessive",
    [
        ("male", "he", "him", "his"),
        ("female", "she", "her", "her"),
        # What a person types when asked "male or female?", which is the whole
        # point of asking that instead of asking for a pronoun set.
        ("Male", "he", "him", "his"),
        ("F", "she", "her", "her"),
        ("man", "he", "him", "his"),
        ("woman", "she", "her", "her"),
        # The three values the retired `pronouns` field could hold. An install
        # that answered this once has answered it.
        ("he", "he", "him", "his"),
        ("she/her", "she", "her", "her"),
        ("they", "they", "them", "their"),
        # Nothing recognisable must fall back, never raise: a typo in a config
        # file misgendering somebody is a worse failure than a boot error, and
        # a boot error is a worse failure than either.
        ("xe", "they", "them", "their"),
        ("", "they", "them", "their"),
        ("   ", "they", "them", "their"),
    ],
)
def test_the_words_follow_from_the_gender(written, subject, obj, possessive):
    resolved = site.Site(**{**OTHER.__dict__, "gender": written})
    assert (resolved.they, resolved.them, resolved.their) == (subject, obj, possessive)


def test_the_default_is_unanswered():
    """A name implies nothing about this, and the machine does not know."""
    assert site.defaults()["gender"] == ""
    assert site.load(env={}, use_file=False).they == "they"


def test_gender_comes_from_the_config_file_and_the_environment(tmp_path):
    path = tmp_path / "portal.toml"
    path.write_text('gender = "female"\n')
    assert site.load(env={}, path=path).they == "she"
    assert site.load(env={"PORTAL_GENDER": "male"}, path=path).they == "he"


def test_an_old_config_files_pronouns_key_still_answers_the_question(tmp_path):
    """`pronouns = "he"` is what this was called before 2026-07-28.

    Honouring it is what stops every existing install silently reverting to
    they/them at the next boot - the failure would be invisible in the config
    file, which still says what it always said.
    """
    path = tmp_path / "portal.toml"
    path.write_text('pronouns = "he"\n')
    resolved = site.load(env={}, path=path)
    assert resolved.gender == "male"
    assert resolved.they == "he"


def test_an_explicit_gender_beats_a_leftover_pronouns_line(tmp_path):
    path = tmp_path / "portal.toml"
    path.write_text('pronouns = "he"\ngender = "female"\n')
    assert site.load(env={}, path=path).they == "she"


def test_the_config_value_is_normalized_on_the_way_in(tmp_path):
    """`Site` is frozen and shared, so it should carry the compared form."""
    path = tmp_path / "portal.toml"
    path.write_text('gender = "  MALE  "\n')
    assert site.load(env={}, path=path).gender == "male"


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
@pytest.mark.parametrize("gender", ["male", "female", ""])
def test_no_prompt_breaks_verb_agreement_for_any_owner(name, gender):
    rendered = render_as(
        PROMPT_TEMPLATES[name], site.Site(**{**OTHER.__dict__, "gender": gender})
    )
    broken = BAD_AGREEMENT.findall(rendered)
    assert not broken, (
        f"{name} rendered for {gender or 'an unanswered owner'} reads {broken} - a "
        "placeholder is followed by a conjugated verb; use a modal or a noun phrase"
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
        site.Site(**{**OTHER.__dict__, "owner": "Wes", "gender": "male"}),
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


# --- American English ------------------------------------------------------
#
# Wes, 2026-07-28: "add some note to a system prompt or something somewhere that
# I want to always use American English spellings rather than British. I want
# 'color,' not 'colour.' 'Gray,' not 'grey,' etc. I have seen you use 'colour'
# in a few places."


def test_the_contract_asks_for_american_spellings():
    from app import agent_runner

    contract = agent_runner.AGENT_CONTRACT
    assert "American English" in contract
    for pair in ('"color" not "colour"', '"gray"'):
        assert pair in contract, pair


def test_the_contract_itself_uses_them():
    """It would be a poor instruction to give in British English."""
    from app import agent_runner

    text = agent_runner.AGENT_CONTRACT
    # The words the instruction names, checked against the instruction. Each is
    # bounded so "color" does not match inside the quoted counter-example the
    # rule has to spell out to forbid it.
    body = text.replace('"color" not "colour"', "").replace('"gray"\nnot "grey"', "")
    body = body.replace('"gray" not "grey"', "")
    for british in ("behaviour", "recognise", "labelled", "centre "):
        assert british not in body, british
