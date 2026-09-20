"""`deploy/interact.py` - the CDP driver every agent uses to *see* a page.

This file exists because of a false negative that cost another project an
afternoon on 2026-09-20. Headless chromium's window never holds OS focus, so
the page it renders reports `document.hasFocus()` false and fires **no** focus,
blur, focusin or focusout events whatsoever - `element.focus()` moves
`document.activeElement` and tells nobody. A feature hung off losing focus (an
on-blur save, a validate-on-leave, a close-on-focus-loss menu, a focus trap)
therefore reads back as "the handler never ran" from a driver that is itself
working perfectly, and the failure points at the app.

`Emulation.setFocusEmulationEnabled` makes the page agree with what a focused
window reports about itself. The two things worth asserting about it are that
it is sent at all and that it is sent **before** the navigation - a page that
arrives before the emulation is on is exactly the page that was wrong.

Everything here drives the real `shoot()` with a fake CDP socket, so the
assertions are about the actual protocol conversation: what is sent, in what
order, and what comes back out. The portal's own UI has three blur/focusout
handlers (`app/static/app.js` - the inline title editor, the workspace file
rename, the deferred reload), none of which could be verified before this.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deploy import interact  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n-not-really-a-png"


class FakeSocket:
    """Stands in for the CDP websocket; answers every command it is sent.

    `results` overrides the result body per method, `errors` makes a method
    come back as a protocol error, and `noise` is a list of unsolicited CDP
    events delivered before the next reply (chromium really does interleave
    these, which is why `send()` matches on the id rather than taking the next
    frame off the wire).
    """

    def __init__(self, results=None, errors=None, noise=None):
        self.sent: list[dict] = []
        self.results = dict(results or {})
        self.errors = dict(errors or {})
        self.noise = list(noise or [])
        self._queue: list[str] = []
        self.results.setdefault(
            "Page.captureScreenshot",
            {"data": base64.b64encode(PNG).decode()},
        )

    @property
    def methods(self) -> list[str]:
        return [m["method"] for m in self.sent]

    def params(self, method: str) -> dict:
        for msg in self.sent:
            if msg["method"] == method:
                return msg.get("params", {})
        raise AssertionError(f"{method} was never sent; sent {self.methods}")

    async def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        # Before every reply, not just the first: chromium keeps talking while
        # it answers, and a driver that only survives one stray frame is a
        # driver that happens to be lucky.
        self._queue.extend(json.dumps(ev) for ev in self.noise)
        reply: dict = {"id": msg["id"]}
        if msg["method"] in self.errors:
            reply["error"] = self.errors[msg["method"]]
        else:
            reply["result"] = self.results.get(msg["method"], {})
        self._queue.append(json.dumps(reply))

    async def recv(self) -> str:
        if not self._queue:
            raise AssertionError("the driver read a frame nobody sent")
        return self._queue.pop(0)


class FakeConnect:
    """`websockets.connect(...)` as an async context manager over one socket."""

    def __init__(self, socket: FakeSocket):
        self.socket = socket
        self.url: str | None = None
        self.kwargs: dict = {}

    def __call__(self, url, **kwargs):
        self.url = url
        self.kwargs = kwargs
        return self

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *exc):
        return False


class FakeChrome:
    """The browser on the render machine, with the ssh and the tunnel removed."""

    last: "FakeChrome | None" = None

    def __init__(self, width: int, height: int):
        self.width, self.height = width, height
        self.entered = False
        self.exited = False
        FakeChrome.last = self

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.exited = True
        return False

    def page_ws(self) -> str:
        return "ws://127.0.0.1:9222/devtools/page/fake"


def drive(monkeypatch, tmp_path, *, js=(), socket=None, viewport_only=False,
          width=1280, height=1600, url="http://portal.example.invalid:8500/"):
    """Run the real `shoot()` against a fake browser. Returns (socket, out)."""
    import asyncio

    sock = socket if socket is not None else FakeSocket()
    connect = FakeConnect(sock)
    monkeypatch.setattr(interact, "websockets",
                        type("ws", (), {"connect": connect}))
    monkeypatch.setattr(interact, "Chrome", FakeChrome)
    out = tmp_path / "shot.png"
    asyncio.run(interact.shoot(url, str(out), list(js), 0.0,
                               width, height, viewport_only))
    return sock, out, connect


# --- The fix: a headless page that reports its own focus honestly ------------


def test_focus_emulation_is_enabled(monkeypatch, tmp_path):
    sock, _, _ = drive(monkeypatch, tmp_path)
    assert "Emulation.setFocusEmulationEnabled" in sock.methods


def test_focus_emulation_is_enabled_not_merely_mentioned(monkeypatch, tmp_path):
    sock, _, _ = drive(monkeypatch, tmp_path)
    assert sock.params("Emulation.setFocusEmulationEnabled") == {"enabled": True}


def test_focus_emulation_is_sent_as_a_real_bool(monkeypatch, tmp_path):
    # CDP types its params; the string "true" is not a boolean and chromium
    # answers with an error rather than quietly coercing it.
    sock, _, _ = drive(monkeypatch, tmp_path)
    enabled = sock.params("Emulation.setFocusEmulationEnabled")["enabled"]
    assert enabled is True


def test_focus_emulation_comes_before_the_navigation(monkeypatch, tmp_path):
    # The load-bearing assertion. Enabling focus emulation *after* Page.navigate
    # leaves the page under test to load, run its scripts and bind its handlers
    # in the unfocused world - the one state this whole change exists to leave.
    sock, _, _ = drive(monkeypatch, tmp_path)
    order = sock.methods
    assert order.index("Emulation.setFocusEmulationEnabled") < \
        order.index("Page.navigate")


def test_the_opening_sequence_is_enable_focus_navigate(monkeypatch, tmp_path):
    sock, _, _ = drive(monkeypatch, tmp_path)
    assert sock.methods[:3] == [
        "Page.enable",
        "Emulation.setFocusEmulationEnabled",
        "Page.navigate",
    ]


def test_focus_emulation_is_sent_once_per_shot(monkeypatch, tmp_path):
    sock, _, _ = drive(monkeypatch, tmp_path, js=["1", "2"])
    assert sock.methods.count("Emulation.setFocusEmulationEnabled") == 1


def test_focus_emulation_is_enabled_even_with_no_js(monkeypatch, tmp_path):
    # A plain screenshot benefits too: a page whose layout depends on a focus
    # ring or a focused-panel style renders differently unfocused.
    sock, _, _ = drive(monkeypatch, tmp_path, js=())
    assert "Emulation.setFocusEmulationEnabled" in sock.methods


def test_a_refusing_chromium_is_reported_not_swallowed(monkeypatch, tmp_path):
    # If a future chromium drops the command, the driver must say so rather
    # than carry on silently back in the state this fixed.
    sock = FakeSocket(errors={"Emulation.setFocusEmulationEnabled":
                              {"code": -32601, "message": "not found"}})
    with pytest.raises(SystemExit) as exc:
        drive(monkeypatch, tmp_path, socket=sock)
    assert "Emulation.setFocusEmulationEnabled" in str(exc.value)


# --- The rest of the conversation, which had no test at all ------------------


def test_the_url_is_navigated_to(monkeypatch, tmp_path):
    sock, _, _ = drive(monkeypatch, tmp_path, url="http://portal.example.invalid:8500/x")
    assert sock.params("Page.navigate") == {"url": "http://portal.example.invalid:8500/x"}


def test_each_js_expression_runs_in_order_after_the_navigation(monkeypatch, tmp_path):
    sock, _, _ = drive(monkeypatch, tmp_path, js=["first()", "second()"])
    evaluated = [m["params"]["expression"] for m in sock.sent
                 if m["method"] == "Runtime.evaluate"]
    assert evaluated == ["first()", "second()"]
    assert sock.methods.index("Page.navigate") < \
        sock.methods.index("Runtime.evaluate")


def test_a_js_promise_is_awaited_and_its_value_returned(monkeypatch, tmp_path):
    sock, _, _ = drive(monkeypatch, tmp_path, js=["go()"])
    params = sock.params("Runtime.evaluate")
    assert params["awaitPromise"] is True
    assert params["returnByValue"] is True


def test_a_js_answer_is_printed(monkeypatch, tmp_path, capsys):
    sock = FakeSocket(results={"Runtime.evaluate":
                               {"result": {"value": "doc:focus,a:focusin"}}})
    drive(monkeypatch, tmp_path, js=["events()"], socket=sock)
    assert "js> doc:focus,a:focusin" in capsys.readouterr().out


def test_a_js_expression_with_no_answer_prints_nothing(monkeypatch, tmp_path, capsys):
    drive(monkeypatch, tmp_path, js=["sideEffect()"])
    assert "js>" not in capsys.readouterr().out


def test_a_throwing_js_expression_stops_the_run(monkeypatch, tmp_path):
    sock = FakeSocket(results={"Runtime.evaluate":
                               {"exceptionDetails": {"text": "boom"}}})
    with pytest.raises(SystemExit) as exc:
        drive(monkeypatch, tmp_path, js=["nope()"], socket=sock)
    assert "nope()" in str(exc.value)


def test_a_throwing_js_expression_leaves_no_half_written_png(monkeypatch, tmp_path):
    sock = FakeSocket(results={"Runtime.evaluate":
                               {"exceptionDetails": {"text": "boom"}}})
    out = tmp_path / "shot.png"
    with pytest.raises(SystemExit):
        drive(monkeypatch, tmp_path, js=["nope()"], socket=sock)
    assert not out.exists()


def test_the_browser_is_shut_down_when_the_page_js_fails(monkeypatch, tmp_path):
    # Chromium is killed by Chrome.__exit__; leaking one wedges the next run,
    # which then reads as "the render machine is broken".
    sock = FakeSocket(results={"Runtime.evaluate":
                               {"exceptionDetails": {"text": "boom"}}})
    with pytest.raises(SystemExit):
        drive(monkeypatch, tmp_path, js=["nope()"], socket=sock)
    assert FakeChrome.last.exited


def test_the_screenshot_bytes_are_what_lands_on_disk(monkeypatch, tmp_path):
    _, out, _ = drive(monkeypatch, tmp_path)
    assert out.read_bytes() == PNG


def test_the_whole_document_is_captured_by_default(monkeypatch, tmp_path):
    sock, _, _ = drive(monkeypatch, tmp_path)
    assert sock.params("Page.captureScreenshot")["captureBeyondViewport"] is True


def test_viewport_only_keeps_fixed_elements_where_they_are(monkeypatch, tmp_path):
    sock, _, _ = drive(monkeypatch, tmp_path, viewport_only=True)
    assert sock.params("Page.captureScreenshot")["captureBeyondViewport"] is False


def test_the_window_size_reaches_the_browser(monkeypatch, tmp_path):
    drive(monkeypatch, tmp_path, width=1135, height=900)
    assert (FakeChrome.last.width, FakeChrome.last.height) == (1135, 900)


def test_the_socket_takes_a_frame_big_enough_for_a_screenshot(monkeypatch, tmp_path):
    # A full-page PNG at 1280px is megabytes; the library's default 1 MiB frame
    # limit drops the reply and the run dies reading a closed socket.
    _, _, connect = drive(monkeypatch, tmp_path)
    assert connect.kwargs["max_size"] >= 16 * 1024 * 1024


def test_the_driver_attaches_to_the_page_target(monkeypatch, tmp_path):
    _, _, connect = drive(monkeypatch, tmp_path)
    assert connect.url == "ws://127.0.0.1:9222/devtools/page/fake"


def test_an_unrelated_cdp_event_does_not_answer_a_command(monkeypatch, tmp_path):
    # Chromium interleaves events with replies. Taking the next frame as the
    # answer would make every subsequent reply belong to the wrong command.
    sock = FakeSocket(noise=[{"method": "Page.frameNavigated", "params": {}}])
    _, out, _ = drive(monkeypatch, tmp_path, js=["ask()"], socket=sock)
    assert sock.methods[:3] == [
        "Page.enable",
        "Emulation.setFocusEmulationEnabled",
        "Page.navigate",
    ]
    # Every command got its own answer, so the screenshot is the screenshot and
    # not whatever reply had been left in the queue by the one before it.
    assert out.read_bytes() == PNG


def test_a_protocol_error_names_the_command_that_failed(monkeypatch, tmp_path):
    sock = FakeSocket(errors={"Page.navigate":
                              {"code": -32000, "message": "Cannot navigate"}})
    with pytest.raises(SystemExit) as exc:
        drive(monkeypatch, tmp_path, socket=sock)
    assert "Page.navigate" in str(exc.value)
    assert "Cannot navigate" in str(exc.value)


# --- Picking the tab to drive -----------------------------------------------


class _FakeTargets:
    """A stand-in for urlopen against chromium's /json/list."""

    def __init__(self, payload):
        self.payload = payload

    def __call__(self, url, timeout=None):
        self.url = url
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_the_about_blank_tab_is_the_one_driven(monkeypatch):
    # Not /json/new: recent chromium answers that with 405 unless it is a PUT.
    monkeypatch.setattr(interact, "urlopen", _FakeTargets([
        {"type": "background_page", "webSocketDebuggerUrl": "ws://bg"},
        {"type": "page", "webSocketDebuggerUrl": "ws://the-tab"},
    ]))
    assert interact.Chrome(800, 600).page_ws() == "ws://the-tab"


def test_a_browser_with_no_tab_is_an_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(interact, "urlopen", _FakeTargets([
        {"type": "background_page", "webSocketDebuggerUrl": "ws://bg"},
    ]))
    with pytest.raises(SystemExit) as exc:
        interact.Chrome(800, 600).page_ws()
    assert "no page target" in str(exc.value)
