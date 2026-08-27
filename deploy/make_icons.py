#!/usr/bin/env python3
"""Cut the home-screen app icon and the favicons from Wes's artwork.

The source is an **Apple Icon Composer export** (2026-08-07): the 1024x1024
`Icon-iOS-Default` PNG, kept in `deploy/icon-source/` so this script still runs
in a checkout that has no `data/` directory. This writes the fixed set of sizes
into app/static/ once; the outputs are committed, so Pillow is a build-time
dependency of this script and NOT a runtime dependency of the portal.

Two things about an Icon Composer export have to be undone before it can be an
`apple-touch-icon`, and both are invisible until you look at a home screen:

1. **The rounded-corner mask is already cut out of it.** iOS applies that mask
   AGAIN to an apple-touch-icon and composites whatever is left onto white, so
   transparent corners return as four white pips just outside the squircle.
   `fill_mask_corners` puts the tile's own gradient back into them; iOS then
   cuts the one mask that actually shows.
2. **The whole file is full bleed.** Icon Composer draws a specular rim right
   at the mask boundary, so cropping to "the visible tile" and re-padding would
   move the rim inward and leave a hairline of background around the icon. The
   artwork is used at its native extent and only the corners are repaired.

There is exactly one home-screen icon, whatever the appearance. iOS honors a
single `apple-touch-icon` link for a web app: `media="(prefers-color-scheme:
dark)"` on it is ignored (checked 2026-08-07 - Apple has published nothing, and
the only thing that works is swapping the href in JS, which is read once at
add-to-Home-Screen time and never again). So the Dark, Tinted and Clear
variants of the export have nowhere to go on the web; Default is the one that
ships. Nothing is lost by that here - the artwork is already dark, and the
Dark export is pixel-identical to Default across the tile.

The artwork is NOT used for the 16-32px tab favicon. Downscaled that far the
two words collapse into an unreadable smudge - it is a home-screen icon, drawn
to be looked at at 180px. The tab keeps the drawn two-portal mark (the blue O
and the orange O from the wordmark), which is the same design language and
still reads at 16px; favicon.ico is rendered from those same shapes so the
browsers that probe the origin root get the legible one too.

**The tab icon is drawn once per theme.** The mark is three colors - a tile and
two rings - and every theme already names all three in its palette block, so a
themed favicon needs no new artwork from anybody: `mark_palettes()` reads
`--bg-color`, `--portal-blue` and `--portal-orange` straight out of the
stylesheets. That is deliberately the ONLY place those colors live. Copying
them into config.py would have made a theme's tab icon drift from its page the
first time somebody nudged a hue, and the drift would be invisible at 16px.
Adding a theme therefore stays what themes.css promises it is - a palette block
and four config lines - plus one run of this script.

Usage:  venv/bin/python deploy/make_icons.py [source.png]
"""
from __future__ import annotations

import colorsys
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402  (needs ROOT on sys.path first)

STATIC = ROOT / "app" / "static"
DEFAULT_SOURCE = ROOT / "deploy" / "icon-source" / "Icon-iOS-Default-1024.png"
BG = (13, 16, 22)  # #0d1016, the terminal background

# name -> square edge in px. All cut from Wes's artwork.
PNG_SIZES = {
    "icon-512.png": 512,   # manifest, and the master anything else is cut from
    "icon-192.png": 192,   # manifest / Android home screen
    "apple-touch-icon.png": 180,  # iOS home screen
}
# name -> square edge in px. All drawn, not cut from the artwork.
MARK_SIZES = {
    "favicon-32.png": 32,
    "favicon-16.png": 16,
}
ICO_SIZES = [16, 32, 48]
# There is deliberately no BLUE/ORANGE constant here any more. There was, and
# it had already drifted: it said the rings were #3fa7ff and #ff8a3d while
# style.css's own `--portal-blue`/`--portal-orange` had moved to #33bbff and
# #ff8d29. Nothing could see it, because the drift is two points at 16px. Every
# mark now comes from `mark_palettes()`, which reads the stylesheet.

# The three palette variables the mark is drawn from, in the order
# `portal_mark` wants them: the tile, then the left ring, then the right one.
MARK_VARS = ("--bg-color", "--portal-blue", "--portal-orange")
# Where each theme's palette block is. `terminal` has no block in themes.css on
# purpose - it IS style.css - so it is read from that sheet's `:root`.
STOCK_SHEET = ("style.css", ":root")
THEME_SHEET = "themes.css"


def _rgb(value: str) -> tuple[int, int, int]:
    """One CSS color from a palette block, as 8-bit RGB.

    Only the two notations the stylesheets actually use are accepted - `hsl()`
    and `#rrggbb` - and anything else raises. A silent fallback here would ship
    a favicon in the wrong theme's colors, which nothing downstream can see.
    """
    value = value.strip()
    hsl = re.fullmatch(
        r"hsl\(\s*([\d.]+)\s*,\s*([\d.]+)%\s*,\s*([\d.]+)%\s*\)", value, re.I
    )
    if hsl:
        h, s, ell = (float(g) for g in hsl.groups())
        r, g, b = colorsys.hls_to_rgb(h / 360.0, ell / 100.0, s / 100.0)
        return (round(r * 255), round(g * 255), round(b * 255))
    if re.fullmatch(r"#[0-9a-f]{6}", value, re.I):
        return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))
    raise ValueError(f"cannot read {value!r} as a color - see MARK_VARS")


def _block(css: str, selector: str) -> str:
    """The declarations inside `selector { ... }`.

    Takes the FIRST `{` after the selector and reads to its matching `}` with a
    depth counter rather than to the next `}`. The palette blocks are flat
    today, but a nested `@media` or `&` inside one would make a first-`}` scan
    return half a block - and the half it returned would still parse, so the
    icons would come out of a partial palette with no error anywhere.
    """
    at = re.search(re.escape(selector) + r"\s*\{", css)
    if not at:
        raise ValueError(f"no `{selector}` block in the stylesheet")
    depth, start = 0, at.end() - 1
    for i in range(start, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[start + 1 : i]
    raise ValueError(f"`{selector}` block is never closed")


def mark_palettes() -> dict[str, tuple[tuple[int, int, int], ...]]:
    """theme name -> (tile, left ring, right ring), read from the stylesheets.

    A theme whose palette block does not restate one of MARK_VARS inherits the
    shipped terminal value for it, exactly the way the cascade would - so a
    future theme that only shifts the page color still gets a favicon, and it
    gets the same one the page's own wordmark is wearing.
    """
    stock_css = (STATIC / STOCK_SHEET[0]).read_text()
    themes_css = (STATIC / THEME_SHEET).read_text()
    stock_block = _block(stock_css, STOCK_SHEET[1])

    def read(block: str, fallback: dict[str, str]) -> tuple[tuple[int, int, int], ...]:
        out = []
        for var in MARK_VARS:
            found = re.findall(rf"{re.escape(var)}\s*:\s*([^;]+);", block)
            out.append(_rgb(found[-1] if found else fallback[var]))
        return tuple(out)

    stock_values = {
        var: re.findall(rf"{re.escape(var)}\s*:\s*([^;]+);", stock_block)[-1]
        for var in MARK_VARS
    }
    palettes = {}
    for name, _label in config.APPEARANCE_CHOICES["ui_theme"]:
        if name == config.APPEARANCE_DEFAULTS["ui_theme"]:
            palettes[name] = read(stock_block, stock_values)
        else:
            palettes[name] = read(_block(themes_css, f"body.theme-{name}"), stock_values)
    return palettes


def themed_name(name: str, theme: str) -> str:
    """`favicon-32.png` + `paper` -> `favicon-32-paper.png`, and `favicon.svg`
    -> `favicon-paper.svg`. One rule for both shapes, because `main.favicon_url`
    has to reproduce it exactly from the other side and a second rule is a
    second thing to get wrong."""
    stem, _, ext = name.rpartition(".")
    return f"{stem}-{theme}.{ext}"


def mark_svg(palette: tuple[tuple[int, int, int], ...]) -> str:
    """The same two rings as `portal_mark`, as an SVG document.

    Kept in step with the raster mark by construction: identical geometry
    constants, same palette tuple. Safari ignores SVG favicons entirely, so
    this is never the only icon a browser is offered - but Chrome prefers it,
    and a Chrome tab showing the terminal mark on a paper page would be the
    exact thing this change is for.
    """
    tile, blue, orange = (f"rgb({r},{g},{b})" for r, g, b in palette)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!-- Generated by deploy/make_icons.py from this theme's palette block\n"
        "     in static/themes.css. Do not hand-edit: run the script. -->\n"
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" '
        'width="64" height="64">\n'
        f'  <rect width="64" height="64" rx="10" fill="{tile}"/>\n'
        f'  <ellipse cx="24" cy="32" rx="12" ry="19" fill="none" stroke="{blue}" '
        'stroke-width="6"/>\n'
        f'  <ellipse cx="42" cy="32" rx="12" ry="19" fill="none" stroke="{orange}" '
        'stroke-width="6"/>\n'
        "</svg>\n"
    )


def fill_mask_corners(img: Image.Image) -> Image.Image:
    """Put the tile's own gradient back into the masked-out corners.

    The fill for a row is taken from that row's *center* pixel rather than from
    its outermost opaque one. The outermost pixel of a row inside the corner
    band is the export's bright specular rim, so extending that sideways would
    paint a pale blob into each corner - the one artifact that survives iOS's
    mask and is visible on a dark home screen. The tile's gradient runs
    vertically, so the center of a row is the right color for the whole row.

    The center column has to be opaque for that to hold, which is true of any
    full-bleed square icon and is asserted rather than assumed.
    """
    img = img.convert("RGBA")
    w, h = img.size
    mid = w // 2
    if img.getchannel("A").crop((mid, 0, mid + 1, h)).getextrema()[0] != 255:
        raise ValueError(
            f"{w}x{h} source is transparent somewhere down its center column, "
            "so it is not a full-bleed square icon - check the export settings"
        )
    # One column stretched across the width: row y is filled with row y's own
    # color. NEAREST so no neighboring row bleeds into it.
    backdrop = img.crop((mid, 0, mid + 1, h)).resize((w, h), Image.NEAREST)
    return Image.alpha_composite(backdrop, img).convert("RGB")


def portal_mark(edge: int, palette: tuple[tuple[int, int, int], ...]) -> Image.Image:
    """The favicon.svg design, rasterized: two rings on a tile.

    `palette` is a theme's (tile, left ring, right ring) triple from
    `mark_palettes()`, and is required rather than defaulted: there is no
    longer any such thing as "the" mark, only a theme's, and a default here is
    how the old hard-coded pair drifted out of the stylesheet unnoticed.

    Drawn at 8x and downsampled, because PIL's ellipse has no antialiasing and
    a 16px favicon of jagged rings looks broken rather than retro.
    """
    tile, blue, orange = palette
    s = edge * 8
    img = Image.new("RGB", (s, s), tile)
    d = ImageDraw.Draw(img)
    # viewBox 0 0 64 64: ellipses at cx 24/42, cy 32, rx 12, ry 19, stroke 6.
    k = s / 64
    for cx, color in ((24, blue), (42, orange)):
        d.ellipse(
            [(cx - 12) * k, (32 - 19) * k, (cx + 12) * k, (32 + 19) * k],
            outline=color,
            width=max(1, round(6 * k)),
        )
    return img.resize((edge, edge), Image.LANCZOS)


def main() -> int:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE
    if not source.exists():
        print(f"no such source image: {source}", file=sys.stderr)
        return 1

    master = fill_mask_corners(Image.open(source))
    print(f"source {source.name}: {master.size[0]}x{master.size[1]}")

    for name, edge in PNG_SIZES.items():
        out = STATIC / name
        master.resize((edge, edge), Image.LANCZOS).save(out, "PNG", optimize=True)
        print(f"  wrote {name} ({edge}px, {out.stat().st_size // 1024} KB)")

    # The tab icons are drawn, not cut from the artwork - see the docstring.
    palettes = mark_palettes()
    stock = palettes[config.APPEARANCE_DEFAULTS["ui_theme"]]

    # The unsuffixed set is the default theme's mark, byte for byte. It is what
    # `main.favicon_url` falls back to for a theme with no set of its own, and
    # what a page cached from before this change keeps asking for - so it has
    # to BE one of the themed marks rather than a fourth thing drawn from its
    # own constants, which is precisely how the old pair drifted.
    for name, edge in MARK_SIZES.items():
        out = STATIC / name
        portal_mark(edge, stock).save(out, "PNG", optimize=True)
        print(f"  wrote {name} ({edge}px, {out.stat().st_size // 1024} KB)")
    (STATIC / "favicon.svg").write_text(mark_svg(stock))

    # One set per theme, drawn from that theme's own palette block.
    for theme, palette in palettes.items():
        for name, edge in MARK_SIZES.items():
            portal_mark(edge, palette).save(
                STATIC / themed_name(name, theme), "PNG", optimize=True
            )
        (STATIC / themed_name("favicon.svg", theme)).write_text(mark_svg(palette))
        tile, blue, orange = ("#%02x%02x%02x" % c for c in palette)
        print(f"  wrote {theme}: tile {tile}, rings {blue} / {orange}")

    # A real multi-size .ico, for the browsers that probe /favicon.ico at the
    # origin root regardless of what the <link> tags say. Deliberately NOT
    # themed: that probe carries no page and so no reader, and having the link
    # tag and the root probe answer with the same bytes is worth more than an
    # icon a browser only reaches when the two PNGs and the SVG have all failed.
    ico = STATIC / "favicon.ico"
    portal_mark(max(ICO_SIZES), stock).save(ico, "ICO", sizes=[(n, n) for n in ICO_SIZES])
    print(f"  wrote favicon.ico ({ICO_SIZES}, {ico.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
