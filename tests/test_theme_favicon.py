"""The tab icon follows the reader's theme.

Todo #664, the half of Wes's per-theme-icon idea that is actually reachable.
He asked (2026-07-28, about Karli's theme) for "all of the functional pieces
still there, but she can change how they appear" - and then supplied app icons
so the installed icon could follow a theme. Half of that turned out to be
impossible: iOS reads `apple-touch-icon` once, at Add-to-Home-Screen time, so a
home-screen icon can only ever freeze whatever theme was on that day. The tab
favicon has no such rule, and it needs no artwork from anybody, because the
mark is three colors - a tile and two rings - and every theme already names all
three in its palette block.

So the generator reads them out of the stylesheets. The load-bearing claim of
this file is that **the stylesheets are the only place those colors live**: a
copy in config.py would drift from the page the first time somebody nudged a
hue, and drift is invisible at 16px. `test_every_shipped_mark_still_matches_its
_palette` is what makes that true rather than intended - it redraws every icon
from today's CSS and compares pixels with what is committed.

The other risk this pins is the failure mode of a missing icon: a browser that
has been offered an icon and gets a 404 shows NO icon. It does not fall back to
the next `<link>`. So `favicon_url` checks the file is on disk, and both sides
of the naming rule - the generator's and the server's - are held against each
other here.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import config, db

ROOT = Path(config.BASE_DIR)
STATIC = ROOT / "app" / "static"
TEMPLATES = ROOT / "app" / "templates"

THEMES = [name for name, _label in config.APPEARANCE_CHOICES["ui_theme"]]
DEFAULT_THEME = config.APPEARANCE_DEFAULTS["ui_theme"]


def _make_icons():
    """Import deploy/make_icons.py, which is not a package. Skips when Pillow
    is absent: it is a build-time dependency of that one script and
    deliberately not in requirements.txt, so the portal can serve these icons
    on a box that could never have drawn them."""
    pytest.importorskip("PIL")
    path = ROOT / "deploy" / "make_icons.py"
    spec = importlib.util.spec_from_file_location("make_icons", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


# --------------------------------------------------------------------------
# Reading the palettes out of the stylesheets
# --------------------------------------------------------------------------

def test_every_theme_gets_a_palette_and_no_two_are_alike():
    """A theme with no mark colors of its own would inherit the terminal's and
    ship a tab icon that is a lie about the page. Every shipped theme restates
    all three, so every triple must be distinct - and the day one is not, this
    fails rather than quietly shipping two identical favicons."""
    mod = _make_icons()
    palettes = mod.mark_palettes()
    assert set(palettes) == set(THEMES)
    seen = {}
    for name, palette in palettes.items():
        assert len(palette) == 3, palette
        for component in palette:
            assert len(component) == 3 and all(0 <= v <= 255 for v in component)
        assert palette not in seen.values(), f"{name} draws the same mark as {seen}"
        seen[name] = palette


def test_the_palette_really_comes_from_the_stylesheet():
    """Not from a table in config.py that happens to agree today. Nudge a hue
    in a copy of themes.css and the icon has to move with it."""
    mod = _make_icons()
    before = mod.mark_palettes()["meadow"]

    css = (STATIC / "themes.css").read_text()
    patched = css.replace(
        "--portal-blue: hsl(150, 40%, 32%);", "--portal-blue: hsl(0, 100%, 50%);"
    )
    assert patched != css, "the meadow palette moved - update this test's needle"

    class _Sheet:
        def __init__(self, text):
            self.text = text

    real_read = Path.read_text

    def fake_read(self, *a, **kw):
        return patched if self.name == "themes.css" else real_read(self, *a, **kw)

    Path.read_text = fake_read
    try:
        after = mod.mark_palettes()["meadow"]
    finally:
        Path.read_text = real_read

    assert after[1] == (255, 0, 0), after
    assert after[0] == before[0], "only the ring should have moved"


def test_an_unreadable_color_raises_instead_of_falling_back():
    """A silent fallback would ship a favicon in the wrong theme's colors, and
    nothing downstream can see a 16px icon being subtly wrong."""
    mod = _make_icons()
    assert mod._rgb("#0d1016") == (13, 16, 22)
    assert mod._rgb("hsl(220, 25%, 7%)") == (13, 16, 22)
    with pytest.raises(ValueError, match="cannot read"):
        mod._rgb("color-mix(in srgb, var(--ansi-blue) 50%, transparent)")


def test_a_nested_block_does_not_truncate_the_palette():
    """`_block` counts braces rather than scanning to the next `}`. A palette
    block that grew an @media or an & would otherwise return its first half -
    which still parses, so the icons would come out of a partial palette with
    no error anywhere."""
    mod = _make_icons()
    css = "body.theme-x { --a: 1; @media (x) { --b: 2; } --c: 3; }\nbody.other { --d: 4; }"
    got = mod._block(css, "body.theme-x")
    assert "--c: 3" in got and "--d: 4" not in got
    with pytest.raises(ValueError, match="no `body.nope` block"):
        mod._block(css, "body.nope")


# --------------------------------------------------------------------------
# What is committed
# --------------------------------------------------------------------------

def test_every_theme_has_a_full_set_of_tab_icons_committed():
    from app import main

    for theme in THEMES:
        for base in main.THEMED_ICONS:
            path = STATIC / main.themed_icon_name(base, theme)
            assert path.exists(), f"{path.name} is missing - re-run make_icons.py"
            assert path.stat().st_size > 0, path.name


def test_every_shipped_mark_still_matches_its_palette():
    """The one that makes 'the stylesheet is the source' true rather than
    intended: redraw each icon from today's CSS and compare pixels with the
    committed file. Somebody who shifts a theme's `--portal-blue` and does not
    re-run the generator fails here, not on Wes's tab.

    Pixels rather than bytes, so a Pillow upgrade re-encoding a PNG does not
    read as a stale icon.
    """
    Image = pytest.importorskip("PIL.Image", reason="Pillow is build-time only")
    mod = _make_icons()
    from app import main

    for theme, palette in mod.mark_palettes().items():
        raster, svg_of = mod.mark_for(theme)
        for base, edge in mod.MARK_SIZES.items():
            name = main.themed_icon_name(base, theme)
            shipped = Image.open(STATIC / name).convert("RGB")
            assert shipped.size == (edge, edge), name
            want = raster(edge, palette)
            assert list(shipped.getdata()) == list(want.getdata()), (
                f"{name} is stale - run venv/bin/python deploy/make_icons.py"
            )
        svg = STATIC / main.themed_icon_name("favicon.svg", theme)
        assert svg.read_text() == svg_of(palette), f"{svg.name} is stale"


def test_the_unsuffixed_files_are_the_default_theme_untouched():
    """They are the fallback `favicon_url` reaches for, and what a page cached
    from before this change keeps asking for, so they have to stay the shipped
    terminal mark rather than becoming whichever theme was generated last."""
    Image = pytest.importorskip("PIL.Image", reason="Pillow is build-time only")
    mod = _make_icons()
    from app import main

    for base in mod.MARK_SIZES:
        plain = Image.open(STATIC / base).convert("RGB")
        themed = Image.open(STATIC / main.themed_icon_name(base, DEFAULT_THEME))
        assert list(plain.getdata()) == list(themed.convert("RGB").getdata()), base
    assert (STATIC / "favicon.svg").read_text() == (
        STATIC / main.themed_icon_name("favicon.svg", DEFAULT_THEME)
    ).read_text()


def test_the_generator_and_the_server_name_files_the_same_way():
    """The rule is restated in app/main.py rather than shared, because the
    generator imports Pillow and must never be on the serving path. This is the
    only thing that makes a restatement safe."""
    mod = _make_icons()
    from app import main

    for base in ("favicon-32.png", "favicon-16.png", "favicon.svg"):
        for theme in THEMES:
            assert mod.themed_name(base, theme) == main.themed_icon_name(base, theme)
    assert main.themed_icon_name("favicon.svg", "paper") == "favicon-paper.svg"
    assert main.themed_icon_name("favicon-32.png", "paper") == "favicon-32-paper.png"


# --------------------------------------------------------------------------
# A second mark, drawn from the same palette
#
# The office node's walmart theme wears Walmart's spark on its tab rather than
# the two portals. That is a different DRAWING, not a different source of
# colors: the table in the generator says which theme wears which mark, and
# every mark reads the same three palette variables out of the stylesheet, so
# the load-bearing claim above - the CSS is the only place the colors live -
# survives a theme that does not look like the rest.
# --------------------------------------------------------------------------

def _palette():
    """A synthetic triple with three colors nothing could confuse: the tile,
    the left/blue ring, the right/orange ring (which the spark uses)."""
    return ((0, 0, 0), (0, 0, 255), (255, 0, 0))


def test_every_theme_wears_the_rings_unless_the_table_says_otherwise():
    mod = _make_icons()
    assert mod.mark_for(DEFAULT_THEME) == (mod.portal_mark, mod.mark_svg)
    assert mod.mark_for("walmart") == (mod.spark_mark, mod.spark_svg)
    for theme in THEMES:
        if theme not in mod.THEME_MARK:
            assert mod.mark_for(theme) == (mod.portal_mark, mod.mark_svg), theme


def test_the_mark_table_names_only_shipped_themes_and_drawn_marks():
    """A theme renamed out from under the table would silently go back to the
    rings; a mark name with no drawing behind it would be a KeyError in the
    middle of a generator run, with half the icons written."""
    mod = _make_icons()
    for theme, mark in mod.THEME_MARK.items():
        assert theme in THEMES, theme
        assert mark in mod.MARKS, mark
    assert mod.DEFAULT_MARK in mod.MARKS


def test_an_unknown_mark_name_raises_rather_than_shipping_the_rings(monkeypatch):
    mod = _make_icons()
    monkeypatch.setitem(mod.THEME_MARK, "walmart", "starburst")
    with pytest.raises(ValueError, match="unknown mark 'starburst'"):
        mod.mark_for("walmart")


def test_the_spark_is_six_rays_of_one_color_around_an_empty_center():
    """Sampled at the geometry the generator says it draws: the tip of every
    ray is the spark color, the middle of every gap between rays is the tile,
    the center is the tile, and the palette's middle color - the blue ring the
    spark has no use for - never appears. A two-tone spark would be a
    different mark from the one on the page."""
    import math

    mod = _make_icons()
    tile, blue, spark = _palette()
    img = mod.spark_mark(64, _palette())
    px = img.load()
    assert px[0, 0] == tile and px[32, 32] == tile
    for i in range(mod.SPARK_RAYS):
        a = math.radians(i * 360 / mod.SPARK_RAYS)
        gap = math.radians((i + 0.5) * 360 / mod.SPARK_RAYS)
        # Radius 22 is well inside a ray, which runs from about 8.6 to 30.
        tip = (round(32 - 22 * math.sin(-a)), round(32 - 22 * math.cos(a)))
        between = (round(32 - 22 * math.sin(-gap)), round(32 - 22 * math.cos(gap)))
        assert px[tip] == spark, (i, tip, px[tip])
        assert px[between] == tile, (i, between, px[between])
    assert blue not in set(img.getdata())


def test_the_spark_svg_is_the_same_six_rays_from_the_same_numbers():
    """The raster and the SVG share SPARK_RAY and the `i * 60` turn, and the
    SVG has to say so in its own terms: one path per ray carrying that very
    string, turned about the center, in the spark color on the tile."""
    mod = _make_icons()
    svg = mod.spark_svg(_palette())
    rays = re.findall(r'<path d="([^"]+)" fill="([^"]+)" transform="rotate\((\d+) 32 32\)"/>', svg)
    assert len(rays) == mod.SPARK_RAYS
    assert {r[0] for r in rays} == {mod.SPARK_RAY}
    assert {r[1] for r in rays} == {"rgb(255,0,0)"}
    assert [int(r[2]) for r in rays] == [i * 60 for i in range(6)]
    assert 'rx="10" fill="rgb(0,0,0)"' in svg
    assert "rgb(0,0,255)" not in svg


def test_the_ray_is_the_brands_tapered_ray_not_a_capsule():
    """The unrotated ray points straight up from the center: its tip is on
    the axis at radius 30, its inner end on the axis at about radius 8.6, it
    is mirror-symmetric about that axis, and it is narrower at the inner end
    than at the cap - the taper that makes it the brand's spark and not the
    portal's earlier six-capsule stand-in."""
    mod = _make_icons()
    pts = mod._flatten_ray()
    assert pts[0] == pts[-1] == (32.0, 2.0)
    assert 23 < max(y for _, y in pts) < 24
    xs = [x for x, _ in pts]
    assert abs(max(xs) - 32) == pytest.approx(abs(32 - min(xs)), abs=0.02)
    at_cap = max(x for x, y in pts if y < 8) - min(x for x, y in pts if y < 8)
    at_inner = max(x for x, y in pts if y > 20) - min(x for x, y in pts if y > 20)
    assert at_inner < at_cap


def test_a_corrupted_ray_string_raises_instead_of_drawing_something(monkeypatch):
    mod = _make_icons()
    monkeypatch.setattr(mod, "SPARK_RAY", "M 32,2 L 40,20 Z")
    with pytest.raises(ValueError, match="unexpected token 'L'"):
        mod._flatten_ray()


def test_the_walmart_tab_is_the_spark_and_not_the_rings():
    """The one that proves the table is consulted rather than merely present:
    the committed walmart icon matches the spark drawn from its palette and
    differs from the rings drawn from the same palette."""
    Image = pytest.importorskip("PIL.Image", reason="Pillow is build-time only")
    mod = _make_icons()
    from app import main

    palette = mod.mark_palettes()["walmart"]
    shipped = Image.open(STATIC / main.themed_icon_name("favicon-32.png", "walmart"))
    shipped = list(shipped.convert("RGB").getdata())
    assert shipped == list(mod.spark_mark(32, palette).getdata())
    assert shipped != list(mod.portal_mark(32, palette).getdata())
    svg = (STATIC / main.themed_icon_name("favicon.svg", "walmart")).read_text()
    assert svg == mod.spark_svg(palette) and "<ellipse" not in svg


# --------------------------------------------------------------------------
# What the page asks for
# --------------------------------------------------------------------------

def test_the_page_links_the_readers_theme(client):
    db.set_setting("ui_theme", "paper")
    html = client.get("/").text
    for base in ("favicon-32.png", "favicon-16.png", "favicon.svg"):
        from app import main

        assert main.themed_icon_name(base, "paper") in html, base
    # ...and not the terminal ones it replaced. The `-paper` names contain the
    # plain ones as substrings, so this has to look at the href, not the page.
    hrefs = re.findall(r'<link rel="icon"[^>]*href="([^"]+)"', html)
    plain = [h for h in hrefs if re.search(r"/static/favicon(-\d+)?\.(png|svg)\?", h)]
    assert plain == [], plain


def test_an_unknown_theme_serves_the_shipped_mark_rather_than_a_404(client, monkeypatch):
    """A 404 on an icon is a blank tab: a browser offered an icon that fails to
    load does not go looking at the next <link>. So a theme with no set of its
    own - one added to config before the generator is re-run - has to land on
    the terminal files."""
    from app import main

    monkeypatch.setattr(main, "theme", lambda: "no-such-theme")
    assert main.favicon_url("favicon-32.png").startswith("/static/favicon-32.png?")
    assert main.favicon_url("favicon.svg").startswith("/static/favicon.svg?")


def test_the_ico_is_not_themed():
    """It is also what /favicon.ico answers at the origin root, where there is
    no page and so no reader to have a theme. One set of bytes for both beats
    theming the format a browser only reaches once the two PNGs and the SVG
    have all failed."""
    html = (TEMPLATES / "base.html").read_text()
    line = next(ln for ln in html.splitlines() if "favicon.ico" in ln and "<link" in ln)
    assert "icon_url('favicon.ico')" in line, line
    assert "favicon_url(" not in line, line


def test_every_themed_link_tells_the_preview_which_file_it_is():
    """The live preview moves each icon link by looking up
    `data-icon-base` - it cannot parse the name back out of an href that
    carries a mtime version and a boot id. A link that grows a themed URL and
    no marker would silently stop following the preview."""
    from app import main

    html = (TEMPLATES / "base.html").read_text()
    marked = set(re.findall(r'<link[^>]*data-icon-base="([^"]+)"', html))
    assert marked == set(main.THEMED_ICONS), marked
    for line in html.splitlines():
        if "<link" in line and "favicon_url(" in line:
            assert "data-icon-base=" in line, line


# --------------------------------------------------------------------------
# The live preview on the settings page
# --------------------------------------------------------------------------

def test_the_settings_page_hands_the_preview_a_url_per_theme(client):
    """Server-built, because only this side knows which themes have an icon on
    disk. A preview that composed the name itself would swap the tab to a 404
    for any theme the generator had not been run for."""
    from app import main

    html = client.get("/settings").text
    raw = re.search(r"data-theme-favicon='([^']+)'", html)
    assert raw, "the theme field lost its favicon map"
    table = json.loads(raw.group(1).replace("&#34;", '"'))
    assert set(table) == set(THEMES)
    for theme, icons in table.items():
        assert set(icons) == set(main.THEMED_ICONS), theme
        for base, url in icons.items():
            name = url.split("?")[0].rsplit("/", 1)[-1]
            assert name == main.themed_icon_name(base, theme), url
            assert (STATIC / name).exists(), url


def test_the_preview_swaps_every_icon_link_not_just_the_png():
    """Chrome prefers the SVG link, so moving only the PNG would leave the tab
    on the old mark in the browser most likely to be showing it."""
    js = (ROOT / "app" / "static" / "app.js").read_text()
    assert "link[data-icon-base]" in js
    assert "themeFavicon" in js
