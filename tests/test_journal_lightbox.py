"""The in-page image viewer (lightbox).

Wes, 2026-07-23 18:45: "When opening an image or something in the journal to
view, make a popup show up on the page to view it and interact with the file.
Should be able to zoom and whatnot. Should not take you to a different page."

A journal image is wrapped by mediamd in a portal self-link
(`<a class="journal-media-link" data-lightbox href="<raw>">`). app.js
intercepts a plain left-click on that link and opens a zoomable, pannable
overlay instead of navigating to the raw file. These tests pin the marker the
JS keys off, the JS behaviour that matters (intercept, but never on a modified
click), and the CSS that makes the overlay a fixed in-page popup.
"""
from __future__ import annotations

from pathlib import Path

from app import config, mediamd

STATIC = Path(config.APP_ROOT) / "app" / "static"


# --------------------------------------------------------------------------
# The marker mediamd emits
# --------------------------------------------------------------------------

def test_self_link_carries_the_lightbox_marker():
    html = '<p><img alt="the layout" src="shots/dash.png" /></p>'
    out = mediamd.resolve_media(html, "/raw/manabase")
    assert 'class="journal-media-link"' in out
    assert "data-lightbox" in out
    # The href/target stay so a modified click still reaches the raw file.
    assert 'href="/raw/manabase/shots/dash.png"' in out
    assert 'target="_blank"' in out


def test_agents_own_link_is_not_marked_for_the_lightbox():
    # [![alt](img)](target): the agent chose the link target, so it must
    # navigate there, not open the viewer.
    html = '<p><a href="https://example.com"><img alt="x" src="a.png" /></a></p>'
    out = mediamd.resolve_media(html, "/raw/manabase")
    assert "data-lightbox" not in out
    assert out.count("<a ") == 1


def test_video_is_a_player_not_a_lightbox_link():
    html = '<p><img alt="demo" src="demo.mp4" /></p>'
    out = mediamd.resolve_media(html, "/raw/manabase")
    assert "<video" in out
    assert "data-lightbox" not in out


# --------------------------------------------------------------------------
# The JS behaviour
# --------------------------------------------------------------------------

def _js():
    return (STATIC / "app.js").read_text()


def test_js_intercepts_the_marked_link():
    js = _js()
    assert "a.journal-media-link[data-lightbox]" in js
    assert "lbOpen(" in js


def test_js_leaves_modified_clicks_to_the_browser():
    # A cmd/ctrl/shift/alt or non-left click must fall through so "open in new
    # tab" still works - the raw file is the graceful fallback.
    js = _js()
    assert "ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey" in js
    assert "ev.button !== 0" in js


def test_js_supports_zoom_and_pan():
    js = _js()
    for fn in ("lbZoomAt", "lbZoomBy", "lbFit", "lbClamp"):
        assert fn in js, fn
    # wheel zoom and pinch (two-pointer) support.
    assert 'addEventListener("wheel"' in js
    assert "lbPointerSpread" in js


def test_wheel_zoom_is_proportional_to_delta_not_a_fixed_step():
    # Wes, 2026-07-24: trackpad zoom was "way too sensitive". A trackpad fires
    # a stream of tiny-delta wheel events; the old handler multiplied scale by
    # a fixed 1.15 on EVERY event, so those tiny nudges compounded into a
    # runaway zoom. The fix scales the zoom by the actual wheel delta.
    js = _js()
    # exp() of the (clamped) delta drives the zoom now...
    assert "Math.exp(-dy * 0.0025)" in js
    # ...and the old fixed-step expression is gone.
    assert "ev.deltaY < 0 ? 1.15 : 1 / 1.15" not in js
    # deltaMode is normalised to pixels and the per-event delta is clamped so a
    # single chunky mouse notch can't leap.
    assert "ev.deltaMode === 1" in js
    assert "Math.max(-50, Math.min(50, dy))" in js


def test_a_click_beside_the_image_closes_the_viewer():
    # Wes, 2026-07-28: "In the image, pop-up view, clicking off the side of the
    # image should close it."
    #
    # This is the third instruction about this one click, so the history is
    # worth stating rather than re-litigating:
    #
    #   07-24: backdrop click closes, guarded against a pan release.
    #   07-25: "doesn't close with a click ... the user has to hit escape or
    #          click the close button" - read as "take the backdrop close
    #          away", and it was taken away outright.
    #   07-28: put it back, for clicks off the SIDE of the image.
    #
    # "Off the side" is the load-bearing phrase and it is what makes this
    # different from the version that was removed: the close fires only when
    # the click landed on the stage itself, i.e. in the letterbox margin
    # beside the image, never on the image. A misjudged pan lands on the
    # image (that is the thing being dragged) and a zoomed image fills the
    # stage entirely, so there is no backdrop left to hit by accident.
    js = _js()
    assert "ev.target === lb.stage || ev.target === lb.root" in js
    # ...and a pan that starts and ends on the backdrop still fires a click,
    # so the distance guard has to survive with it.
    assert "lb.moved < 5" in js
    # Accumulated distance, not straight-line: a pan that wanders and returns
    # to where it started is still a pan.
    assert "lb.moved += Math.abs(" in js
    # lbClose() is called from exactly three places: the ✕ button's action,
    # the Escape handler, and this. Anything else is a fourth way to lose the
    # image you were reading.
    calls = [ln.strip() for ln in js.splitlines()
             if "lbClose()" in ln and not ln.startswith("function lbClose")]
    assert len(calls) == 3, calls
    assert any('act === "close"' in c for c in calls)
    assert any("ev.stopPropagation()" in c for c in calls)


def test_a_click_on_the_image_itself_never_closes_the_viewer():
    # The 2026-07-25 complaint, kept as a live guarantee: whatever the
    # backdrop does, a click that lands on the image must not throw it away.
    # The handler's only close path is gated on the target being the stage or
    # the root, and the image is neither.
    js = _js()
    handler = js.split('root.addEventListener("click"')[1].split("var act =")[0]
    assert "lbClose()" in handler
    assert "lb.img" not in handler


def test_escape_and_the_close_button_are_the_two_ways_out():
    js = _js()
    # The bar carries a ✕ that says so, including the keyboard alternative.
    assert 'data-lb="close"' in js
    assert 'title="Close (Esc)"' in js
    # Escape closes, in the capture phase so it beats the page's other Escape
    # handlers (the context menu, the draft-note restore).
    esc = js.split("// Escape closes the viewer")[1][:400]
    assert "lbClose()" in esc
    assert "true" in esc  # capture-phase listener


def test_js_closes_on_escape_and_the_overlay_survives_a_morph():
    js = _js()
    assert "lbClose" in js
    # Escape handler is capture-phase so it beats the page's other Escape
    # handlers.
    assert '#img-lightbox' in js  # in MORPH_KEEP
    assert "MORPH_KEEP" in js and "#img-lightbox" in js


# --------------------------------------------------------------------------
# The CSS that makes it a popup, not a page
# --------------------------------------------------------------------------

def test_css_defines_a_fixed_overlay():
    css = (STATIC / "style.css").read_text()
    assert "#img-lightbox" in css
    assert "position: fixed" in css
    # The stage clips a zoomed/panned image and drives its own gestures.
    assert ".lb-stage" in css
    assert "touch-action: none" in css
