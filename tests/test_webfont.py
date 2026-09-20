"""The webfont is this origin's, not Google's (agent-filed, 2026-09-20).

Site-wide tools' `third-party-watch` found four public pages on Wes's estate
asking `fonts.googleapis.com` for a stylesheet before they rendered, and
traced every one of them back here: the portal's own `base.html`, the `/style`
page's starter skeleton a project copies, the header comment of
`terminal-theme.css`, and the `terminal-style` skill. 51 projects read those
last two, so the documentation copies matter as much as the code.

Three costs, none of them visible from inside the page:

1. Google is handed the visitor's IP, user agent and - in the `Referer` - the
   exact page they opened, before anything draws. One of the four pages was
   the sign-in page every person on that estate passes through, so Google was
   handed the IP of every visitor to every app there on their way in.
2. A stylesheet is render-blocking, so Google's outage is the page's outage.
3. It forces `style-src https://fonts.googleapis.com` and
   `font-src https://fonts.gstatic.com` into any CSP, and a grant that exists
   is a grant that can be used.

And a quieter fourth: a `<link rel="preconnect">` opens a DNS lookup, a TCP
connection and a TLS handshake to Google on *every* page load whether or not
anything is ever fetched over it - so deleting the stylesheet line without
deleting the preconnects keeps announcing every visit after the font has gone.
That is why the sweep below greps for the hostnames rather than for the
stylesheet `<link>`.
"""
from __future__ import annotations

import hashlib
import re

import pytest
from starlette.testclient import TestClient

from app import config, vendorstatic

APP = config.APP_ROOT / "app"
STATIC = APP / "static"
VENDOR = STATIC / "vendor" / "fira-code-v27"
SKILL = APP / "skills" / "terminal-style" / "SKILL.md"


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


# Byte-for-byte the shared webfont directory this install was handed, which is
# gstatic's v27 fetched with a browser User-Agent on purpose: that endpoint
# content-negotiates on User-Agent, and a default python client is handed a
# TrueType stylesheet naming .ttf URLs that are not in this directory. Pinned
# here because a truncated or re-fetched .woff2 is a file that still parses as
# "a font" and renders as tofu, with no error anywhere to notice it by.
EXPECTED_SHA256 = {
    "font.css": "c5f5e22e125c567a777b8ebf0a242f71fab1993c564ec42aad738c6b25dd0bbe",
    "uU9NCBsR6Z2vfE9aq3bh09SDulI.woff2": "801677342d1191c5e964719bbcb5834f5da3c39a00e1e1501f450b1379fcc116",
    "uU9NCBsR6Z2vfE9aq3bh0NSDulI.woff2": "6d7c26d616350953ed751b3ba96aa1c2154b57e09effa463abb6005c2eb4b6ef",
    "uU9NCBsR6Z2vfE9aq3bh0dSDulI.woff2": "dc6cb5824a57bcf2be92811dfde22c8e3ad701a8f29bb1c9f82c7b2e64a52eb0",
    "uU9NCBsR6Z2vfE9aq3bh2dSDulI.woff2": "b0ea81e7827ccb1e8258b911e632b694e898fe15665fab3ed0d12bc565288a34",
    "uU9NCBsR6Z2vfE9aq3bh3dSD.woff2": "771bf4b79a97fc005d12866168bd39d868a9dd3d5903008fe8b796723c8a56f4",
    "uU9NCBsR6Z2vfE9aq3bh3tSDulI.woff2": "2f0318b51ce6418b00b597868aa877c93b272bfdbc419e404c2fece09f488cae",
    "uU9NCBsR6Z2vfE9aq3bhZ_Wmh2uX.woff2": "a0599035b56206d09bc1873c2a025c647f8fb5cd7c0921999aeff7435997eb7d",
}

FONT_URL = "/static/vendor/fira-code-v27/font.css"


# --- the bytes are here and are the right bytes ---------------------------


def test_the_font_directory_is_vendored():
    assert VENDOR.is_dir(), "the vendored font is missing - restore it from git history"
    assert (VENDOR / "font.css").is_file()


def test_every_vendored_file_matches_its_published_checksum():
    for name, expected in EXPECTED_SHA256.items():
        got = hashlib.sha256((VENDOR / name).read_bytes()).hexdigest()
        assert got == expected, f"{name} is not the published v27 file"


def test_the_directory_holds_exactly_the_published_set_and_nothing_else():
    """All seven subsets, no strays.

    Seven, not one: `unicode-range` means a browser downloads only the subsets
    it actually has to render, so the six non-Latin files cost a visitor
    nothing - and dropping them renders a non-Latin character as tofu with no
    error anywhere. A stray file matters too, because everything in a served
    directory is downloadable by name.
    """
    assert {p.name for p in VENDOR.iterdir()} == set(EXPECTED_SHA256)
    assert sum(1 for p in VENDOR.iterdir() if p.suffix == ".woff2") == 7


def test_every_url_inside_font_css_resolves_inside_the_directory():
    """The `url()`s are relative, so the directory is one self-contained unit.

    Flattening it, or copying `font.css` without its `.woff2` neighbors, gives
    a page that 404s seven fonts and silently falls back to system monospace.
    """
    css = (VENDOR / "font.css").read_text(encoding="utf-8")
    urls = re.findall(r"url\(([^)]+)\)", css)
    assert urls
    for raw in urls:
        url = raw.strip("'\"")
        assert not url.startswith(("http:", "https:", "//", "/")), url
        assert (VENDOR / url).is_file(), url


def test_the_font_css_covers_the_weights_the_theme_uses():
    """400/500/600/700 - the same four the Google link used to request."""
    css = (VENDOR / "font.css").read_text(encoding="utf-8")
    assert set(re.findall(r"font-weight:\s*(\d+)", css)) == {"400", "500", "600", "700"}
    assert "font-family: 'Fira Code'" in css
    # Without `swap` the text is invisible while the font loads, which on a
    # slow connection is a blank page rather than a styled one.
    assert "font-display: swap" in css


# --- nothing in the tree reaches for Google any more -----------------------


GOOGLE_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com")


def _sources_that_ship_to_a_browser_or_a_project():
    """Everything that could put a Google font link into a page.

    The templates are the portal's own pages; the CSS header comment and the
    skill are what the other 51 projects read and copy. This test file names
    the hostnames itself, hence the exclusion.
    """
    yield from (APP / "templates").rglob("*.html")
    yield from STATIC.rglob("*.css")
    yield from STATIC.rglob("*.js")
    yield from (APP / "skills").rglob("*.md")


@pytest.mark.parametrize("path", sorted(_sources_that_ship_to_a_browser_or_a_project()), ids=str)
def test_no_shipped_file_mentions_a_google_font_host(path):
    text = path.read_text(encoding="utf-8")
    for host in GOOGLE_HOSTS:
        for line in text.splitlines():
            if host not in line:
                continue
            # The skill and the CSS header say "never link this", by name.
            # That is the documentation doing its job; a `<link>`,
            # `preconnect` or `@import` naming the host is the defect.
            assert not re.search(r"<link|@import|preconnect|url\(", line), f"{path}: {line.strip()}"


def test_the_portal_pages_link_the_vendored_font(client):
    html = client.get("/").text
    assert FONT_URL in html
    assert "fonts.googleapis.com" not in html
    assert "fonts.gstatic.com" not in html


def test_the_style_page_links_the_vendored_font(client):
    html = client.get("/style").text
    assert FONT_URL in html
    assert "preconnect" not in html


def test_the_starter_skeleton_a_project_copies_names_the_relative_font(client):
    """The skeleton is copied into a project's own docroot, so its font path
    is relative to the page, not to the portal."""
    html = client.get("/style").text
    skeleton = html.split('id="starter"', 1)[1].split("</pre>", 1)[0]
    assert "vendor/fira-code-v27/font.css" in skeleton
    assert "googleapis" not in skeleton


def test_the_font_link_carries_no_cache_busting_query(client):
    """`static_url()` would append `?v=<mtime>`, which would re-fetch the font
    on every redeploy for nothing. The version is in the directory name."""
    for page in ("/", "/style"):
        assert f"{FONT_URL}?" not in client.get(page).text


# --- the skill and the CSS header, which 51 projects read ------------------


def test_the_skill_tells_a_project_where_the_font_is_and_to_take_all_of_it():
    text = SKILL.read_text(encoding="utf-8")
    assert "$PORTAL_ROOT/app/static/vendor/fira-code-v27" in text
    assert "$BASE_URL/static/vendor/fira-code-v27/font.css" in text
    assert "Never link `fonts.googleapis.com`" in text
    assert "preconnect" in text
    assert "unicode-range" in text


def test_every_path_the_skill_hands_a_project_actually_exists():
    """`$PORTAL_ROOT` is substituted into this install's checkout root at sync
    time, so every path the skill names is a real `cp` an agent will run. A
    recipe naming a path that is not there fails in someone else's workspace,
    hours from here, and reads as the portal being broken."""
    text = SKILL.read_text(encoding="utf-8")
    named = re.findall(r"\$PORTAL_ROOT(/[\w./-]+)", text)
    assert named, "the skill no longer tells anyone where the files are"
    for rel in named:
        assert (config.APP_ROOT / rel.lstrip("/")).exists(), f"$PORTAL_ROOT{rel}"


def test_every_portal_url_the_skill_names_answers(client):
    """Same for the served addresses: `$BASE_URL/...` is a link handed to
    another project's agent, and a 404 there is a dead end it cannot debug."""
    text = SKILL.read_text(encoding="utf-8")
    named = {m for m in re.findall(r"\$BASE_URL(/[\w./-]+)", text)}
    assert named
    for path in named:
        assert client.get(path).status_code == 200, path


def test_the_theme_css_header_points_at_the_self_hosted_font():
    header = (STATIC / "terminal-theme.css").read_text(encoding="utf-8").split("*/", 1)[0]
    assert 'href="vendor/fira-code-v27/font.css"' in header
    assert "/static/vendor/fira-code-v27/" in header
    assert "Do NOT link fonts.googleapis.com" in header


# --- immutable caching, which is the whole reason for the version in the name


@pytest.mark.parametrize(
    "rel, pinned",
    [
        ("vendor/fira-code-v27/font.css", True),
        ("vendor/fira-code-v27/uU9NCBsR6Z2vfE9aq3bh3dSD.woff2", True),
        ("vendor/pdfjs-v4.10.38/pdf.mjs", True),
        # Nested deeper is still inside the pinned directory.
        ("vendor/fira-code-v27/subset/extra.woff2", True),
        # No version in the name: an edit in place would be invisible to a
        # browser holding a year-old copy, so it gets ordinary caching.
        ("vendor/fira-code/font.css", False),
        ("vendor/fonts/font.css", False),
        # A version that is not a released one.
        ("vendor/fira-code-v27-scratch/font.css", False),
        ("vendor/fira-code-vnext/font.css", False),
        # A bare file directly in vendor/ has no directory name to pin it -
        # including one whose own name carries a version. The unit that gets
        # frozen is a directory, because that is what an upgrade replaces.
        ("vendor/anything.css", False),
        ("vendor/fira-code-v27", False),
        ("vendor/pdfjs-v4.10.38", False),
        ("vendor", False),
        # The portal's own assets change in place on every self-modifying run.
        ("terminal-theme.css", False),
        ("style.css", False),
        ("icons/favicon.svg", False),
        # Not the vendor directory at all, however similar it reads.
        ("vendored/fira-code-v27/font.css", False),
        ("app/vendor/fira-code-v27/font.css", False),
    ],
)
def test_is_version_pinned(rel, pinned):
    assert vendorstatic.is_version_pinned(rel) is pinned


def test_the_served_font_is_cached_for_a_year(client):
    r = client.get(FONT_URL)
    assert r.status_code == 200
    assert r.headers["cache-control"] == vendorstatic.IMMUTABLE_CACHE_CONTROL
    assert "max-age=31536000" in r.headers["cache-control"]
    assert "immutable" in r.headers["cache-control"]


def test_the_served_woff2_is_cached_for_a_year_too(client):
    r = client.get("/static/vendor/fira-code-v27/uU9NCBsR6Z2vfE9aq3bh3dSD.woff2")
    assert r.status_code == 200
    assert r.headers["cache-control"] == vendorstatic.IMMUTABLE_CACHE_CONTROL
    assert r.content == (VENDOR / "uU9NCBsR6Z2vfE9aq3bh3dSD.woff2").read_bytes()


def test_the_portals_own_stylesheet_is_not_frozen(client):
    """A self-modifying run changes style.css in place; freezing it for a year
    would mean the next reload never showed the change."""
    r = client.get("/static/terminal-theme.css")
    assert r.status_code == 200
    assert "immutable" not in r.headers.get("cache-control", "")


def test_a_conditional_request_for_a_frozen_file_still_says_it_is_frozen(client):
    """The 304 is the browser's chance to learn it never needed to ask."""
    first = client.get(FONT_URL)
    again = client.get(FONT_URL, headers={"if-none-match": first.headers["etag"]})
    assert again.status_code == 304
    assert again.headers["cache-control"] == vendorstatic.IMMUTABLE_CACHE_CONTROL


def test_the_font_is_served_as_css_and_the_subsets_as_fonts(client):
    assert client.get(FONT_URL).headers["content-type"].startswith("text/css")
    woff = client.get("/static/vendor/fira-code-v27/uU9NCBsR6Z2vfE9aq3bh3dSD.woff2")
    assert woff.headers["content-type"] in ("font/woff2", "application/font-woff2")


def test_every_font_file_the_css_names_is_actually_reachable(client):
    """End to end: the exact relative URLs a browser will resolve off
    /static/vendor/fira-code-v27/font.css all answer 200."""
    css = client.get(FONT_URL).text
    urls = {u.strip("'\"") for u in re.findall(r"url\(([^)]+)\)", css)}
    assert len(urls) == 7
    for url in urls:
        r = client.get(f"/static/vendor/fira-code-v27/{url}")
        assert r.status_code == 200, url
        assert r.content[:4] == b"wOF2", url


def test_the_vendor_mount_did_not_shadow_the_rest_of_static(client):
    """One mount serves both, so this is the regression guard for ever
    splitting it into two: a mount registered before /static would swallow it."""
    assert client.get("/static/terminal-theme.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/vendor/fira-code-v27/nope.woff2").status_code == 404
