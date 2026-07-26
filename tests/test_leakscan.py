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


def test_the_personal_config_is_not_scanned_but_the_example_is_shipped():
    names = {p.name for p in leakscan.files()}
    assert "portal.toml" not in names
    assert "portal.example.toml" not in names  # documents keys by showing values


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
