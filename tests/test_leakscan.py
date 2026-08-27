"""The standing guard against republishing somebody's infrastructure.

The one-off scrub was the easy half. This is the part that has to keep working:
without it the next feature quietly writes a hostname into a comment and nobody
notices until it is public.

Two of these tests exist because the scanner reported a clean tree that was not
clean. Both bugs were found by grepping the staged output by hand *after* the
scan passed, which is the reason `deploy/publish.py` is not the only check
between this repo and a public one.
"""
from __future__ import annotations

from app import config, leakscan, site


def _site(**over) -> site.Site:
    return site.Site(**{**site.defaults(), **over})


# --- what counts as personal ------------------------------------------------

def test_the_needles_come_from_the_installation_not_a_list():
    """The point of the design: no hard-coded list of one person's machines,
    so the guard protects whoever installs this next."""
    words = leakscan.needles(_site(host="ada-box", render_host="ada@render.example.com"))
    assert "ada-box" in words
    assert "render.example.com" in words


def test_a_url_yields_its_host_and_not_its_scheme_or_port():
    """Splitting the whole address on punctuation gave `http` and `8500` as
    needles, which between them matched most lines of a web application and
    buried the real findings under three hundred false ones."""
    words = leakscan.needles(_site(render_portal_url="http://10.0.0.21:8500/x"))
    assert "10.0.0.21" in words
    assert "http" not in words and "8500" not in words


def test_the_home_directory_is_infrastructure_even_though_the_name_is_not():
    """A comment saying "Ada asked for this" is history worth keeping. A README
    telling a stranger to `cd /home/ada/project-portal` is configuration, and
    wrong for them."""
    words = leakscan.needles(_site(ssh_user="ada"))
    assert "/home/ada" in words
    assert "ada" not in words  # the bare login is not a needle


def test_a_too_common_login_is_not_a_needle():
    """An install running as `root` or `admin` must not report every line of
    its own source as a leak."""
    assert "/home/root" not in leakscan.needles(_site(ssh_user="root"))


def test_no_identity_matches_nothing_rather_than_everything():
    """An empty alternation matches at every position. A portal that knows
    nothing about itself must report no leaks, not all of them."""
    pattern = leakscan.compile_needles([])
    assert not pattern.search("anything at all")


# --- the two bugs found by hand ---------------------------------------------

def test_a_path_needle_matches_where_paths_are_actually_written():
    """The regression that matters most. Wrapping every needle in `\\b` asserts
    a word character immediately before the leading "/", so "/home/ada" never
    matched " /home/ada" - the only way anyone writes it. The scan reported a
    clean tree while six such paths sat in the README."""
    pattern = leakscan.compile_needles(["/home/ada"])
    assert pattern.search("cd /home/ada/project-portal")
    assert pattern.search("WorkingDirectory=/home/ada/project-portal")


def test_a_short_hostname_does_not_match_inside_a_longer_word():
    """The reason the boundaries are there at all."""
    pattern = leakscan.compile_needles(["box"])
    assert not pattern.search("run it in a sandbox")
    assert pattern.search("ssh to box now")


def test_a_hostname_still_matches_inside_a_domain():
    pattern = leakscan.compile_needles(["acme"])
    assert pattern.search("published at acme.com/tools")


# --- what gets scanned ------------------------------------------------------

def test_the_scan_covers_the_whole_publishable_tree():
    parts = {p.relative_to(config.APP_ROOT).parts[0] for p in leakscan.files()}
    assert {"app", "deploy", "tests"} <= parts


def test_gitignored_directories_are_never_scanned():
    """data/ and secrets/ are the personal halves. Scanning them would report
    hundreds of leaks in files that are not published and never were."""
    scanned = {p.relative_to(config.APP_ROOT).parts[0] for p in leakscan.files()}
    assert not scanned & {"data", "secrets", "venv"}


def test_the_personal_config_is_not_read_at_all():
    """It is gitignored, never published, and the one file that is SUPPOSED to
    name its owner."""
    assert "portal.toml" not in {p.name for p in leakscan.files()}


def test_the_example_config_is_read_but_exempt_from_the_identity_half():
    """It ships, so a key pasted into it while editing would ship too - but
    `host = "myserver"` in it is documentation, not a leak. Those are two
    different answers about one file, which is why the exemption is not the
    same list as the skip."""
    assert "portal.example.toml" in {p.name for p in leakscan.files()}
    assert "portal.example.toml" in leakscan.IDENTITY_EXEMPT_NAMES
    assert "portal.example.toml" not in leakscan.SKIP_NAMES


def test_a_file_with_no_extension_is_still_read(tmp_path):
    """`deploy/whisper/Dockerfile` was tracked and read by nothing, because the
    scan matched on suffix alone. It named the author's server in its second
    paragraph, and that is exactly where an ENV line puts a token."""
    (tmp_path / "Dockerfile").write_text("# built for ada-box\n")
    leaks = leakscan.scan(root=tmp_path, words=["ada-box"])
    assert [x.lineno for x in leaks] == [1]


# --- the machine's own addresses --------------------------------------------

def test_the_machines_addresses_are_needles():
    """The hole that let a real LAN address through. `leak_patterns` in the
    author's config named the wifi subnet and not the ethernet one, so the
    server's own address sat in a committed test fixture through every clean
    scan. A hand-written list of your own addresses is wrong the first time an
    interface changes."""
    words = leakscan.needles(_site(), addresses=["10.1.2.3", "100.64.1.2"])
    assert "10.1.2.3" in words and "100.64.1.2" in words


def test_loopback_and_link_local_are_not_needles():
    """They are identical on every machine on earth, so they identify nobody -
    and making 127.0.0.1 a needle would flag most of the test suite."""
    found = leakscan.local_addresses()
    assert "127.0.0.1" not in found
    assert not any(a.startswith(("169.254.", "fe80:")) for a in found)


def test_reading_the_addresses_never_raises_on_a_machine_without_ip(monkeypatch):
    """The scan runs in the test suite, and a suite that cannot run on a
    machine without iproute2 is a suite that gets skipped."""
    leakscan.local_addresses.cache_clear()
    monkeypatch.setattr(leakscan.shutil, "which", lambda _: None)
    try:
        assert leakscan.local_addresses() == ()
    finally:
        leakscan.local_addresses.cache_clear()


# --- credentials, which belong to nobody the config knows about --------------
#
# Every sample below is assembled from two fragments, and the split is not
# decoration: written as one literal, each of these makes THIS file a line the
# scanner refuses to publish - the guard's own test tripping the guard, which is
# how the first run of these tests ended. Splitting the prefix is also the
# honest habit anyway. A key-shaped literal does not belong in a commit whether
# or not it is live, because nobody reading a diff can tell which it is.

def _sample(prefix: str, body: str) -> str:
    return prefix + body


def test_a_real_looking_key_of_each_kind_is_caught():
    """These match no needle at all - a Slack webhook or an AWS pair names none
    of the owner's machines - so without a shape check they publish cleanly."""
    for line in [
        "ANTHROPIC_API_KEY=" + _sample("sk-ant-", "api03-" + "A9b" * 32),
        'key = "' + _sample("sk-", "proj-" + "x7Q" * 16) + '"',
        "token: " + _sample("ghp", "_" + "a1B" * 12),
        "pat = " + _sample("github", "_pat_11ABCDE0Y" + "q9" * 30),
        "SLACK=" + _sample("xox", "b-1234567890-1234567890123-" + "aZ9" * 8),
        "url = " + _sample("https://hooks.slack.com/serv", "ices/T0/B0/" + "ab01" * 5),
        "aws_access_key_id = " + _sample("AKI", "AIOSFODNN7EXAMPLE"),
        "GOOGLE_KEY=" + _sample("AIz", "a" + "b" * 35),
        "BOT=" + _sample("123456789:", "AA" + "Hh9" * 12),
        "TS_AUTHKEY=" + _sample("tskey-", "auth-kABC123DEF-9xyzQRS456"),
        _sample("-----BEGIN OPENSSH PRI", "VATE KEY-----"),
        "jwt = " + _sample("eyJhbGciOiJIUzI1NiJ9", ".eyJzdWIiOiIxMjMifQ.dQw4w9WgXcQab"),
        'password = "' + _sample("Tr0ub4dor3x", 'Kcd936Correct9"'),
    ]:
        assert leakscan.credentials(line), f"not caught: {line[:40]}"


def test_an_obviously_fake_fixture_is_not_a_credential():
    """The bar that keeps this check switched on. The suite is full of strings
    like `sk-ant-in-a-file`, and a scan that stopped a publish over those would
    be disabled within a week."""
    for line in [
        'key_file.write_text("sk-ant-in-a-file")',
        '"access_token": "sk-ant-oat01-new"',
        'result = _ask(project, token="wrong-token")',
        'password = "hunter2"',
        'api_key = os.environ["ANTHROPIC_API_KEY"]',
        'token = "a-plain-english-phrase"',
        'secret = ""',
    ]:
        assert not leakscan.credentials(line), f"false positive: {line[:40]}"


def test_a_fixed_length_key_is_caught_inside_a_longer_run():
    """`\\b` after the last character asserts the next one is NOT a word
    character, so a key concatenated into a URL or a filename matched nothing
    and was published. A false positive costs a glance; a miss costs a key.

    Both fixed-length shapes, not one: the sweep put the boundary back on the
    Google pattern and nothing failed, because this test only ever exercised
    the AWS one.
    """
    assert leakscan.credentials("https://x/" + _sample("AKI", "AIOSFODNN7EXAMPLEZZ") + "/y")
    assert leakscan.credentials("path/" + _sample("AIz", "a" + "b" * 35) + "ZZ.json")


def test_a_credential_is_reported_redacted_and_the_hostname_beside_it_too(tmp_path):
    """A finding is printed to a terminal and pasted into a report, so echoing
    the line whole would copy the key one step further out - and a line holding
    a key often holds the host it belongs to as well."""
    key = "sk-ant-api03-" + "A9b" * 32
    (tmp_path / "conf.py").write_text(f'KEY = "{key}"  # for ada-box\n')
    (leak,) = leakscan.scan(root=tmp_path, words=["ada-box"])
    assert leak.needle == "anthropic-key"
    assert key not in str(leak)
    assert leakscan.REDACTED in leak.line


def test_two_keys_on_one_line_are_both_redacted():
    """Redacting only the shape that matched would report the first and print
    the second."""
    a, b = "sk-ant-api03-" + "A9b" * 32, "ghp_" + "a1B" * 12
    out = leakscan._redact(f"{a} and {b}")
    assert a not in out and b not in out


def test_the_example_config_is_still_checked_for_keys(tmp_path):
    """The half of the exemption that is not exempt: a documented host in it is
    fine, a pasted key is not."""
    (tmp_path / "portal.example.toml").write_text(
        'host = "ada-box"\napi_key = "sk-ant-api03-' + "A9b" * 32 + '"\n'
    )
    (leak,) = leakscan.scan(root=tmp_path, words=["ada-box"])
    assert leak.lineno == 2 and leak.needle == "anthropic-key"


def test_a_private_path_is_excluded_from_the_scan_and_from_publishing():
    """One list, so a file cannot be exempted from the check while still being
    published - the exemption IS the exclusion."""
    scanned = {p.relative_to(config.APP_ROOT).as_posix() for p in leakscan.files()}
    assert leakscan.PRIVATE_PATHS
    assert not (scanned & leakscan.PRIVATE_PATHS)


# --- the check itself -------------------------------------------------------

def test_this_tree_is_publishable_right_now():
    leaks = leakscan.scan()
    assert not leaks, "personal strings in the tree:\n" + "\n".join(str(x) for x in leaks)


def test_the_scan_finds_a_leak_that_is_really_there(tmp_path):
    """Proof the clean result above means something. A scan that cannot fail is
    not evidence of anything."""
    (tmp_path / "sneaky.py").write_text("# deploy to ada-box in the morning\n")
    leaks = leakscan.scan(root=tmp_path, words=["ada-box"])
    assert len(leaks) == 1
    assert leaks[0].lineno == 1 and leaks[0].needle == "ada-box"


def test_an_unreadable_file_is_skipped_rather_than_fatal(tmp_path):
    (tmp_path / "binary.md").write_bytes(b"\xff\xfe\x00 not utf-8")
    assert leakscan.scan(root=tmp_path, words=["anything"]) == []


def test_an_image_cannot_ride_along_unchecked():
    """A screenshot is the one kind of file the text scan cannot read, so a
    picture of somebody's real board would pass a clean scan and be published.
    Images are held back by path instead."""
    assert any(p.endswith(".png") for p in leakscan.PRIVATE_PATHS)


def _strip_comments(text):
    """CSS comments out. A comment is prose, not a declaration.

    Without this the scanner reads the stylesheet's own explanations of how
    custom properties work as uses of them - a comment saying a reference
    "would fall back" flagged an empty name, which reports as "style.css uses
    , which nothing defines" and sends the reader hunting through the rules for
    a typo that is in a sentence. Found the day .rail-pie was written.
    """
    out, at = [], 0
    while True:
        start = text.find("/*", at)
        if start < 0:
            return "".join(out) + text[at:]
        out.append(text[at:start])
        end = text.find("*/", start + 2)
        if end < 0:
            return "".join(out)
        at = end + 2


def _var_uses(text):
    """Every `var(...)` in a stylesheet, as (name, fallback). Scanned with
    balanced parentheses rather than matched with a regex, because a fallback
    is itself often a nested reference and a regex stops at the first `)`."""
    text = _strip_comments(text)
    out = []
    at = 0
    while True:
        at = text.find("var(", at)
        if at < 0:
            return out
        depth, i = 0, at + 3
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        inner = text[at + 4:i]
        name, _, fallback = inner.partition(",")
        out.append((name.strip(), fallback.strip()))
        at = i + 1


def test_the_scanner_reads_rules_and_not_the_prose_around_them():
    """`_strip_comments` is load-bearing for the test below, and a version that
    ate too much would make it pass while scanning nothing at all. So: a real
    use survives, a commented-out one does not, and an unterminated comment
    swallows the rest of the file rather than reopening it."""
    assert _var_uses("a { color: var(--real); } /* var(--prose) */") == [("--real", "")]
    assert _var_uses("/* var(--a) */ b { top: var(--b); } /* var(--c) */") == [("--b", "")]
    assert _var_uses("/* var(--never-closed)") == []
    # A fallback that is itself a reference still comes back whole.
    assert _var_uses("a { top: var(--x, var(--y)); }") == [("--x", "var(--y)")]


def test_no_stylesheet_uses_a_custom_property_nobody_defines():
    """An invalid `var()` makes the property fall back to its inherited or
    initial value with no warning anywhere: `border-color: var(--nope)` paints
    the border in the text color on every theme, which reads as a design choice
    rather than a typo. `--terminal-border` was two such uses in .everyone-who
    and .everyone-list, both meaning `--window-border`.

    A fallback does not make it safe: `var(--border, #1e2a38)` hid the same typo
    behind a hardcoded dark blue, which then painted a border on the two light
    themes too. So the rule is about the NAME - every one has to be defined
    somewhere - and a fallback beside a real name is left alone, because that is
    an ordinary default rather than a mistake.
    """
    import re
    from pathlib import Path

    static = Path(__file__).resolve().parents[1] / "app" / "static"
    sheets = {p: p.read_text(encoding="utf-8") for p in static.glob("*.css")}
    defined = set()
    for text in sheets.values():
        defined |= set(re.findall(r"(--[\w-]+)\s*:", text))

    for path, text in sheets.items():
        for name, fallback in _var_uses(text):
            assert name in defined, f"{path.name} uses {name}, which nothing defines"
            if fallback.startswith("var("):
                inner = _var_uses(fallback)
                assert inner and inner[0][0] in defined, (
                    f"{path.name}: {name} falls back to {fallback}, which nothing defines"
                )
