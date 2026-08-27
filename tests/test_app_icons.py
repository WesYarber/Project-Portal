"""The home-screen icon, cut from an Apple Icon Composer export.

Wes, 2026-08-07: "If I want to update the app logo that is applied to iOS home
screen apps of this, can you use these app icons from the apple icon composer?"

He attached the six-file iOS export (Default, Dark, TintedLight, TintedDark,
ClearLight, ClearDark). Two facts shape everything here, and both are the kind
that look fine in a test suite and wrong on a phone:

- **An Icon Composer export already has iOS's rounded-corner mask cut out of
  it.** iOS masks an `apple-touch-icon` again and composites the rest onto
  white, so shipping the export untouched puts four white pips just outside the
  squircle. `make_icons.fill_mask_corners` fills them back in from the tile's
  own gradient, and the tests below pin that the shipped files came out of it.
- **iOS honors exactly one `apple-touch-icon` for a web app.** A `media`
  attribute on the link is ignored (checked 2026-08-07), so the Dark, Tinted
  and Clear variants have nowhere to go and a variant link would be a silent
  lie. Only Default ships.

The size / opacity / naming assertions live next door in `test_ui_polish.py`,
where the original app-icon note is answered; this file is about the source and
the corners.
"""
from __future__ import annotations

import importlib.util
import struct
from pathlib import Path

import pytest

from app import config

ROOT = Path(config.BASE_DIR)
STATIC = ROOT / "app" / "static"
TEMPLATES = ROOT / "app" / "templates"

# The three files cut from the artwork, and the edge each is drawn for.
ARTWORK_ICONS = {"apple-touch-icon.png": 180, "icon-192.png": 192, "icon-512.png": 512}


def _make_icons():
    """Import deploy/make_icons.py, which is not a package and not importable
    by name. Skips when Pillow is absent: it is a build-time dependency of that
    one script and deliberately not in requirements.txt."""
    pytest.importorskip("PIL")
    path = ROOT / "deploy" / "make_icons.py"
    spec = importlib.util.spec_from_file_location("make_icons", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_icon_source_artwork_is_in_the_repo():
    """It used to be read out of `data/projects/.../attachments/`, which is in
    .gitignore - so a fresh checkout (and the public repo) could not regenerate
    a single icon. The Icon Composer export lives in deploy/icon-source/."""
    mod = _make_icons()
    source = mod.DEFAULT_SOURCE
    assert source.exists(), source
    assert "data" not in source.relative_to(ROOT).parts
    w, h = struct.unpack(">II", source.read_bytes()[16:24])
    assert (w, h) == (1024, 1024)


def test_the_shipped_icons_have_no_masked_out_corners():
    """The one defect a size check cannot see: a transparent corner in the
    export becomes a white pip on the home screen. Every corner must be opaque
    AND continuous with the tile edge it sits on - a corner filled with some
    flat fallback color would pass 'is opaque' and still look like a patch."""
    Image = pytest.importorskip("PIL.Image", reason="Pillow is build-time only")
    for name in ARTWORK_ICONS:
        im = Image.open(STATIC / name)
        assert im.mode == "RGB", f"{name} carries an alpha channel"
        px = im.load()
        w, h = im.size
        mid = w // 2
        for (x, y), edge in (
            ((0, 0), (mid, 0)),
            ((w - 1, 0), (mid, 0)),
            ((0, h - 1), (mid, h - 1)),
            ((w - 1, h - 1), (mid, h - 1)),
        ):
            got, want = px[x, y], px[edge]
            drift = max(abs(a - b) for a, b in zip(got, want))
            assert drift <= 24, f"{name} corner {(x, y)} is {got}, edge is {want}"


def test_fill_mask_corners_rebuilds_the_row_it_punched_out():
    """A vertical gradient with the corners cut away comes back with each
    corner holding its own ROW's color - not the color of the nearest opaque
    pixel, which inside the corner band is the export's bright specular rim."""
    Image = pytest.importorskip("PIL.Image", reason="Pillow is build-time only")
    mod = _make_icons()

    n = 64
    src = Image.new("RGBA", (n, n))
    px = src.load()
    for y in range(n):
        for x in range(n):
            # A vertical gradient, with a hot rim one pixel in from each side.
            rim = x < 2 or x >= n - 2
            px[x, y] = (250, 250, 250, 255) if rim else (0, y * 4, 0, 255)
    for x, y in ((0, 0), (n - 1, 0), (0, n - 1), (n - 1, n - 1)):
        for dx in range(6):
            for dy in range(6):
                px[min(max(x + (dx if x == 0 else -dx), 0), n - 1),
                   min(max(y + (dy if y == 0 else -dy), 0), n - 1)] = (0, 0, 0, 0)

    out = mod.fill_mask_corners(src)
    assert out.mode == "RGB"
    got = out.load()
    for x, y in ((0, 0), (n - 1, 0), (0, n - 1), (n - 1, n - 1)):
        assert got[x, y] == (0, y * 4, 0), f"corner {(x, y)} is {got[x, y]}"


def test_fill_mask_corners_refuses_artwork_that_is_not_full_bleed():
    """Padded artwork - a tile floating in transparency - would have the pad
    smeared across every row instead of repaired corners, and would silently
    produce an icon with a border. It has to fail loudly instead."""
    Image = pytest.importorskip("PIL.Image", reason="Pillow is build-time only")
    mod = _make_icons()

    padded = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    padded.paste(Image.new("RGBA", (40, 40), (30, 30, 30, 255)), (12, 12))
    with pytest.raises(ValueError, match="full-bleed"):
        mod.fill_mask_corners(padded)


def test_no_dark_or_tinted_apple_touch_icon_variant_is_declared():
    """iOS reads one apple-touch-icon and ignores `media` on it, so a
    `media="(prefers-color-scheme: dark)"` variant link would ship a second
    icon that never appears anywhere - the quiet-failure shape. If a future run
    wants appearance variants, the export set is in this project's attachments
    and the only mechanism that has ever worked is rewriting the href in JS
    before the person taps Add to Home Screen."""
    html = (TEMPLATES / "base.html").read_text()
    for line in html.splitlines():
        if "apple-touch-icon" in line:
            assert "media=" not in line, line
