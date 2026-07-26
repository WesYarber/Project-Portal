"""The site config (app/site.py): this installation's identity.

Two things are being pinned here, and the second matters more than the first.

1. That the config resolves correctly - file, environment, machine defaults,
   and every malformed input degrading to something bootable.

2. That the portal is no longer *wired* to one particular machine. The whole
   point of the exercise is that someone else can run this, so the tests that
   matter are the ones that load the tree under a different identity and check
   that nothing personal survives. Those are at the bottom of the file, and
   they are the reason this module exists at all.

Note that on the author's own box the machine defaults happen to equal the
values that used to be hard-coded, so a passing suite proves nothing by
itself - every test here is written to be independent of whose box it runs on.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import agent_runner, config, oneoff, site


# --- resolution order -------------------------------------------------------

def test_no_config_anywhere_still_yields_a_usable_site():
    """A fresh clone with no portal.toml and no environment must still boot."""
    resolved = site.load(env={}, use_file=False)
    assert resolved.port == 8500
    assert resolved.preview_port == 8501
    assert resolved.preview_https_port == 8443
    # Derived from the machine, so we can't assert the value - only that the
    # portal got *an* answer rather than an empty string it would print.
    assert resolved.host
    assert resolved.ssh_user


def test_machine_defaults_come_from_the_machine():
    import getpass
    import socket

    resolved = site.load(env={}, use_file=False)
    assert resolved.host == socket.gethostname()
    assert resolved.ssh_user == getpass.getuser()


def test_a_config_file_overrides_the_machine(tmp_path: Path):
    path = tmp_path / "portal.toml"
    path.write_text(
        'owner = "Ada Lovelace"\nhost = "analytical"\nport = 9000\nssh_user = "ada"\n',
        encoding="utf-8",
    )
    resolved = site.load(env={}, path=path)
    assert resolved.owner == "Ada Lovelace"
    assert resolved.host == "analytical"
    assert resolved.port == 9000
    assert resolved.ssh_user == "ada"
    # Untouched keys keep their defaults rather than becoming empty.
    assert resolved.preview_port == 8501


def test_a_portal_section_is_accepted_too(tmp_path: Path):
    """People reach for a section header by habit; failing silently is unkind."""
    path = tmp_path / "portal.toml"
    path.write_text('[portal]\nhost = "sectioned"\nport = 9100\n', encoding="utf-8")
    resolved = site.load(env={}, path=path)
    assert resolved.host == "sectioned"
    assert resolved.port == 9100


def test_the_environment_beats_the_file(tmp_path: Path):
    path = tmp_path / "portal.toml"
    path.write_text('host = "from-file"\nport = 9000\n', encoding="utf-8")
    resolved = site.load(env={"PORTAL_HOST": "from-env", "PORTAL_PORT": "9500"}, path=path)
    assert resolved.host == "from-env"
    assert resolved.port == 9500


def test_a_blank_environment_variable_does_not_blank_a_setting(tmp_path: Path):
    """`PORTAL_HOST=` in a unit file is an unset variable, not an empty host."""
    path = tmp_path / "portal.toml"
    path.write_text('host = "from-file"\n', encoding="utf-8")
    resolved = site.load(env={"PORTAL_HOST": "   "}, path=path)
    assert resolved.host == "from-file"


def test_portal_config_env_var_points_at_the_file(tmp_path: Path):
    path = tmp_path / "elsewhere.toml"
    path.write_text('host = "elsewhere"\n', encoding="utf-8")
    assert site.config_path({"PORTAL_CONFIG": str(path)}) == path
    assert site.load(env={"PORTAL_CONFIG": str(path)}).host == "elsewhere"


def test_a_missing_explicit_config_path_is_not_a_crash(tmp_path: Path):
    assert site.config_path({"PORTAL_CONFIG": str(tmp_path / "nope.toml")}) is None


# --- degrading rather than exploding ----------------------------------------

def test_a_malformed_config_file_falls_back_to_defaults(tmp_path: Path):
    """A broken config must not be an unbootable portal."""
    path = tmp_path / "portal.toml"
    path.write_text("this is not = = toml [[[\n", encoding="utf-8")
    resolved = site.load(env={}, path=path)
    assert resolved.port == 8500
    assert resolved.host


def test_a_non_numeric_port_is_ignored_rather_than_fatal(tmp_path: Path):
    path = tmp_path / "portal.toml"
    path.write_text('port = "eight thousand"\nhost = "kept"\n', encoding="utf-8")
    resolved = site.load(env={}, path=path)
    assert resolved.port == 8500
    # The rest of the file still applies - one bad line is not a bad file.
    assert resolved.host == "kept"


def test_unknown_keys_are_ignored(tmp_path: Path):
    """A config written for a later version must still load on this one."""
    path = tmp_path / "portal.toml"
    path.write_text('host = "fine"\nsome_future_key = "whatever"\n', encoding="utf-8")
    resolved = site.load(env={}, path=path)
    assert resolved.host == "fine"
    assert not hasattr(resolved, "some_future_key")


# --- derived values ---------------------------------------------------------

def test_handle_and_base_url_are_built_from_the_parts(tmp_path: Path):
    path = tmp_path / "portal.toml"
    path.write_text('host = "box"\nport = 9000\nssh_user = "ada"\n', encoding="utf-8")
    resolved = site.load(env={}, path=path)
    assert resolved.handle == "ada@box"
    assert resolved.base_url == "http://box:9000"


# --- warnings ---------------------------------------------------------------

@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "127.0.0.1", ""])
def test_an_unreachable_hostname_is_warned_about(host: str):
    """The portal's cardinal rule is that its links work on another device."""
    resolved = site.load(env={"PORTAL_HOST": host or "localhost"}, use_file=False)
    resolved = site.Site(**{**resolved.__dict__, "host": host})
    warnings = site.warnings(resolved)
    assert warnings and "reach" in warnings[0]


def test_a_reachable_hostname_warns_about_nothing():
    resolved = site.load(env={"PORTAL_HOST": "myserver", "PORTAL_OWNER": "Ada"}, use_file=False)
    assert site.warnings(resolved) == []


def test_a_missing_owner_is_warned_about():
    resolved = site.load(env={"PORTAL_HOST": "myserver"}, use_file=False)
    resolved = site.Site(**{**resolved.__dict__, "owner": ""})
    assert any("owner" in w for w in site.warnings(resolved))


# --- nothing is wired to one particular machine any more --------------------
#
# These are the tests the refactor exists for. They read the *source text* of
# the things that used to name a machine, rather than the resolved values,
# because on the author's own box the resolved values are indistinguishable
# from the old hard-coded ones - a test on those would pass either way and
# prove nothing.

# The needles come from the installation itself (app/leakscan.py), so this
# file no longer has to *contain* the strings it is forbidding - which it did
# until 2026-07-26, and which meant the guard against publishing personal
# infrastructure was itself the largest concentration of it in the tree.
from app import leakscan  # noqa: E402

PERSONAL = leakscan.compile_needles(
    leakscan.needles(extra=leakscan.extra_patterns())
)

SCANNED = leakscan.files()


def test_the_scan_actually_covers_something():
    """A file-list bug would make every check below vacuously pass."""
    assert len(SCANNED) > 40
    names = {p.name for p in SCANNED}
    assert {"config.py", "site.py", "base.html", "style.css"} <= names
    # The shipped skills are the newest thing in scope and the easiest to
    # forget, so pin that they are actually being read.
    assert any(p.name == "SKILL.md" for p in SCANNED)
    # deploy/ and tests/ joined the scan with the public repo (#256); before
    # that they were the two places personal hostnames were still legal.
    parts = {p.parts[0] for p in (q.relative_to(config.APP_ROOT) for q in SCANNED)}
    assert {"app", "deploy", "tests"} <= parts


def test_no_source_file_names_a_personal_machine_or_domain():
    leaks = leakscan.scan()
    assert not leaks, "personal strings left in the source:\n" + "\n".join(
        str(leak) for leak in leaks
    )


def test_the_agent_contract_carries_no_hostname_of_its_own():
    """The contract's host must be substituted, not written into the text."""
    template = agent_runner._AGENT_CONTRACT_TEMPLATE
    assert "$HOST" in template and "$BASE_URL" in template
    assert not PERSONAL.search(template)


def test_the_oneoff_contract_carries_no_hostname_of_its_own():
    template = oneoff._ONEOFF_CONTRACT_TEMPLATE
    assert "$HOST" in template
    assert not PERSONAL.search(template)


def test_the_contracts_still_come_out_substituted():
    """Template-shaped source is only correct if the value still lands."""
    for contract in (agent_runner.AGENT_CONTRACT, oneoff.ONEOFF_CONTRACT):
        assert config.HOST_LABEL in contract
        assert "$HOST" not in contract
        assert "$BASE_URL" not in contract


def test_substituting_the_contract_leaves_its_json_shape_alone():
    """The contract is mostly a JSON example; `{}`-formatting would eat it."""
    contract = agent_runner.AGENT_CONTRACT
    assert '"summary": [' in contract
    assert '"todo_updates": {"add"' in contract


def test_config_exposes_the_site_under_its_long_standing_names():
    assert config.HOST_LABEL == config.SITE.host
    assert config.SSH_USER == config.SITE.ssh_user
    assert config.PORT == config.SITE.port
    assert config.PREVIEW_PORT == config.SITE.preview_port
    assert config.PREVIEW_HTTPS_PORT == config.SITE.preview_https_port
    assert config.OWNER == config.SITE.owner


def test_the_ssh_command_is_built_from_the_site_config():
    command = config.ssh_command("demo")
    assert command.startswith(f"ssh {config.SITE.handle} -t ")
    assert "demo" in command


def test_the_example_config_documents_every_field():
    """A field nobody can discover is a field nobody sets."""
    example = (config.APP_ROOT / "portal.example.toml").read_text(encoding="utf-8")
    for field in site.Site.__dataclass_fields__:
        assert re.search(rf"^#\s*{field}\s*=", example, re.MULTILINE), field
