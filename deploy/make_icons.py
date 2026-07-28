#!/usr/bin/env python3
"""Cut the app icon and favicons from the artwork Wes attached.

He uploaded one 882x882 PNG (`0001-project_portal-app-icon-clean.png`, an
attachment filename nobody wants in a <link> tag) and asked for it to be the
app icon and the favicon under a tidier name. This writes the fixed set of
sizes into app/static/ once; the outputs are committed, so Pillow is a
build-time dependency of this script and NOT a runtime dependency of the
portal.

The artwork is RGBA with a soft glow fading to transparent at the edges. iOS
composites an apple-touch-icon onto white, which would put a white halo around
a deliberately dark icon, so everything is flattened onto the terminal
background color first.

The artwork is NOT used for the 16-32px tab favicon. Downscaled that far the
two words collapse into an unreadable smudge - it is a home-screen icon, drawn
to be looked at at 180px. The tab keeps the drawn two-portal mark (the blue O
and the orange O from the wordmark), which is the same design language and
still reads at 16px; favicon.ico is rendered from those same shapes so the
browsers that probe the origin root get the legible one too.

Usage:  venv/bin/python deploy/make_icons.py [source.png]
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
DEFAULT_SOURCE = (
    ROOT / "data" / "projects" / "project-portal" / "attachments"
    / "0001-project_portal-app-icon-clean.png"
)
BG = (13, 16, 22)  # #0d1016, the terminal background

# name -> square edge in px. All cut from Wes's artwork.
PNG_SIZES = {
    "icon-512.png": 512,   # manifest, and the master anything else is cut from
    "icon-192.png": 192,   # manifest / Android home screen
    "apple-touch-icon.png": 180,  # iOS home screen
}
ICO_SIZES = [16, 32, 48]
BLUE = (63, 167, 255)
ORANGE = (255, 138, 61)


def flatten(img: Image.Image) -> Image.Image:
    """RGBA -> RGB on the terminal background."""
    img = img.convert("RGBA")
    base = Image.new("RGBA", img.size, BG + (255,))
    return Image.alpha_composite(base, img).convert("RGB")


def portal_mark(edge: int) -> Image.Image:
    """The favicon.svg design, rasterised: two rings on a dark tile.

    Drawn at 8x and downsampled, because PIL's ellipse has no antialiasing and
    a 16px favicon of jagged rings looks broken rather than retro.
    """
    s = edge * 8
    img = Image.new("RGB", (s, s), BG)
    d = ImageDraw.Draw(img)
    # viewBox 0 0 64 64: ellipses at cx 24/42, cy 32, rx 12, ry 19, stroke 6.
    k = s / 64
    for cx, color in ((24, BLUE), (42, ORANGE)):
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

    master = flatten(Image.open(source))
    print(f"source {source.name}: {master.size[0]}x{master.size[1]}")

    for name, edge in PNG_SIZES.items():
        out = STATIC / name
        master.resize((edge, edge), Image.LANCZOS).save(out, "PNG", optimize=True)
        print(f"  wrote {name} ({edge}px, {out.stat().st_size // 1024} KB)")

    # A real multi-size .ico, for the browsers that probe /favicon.ico at the
    # origin root regardless of what the <link> tags say. Drawn, not cut from
    # the artwork - see the module docstring.
    ico = STATIC / "favicon.ico"
    portal_mark(max(ICO_SIZES)).save(ico, "ICO", sizes=[(n, n) for n in ICO_SIZES])
    print(f"  wrote favicon.ico ({ICO_SIZES}, {ico.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
