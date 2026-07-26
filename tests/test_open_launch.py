"""The "open it" button starts the thing when it is down.

Wes, 2026-07-23 03:54: "the button to open whatever has been built should also
run the thing if it isn't running. When I click it, it should surely open a
working version of whatever page it is."

Grouped by claim: the serve.json recipe, the probe, starting (systemd path,
fallback path, cooldown), the /open route's four outcomes, and the contract
text that teaches agents to leave the recipe.
"""
from __future__ import annotations

import http.server
import json
import subprocess
import threading

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config, db, launch


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app, follow_redirects=False)


@pytest.fixture(autouse=True)
def _fresh_cooldowns():
    launch.reset_cooldowns()
    yield
    launch.reset_cooldowns()


def _project(slug="tak", **fields):
    row = db.create_project("Tak", description="a game server", slug=slug)
    (config.PROJECTS_DIR / slug).mkdir(parents=True, exist_ok=True)
    if fields:
        db.update_project(row["id"], **fields)
        row = db.get_project(row["id"])
    return row


def _recipe(slug, data):
    portal_dir = config.PROJECTS_DIR / slug / ".portal"
    portal_dir.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data)
    (portal_dir / "serve.json").write_text(text)


# --------------------------------------------------------------------------
# serve.json: what counts as a recipe
# --------------------------------------------------------------------------

def test_valid_recipe_parses(temp_data_dir):
    _project()
    _recipe("tak", {"cmd": "bun server.js"})
    assert launch.serve_config("tak") == {"cmd": "bun server.js", "cwd": "."}


def test_missing_file_is_none(temp_data_dir):
    _project()
    assert launch.serve_config("tak") is None


@pytest.mark.parametrize(
    "data",
    [
        "not json {",
        json.dumps(["cmd"]),
        json.dumps({}),
        json.dumps({"cmd": ""}),
        json.dumps({"cmd": 42}),
        json.dumps({"cmd": "x" * 1001}),
        json.dumps({"cmd": "bun x", "cwd": "../other"}),
        json.dumps({"cmd": "bun x", "cwd": 3}),
    ],
)
def test_bad_recipes_are_none_not_errors(temp_data_dir, data):
    _project()
    _recipe("tak", data)
    assert launch.serve_config("tak") is None


def test_cwd_subdirectory_is_kept(temp_data_dir):
    _project()
    _recipe("tak", {"cmd": "bun server.js", "cwd": "server"})
    assert launch.serve_config("tak")["cwd"] == "server"


# --------------------------------------------------------------------------
# The probe: up means "something answered HTTP", not "answered 200"
# --------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):  # noqa: N802
        self.send_response(self.status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


def test_probe_up(local_server):
    assert launch.probe(local_server) is True


def test_probe_error_status_still_counts_as_up(local_server):
    _Handler.status = 500
    try:
        assert launch.probe(local_server) is True
    finally:
        _Handler.status = 200


def test_probe_down():
    # Port 1 is never listening.
    assert launch.probe("http://127.0.0.1:1/", timeout=0.5) is False


def test_probe_refuses_non_http():
    assert launch.probe("ftp://example.com/") is False


# --------------------------------------------------------------------------
# Starting: systemd path, fallback path, cooldown
# --------------------------------------------------------------------------

def test_systemd_start_records_unit_and_command(temp_data_dir, monkeypatch):
    _project()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(launch.subprocess, "run", fake_run)
    started, detail = launch.start("tak", {"cmd": "bun server.js", "cwd": "."})
    assert started is True
    assert "portal-app-tak" in detail
    run_call = calls[-1]
    assert run_call[0] == "systemd-run"
    assert "--unit=portal-app-tak" in run_call
    assert run_call[-1] == "bun server.js"
    # A dead unit with the same name is stopped first, or systemd-run refuses
    # the duplicate and the button dead-ends.
    assert any(c[:3] == ["systemctl", "--user", "stop"] for c in calls)


def test_fallback_spawns_detached_and_logs(temp_data_dir, monkeypatch):
    _project()

    def no_systemd(argv, **kwargs):
        raise FileNotFoundError("systemd-run")

    monkeypatch.setattr(launch.subprocess, "run", no_systemd)
    started, detail = launch.start(
        "tak", {"cmd": "echo started-marker; sleep 0.05", "cwd": "."}
    )
    assert started is True
    portal_dir = config.PROJECTS_DIR / "tak" / ".portal"
    assert (portal_dir / "serve.pid").exists()
    # The child logs to serve.log; give it a beat to run.
    import time

    for _ in range(50):
        log_path = portal_dir / "serve.log"
        if log_path.exists() and "started-marker" in log_path.read_text():
            break
        time.sleep(0.05)
    assert "started-marker" in (portal_dir / "serve.log").read_text()


def test_missing_workdir_refuses(temp_data_dir):
    _project()
    started, detail = launch.start("tak", {"cmd": "true", "cwd": "gone"})
    assert started is False
    assert "gone" in detail


def test_cooldown_blocks_a_second_start(temp_data_dir, monkeypatch):
    _project()
    monkeypatch.setattr(
        launch.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="", stderr=""),
    )
    assert launch.start("tak", {"cmd": "bun x", "cwd": "."})[0] is True
    started, detail = launch.start("tak", {"cmd": "bun x", "cwd": "."})
    assert started is False
    assert "recently started" in detail


# --------------------------------------------------------------------------
# The /open route
# --------------------------------------------------------------------------

def test_open_unknown_project_404s(client):
    assert client.get("/open/nope").status_code == 404


def test_open_nothing_to_open_404s(client):
    _project()
    assert client.get("/open/tak").status_code == 404


def test_open_static_page_redirects_to_preview_server(client):
    _project()
    (config.PROJECTS_DIR / "tak" / "index.html").write_text("<!doctype html>hi")
    resp = client.get("/open/tak")
    assert resp.status_code == 307
    assert f":{config.PREVIEW_PORT}/tak/" in resp.headers["location"]


def test_open_running_server_redirects_straight_there(client, monkeypatch):
    _project(preview_url="http://testhost:8790/")
    monkeypatch.setattr(launch, "probe", lambda url, timeout=2.5: True)
    resp = client.get("/open/tak")
    assert resp.status_code == 307
    assert resp.headers["location"] == "http://testhost:8790/"


def test_open_down_without_recipe_explains(client, monkeypatch):
    _project(preview_url="http://testhost:8790/")
    monkeypatch.setattr(launch, "probe", lambda url, timeout=2.5: False)
    resp = client.get("/open/tak")
    assert resp.status_code == 200
    assert "serve.json" in resp.text
    assert "not answering" in resp.text


def test_open_down_with_recipe_starts_and_holds(client, monkeypatch):
    _project(preview_url="http://testhost:8790/")
    _recipe("tak", {"cmd": "bun server.js"})
    monkeypatch.setattr(launch, "probe", lambda url, timeout=2.5: False)
    starts = []

    def fake_start(slug, cfg):
        starts.append((slug, cfg["cmd"]))
        return True, "started as user unit portal-app-tak"

    monkeypatch.setattr(launch, "start", fake_start)
    resp = client.get("/open/tak")
    assert resp.status_code == 200
    assert starts == [("tak", "bun server.js")]
    assert "being started" in resp.text
    # The holding page polls the status endpoint.
    assert "/open/tak/status" in resp.text


def test_open_status_reports_probe(client, monkeypatch):
    _project(preview_url="http://testhost:8790/")
    monkeypatch.setattr(launch, "probe", lambda url, timeout=2.5: True)
    data = client.get("/open/tak/status").json()
    assert data == {"up": True, "url": "http://testhost:8790/"}


def test_button_points_at_open_route(client):
    _project()
    (config.PROJECTS_DIR / "tak" / "index.html").write_text("<!doctype html>hi")
    page = client.get("/project/tak").text
    assert 'href="/open/tak"' in page


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------

def test_contract_teaches_serve_json():
    assert "serve.json" in agent_runner.AGENT_CONTRACT
    assert '"cmd"' in agent_runner.AGENT_CONTRACT
