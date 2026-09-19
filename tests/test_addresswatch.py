"""Watching the addresses the shared skills name on the local network.

The defect this exists for, measured on 2026-09-19: a project moved its
container onto a shared Docker network and published no port, which closed the
address a shared skill had been telling about fifteen other projects to fetch
three paths from. `data/skills/` has no git history, so the scanner in another
project that eventually found it could not read the copy every run is actually
handed - only the portal can.

These tests pin what counts as an address (and, harder, what does not), the
per-file opt-out, the TCP probe against a real socket, the once-only todo, and
the fail-open posture in every layer.
"""
from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import addresswatch, config, db, settings_form, worker
from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def skills(tmp_path, monkeypatch):
    """A skills root of our own, with the built-in root pointed at nothing.

    `config.SKILLS_DIR` is NOT repointed by the shared fixture - it is under the
    repo, not under DATA_DIR - so a test that forgot this would scan the real
    `app/skills/` and probe whatever it names.
    """
    root = tmp_path / "shared-skills"
    root.mkdir()
    monkeypatch.setattr(config, "SKILLS_DIR", tmp_path / "no-built-ins")
    return root


def _skill(root: Path, name: str, body: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(body, encoding="utf-8")
    return path


def _meta_project():
    return db.create_project(
        title="Project Portal", description="", kind="software", slug=config.META_PROJECT_SLUG
    )


# --------------------------------------------------------------------------
# What is an address
# --------------------------------------------------------------------------

def test_a_url_on_the_lan_is_an_address():
    assert addresswatch.addresses_in("see http://192.168.1.10:3002 for Kuma") == [
        ("192.168.1.10", 3002)
    ]


def test_a_bare_address_in_a_code_span_is_an_address():
    assert addresswatch.addresses_in("write `homeserver:8500` in a summary") == [
        ("homeserver", 8500)
    ]


def test_the_same_address_written_twice_is_found_once():
    text = "`homeserver:8500` and http://homeserver:8500/api/ping"
    assert addresswatch.addresses_in(text) == [("homeserver", 8500)]


def test_a_bare_address_in_prose_is_not_an_address():
    # "Note:8 items" reads as host `note`, port 8 to any regex. Prose is not
    # where an address is written in these skills; a code span is.
    assert addresswatch.addresses_in("Note:8080 items were left over") == []


def test_a_public_hostname_is_left_alone():
    # example.com is an SPA behind `try_files $uri /index.html` fronted by
    # Cloudflare: a TCP connect to it proves nothing about the path in question.
    assert addresswatch.addresses_in("GET https://shop.example.com:443/api") == []


def test_loopback_is_left_alone():
    # `127.0.0.1:9222` in a skill means "on whatever machine you are on" -
    # usually desktop-box at the far end of an ssh tunnel.
    assert addresswatch.addresses_in("curl http://127.0.0.1:9222/json/version") == []
    assert addresswatch.addresses_in("open `localhost:8531` in the browser") == []


def test_a_tailnet_address_is_watched():
    # 100.64/10 is carrier-grade NAT, which `ipaddress.is_private` calls public.
    # On this estate it is the LAN.
    assert addresswatch.addresses_in("`100.100.1.2:9222`") == [("100.100.1.2", 9222)]


def test_the_middle_of_an_ssh_port_forward_is_not_a_host():
    # `-L 9334:127.0.0.1:9222` would otherwise read as host `9334`, port 127.
    assert addresswatch.addresses_in("run `ssh -f -N -L 9334:127.0.0.1:9222 user@box`") == []


def test_a_bare_pair_below_1024_is_not_an_address():
    # Measured against the real skills: `fake-camera-in-headless-chromium`
    # carries a YUV4MPEG header with a literal `A1:1` aspect ratio, which is a
    # dotless host and a valid port by every other rule here.
    assert addresswatch.addresses_in("`YUV4MPEG2 W640 H480 F30:1 Ip A1:1 C420`") == []


def test_a_url_below_1024_is_still_an_address():
    # The floor is about the bare shape only: a URL says "host" unambiguously.
    assert addresswatch.addresses_in("http://homeserver:80/") == [("homeserver", 80)]


def test_a_port_out_of_range_is_not_an_address():
    assert addresswatch.addresses_in("http://homeserver:99999/") == []


@pytest.mark.parametrize(
    "host, watchable",
    [
        ("192.168.1.10", True),
        ("10.0.0.4", True),
        ("172.16.5.4", True),
        ("169.254.1.1", True),
        ("100.100.1.2", True),
        ("8.8.8.8", False),
        ("127.0.0.1", False),
        ("localhost", False),
        ("homeserver", True),
        ("desktop-box", True),
        ("shop.example.com", False),
        # The port half of an `ssh -L 9334:127.0.0.1:9222` forward. Dotless,
        # not loopback, and not an IP address - only "starts with a letter"
        # keeps it out.
        ("9334", False),
        ("8500", False),
        ("", False),
    ],
)
def test_which_hosts_the_portal_can_speak_for(host, watchable):
    assert addresswatch.is_watchable_host(host) is watchable


# --------------------------------------------------------------------------
# Scanning the skills
# --------------------------------------------------------------------------

def test_the_scan_reads_the_shared_skills(skills):
    _skill(skills, "kuma", "Kuma is at http://192.168.1.10:3002 on the box.")
    watched, ignored = addresswatch.scan([skills])
    assert [(a.text, a.skill) for a in watched] == [("192.168.1.10:3002", "kuma")]
    assert ignored == []


def test_host_tokens_are_filled_in_before_scanning(skills, monkeypatch):
    # A skill ships with `$HOST` and `worker._localize_skill` substitutes it on
    # the way into the workspace, so the shipped copy is what to check.
    _skill(skills, "portal", "The gallery is at http://$HOST:8500/style")
    watched, _ = addresswatch.scan([skills])
    assert [a.text for a in watched] == [f"{config.SITE.host}:8500"]


def test_a_file_can_declare_one_of_its_addresses_a_placeholder(skills):
    _skill(skills, "sw", (
        "<!-- address-watch-ignore: homeserver:9500 - a placeholder origin. -->\n\n"
        'A snippet: `{"origin": "https://homeserver:9500"}` and the real '
        "portal at http://192.168.1.10:8500/.\n"
    ))
    watched, ignored = addresswatch.scan([skills])
    assert [a.text for a in watched] == ["192.168.1.10:8500"]
    assert [a.text for a in ignored] == ["homeserver:9500"]


def test_an_opt_out_is_scoped_to_the_file_that_declares_it(skills):
    # Otherwise a port some other skill later names for real goes unwatched
    # because an unrelated snippet once used it as an example.
    _skill(skills, "example", "<!-- address-watch-ignore: homeserver:9500 -->\n\n`homeserver:9500`\n")
    _skill(skills, "real", "The service really does answer on `homeserver:9500`.\n")
    watched, ignored = addresswatch.scan([skills])
    assert [(a.text, a.skill) for a in watched] == [("homeserver:9500", "real")]
    assert [(a.text, a.skill) for a in ignored] == [("homeserver:9500", "example")]


def test_several_addresses_can_be_declared_on_one_line(skills):
    _skill(skills, "s", (
        "<!-- address-watch-ignore: some-container:8210, 192.168.1.11:8787 -->\n\n"
        "`some-container:8210` and `192.168.1.11:8787` and `homeserver:8500`\n"
    ))
    watched, ignored = addresswatch.scan([skills])
    assert [a.text for a in watched] == ["homeserver:8500"]
    assert sorted(a.text for a in ignored) == ["192.168.1.11:8787", "some-container:8210"]


def test_one_address_named_by_two_skills_is_probed_once(skills):
    _skill(skills, "a", "`homeserver:8500`")
    _skill(skills, "b", "`homeserver:8500`")
    watched, _ = addresswatch.scan([skills])
    assert [(a.text, a.skill) for a in watched] == [("homeserver:8500", "a")]


def test_a_reference_file_beside_the_skill_is_read_too(skills):
    _skill(skills, "s", "See reference.md.")
    (skills / "s" / "reference.md").write_text("`homeserver:8123`", encoding="utf-8")
    watched, _ = addresswatch.scan([skills])
    assert [(a.text, a.skill, a.source) for a in watched] == [
        ("homeserver:8123", "s", "s/reference.md")
    ]


def test_an_unreadable_skill_does_not_stop_the_scan(skills):
    _skill(skills, "good", "`homeserver:8500`")
    (skills / "bad").mkdir()
    (skills / "bad" / "SKILL.md").write_bytes(b"\xff\xfe not utf-8 \x00 `homeserver:9999`")
    watched, _ = addresswatch.scan([skills])
    assert [a.text for a in watched] == ["homeserver:8500"]


def test_a_missing_root_is_not_an_error(tmp_path):
    assert addresswatch.scan([tmp_path / "nowhere"]) == ([], [])


def test_both_skill_roots_are_scanned(tmp_path, monkeypatch):
    promoted = tmp_path / "skills"          # config.DATA_DIR / "skills"
    built_in = tmp_path / "app-skills"
    _skill(promoted, "promoted", "`homeserver:8501`")
    _skill(built_in, "built-in", "`homeserver:8502`")
    monkeypatch.setattr(config, "SKILLS_DIR", built_in)
    assert [p.name for p in addresswatch.skill_roots()] == ["skills", "app-skills"]
    watched, _ = addresswatch.scan()
    assert sorted(a.text for a in watched) == ["homeserver:8501", "homeserver:8502"]


# --------------------------------------------------------------------------
# The probe
# --------------------------------------------------------------------------

def test_a_listening_port_answers():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert addresswatch.answers("127.0.0.1", port, timeout=2) is True


def test_a_closed_port_does_not_answer():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert addresswatch.answers("127.0.0.1", port, timeout=2) is False


def test_a_name_that_does_not_resolve_does_not_answer():
    assert addresswatch.answers("no-such-host-anywhere.invalid", 8500, timeout=2) is False


def test_the_check_splits_alive_from_dead(skills, monkeypatch):
    _skill(skills, "s", "`homeserver:8500` and `homeserver:8620`")
    monkeypatch.setattr(addresswatch, "answers", lambda host, port, **k: port == 8500)
    result = addresswatch.check([skills])
    assert result["checked"] == 2
    assert [a.text for a in result["alive"]] == ["homeserver:8500"]
    assert [a.text for a in result["dead"]] == ["homeserver:8620"]


# --------------------------------------------------------------------------
# Saying so
# --------------------------------------------------------------------------

def test_a_dead_address_opens_a_todo_on_the_portals_own_project(skills, monkeypatch):
    project = _meta_project()
    _skill(skills, "read-the-filament-list", "GET http://homeserver:8620/case/api/pricing")
    monkeypatch.setattr(addresswatch, "answers", lambda *a, **k: False)
    result = asyncio.run(addresswatch.run_check([skills]))

    assert result["filed"] == ["homeserver:8620"]
    todos = db.list_todos(project["id"])
    assert len(todos) == 1
    assert "read-the-filament-list" in todos[0]["text"]
    assert "homeserver:8620" in todos[0]["text"]
    assert todos[0]["owner"] == "agent"
    assert "cleanup" in (todos[0]["tags"] or "")


def test_the_same_dead_address_does_not_file_a_second_todo_tomorrow(skills, monkeypatch):
    project = _meta_project()
    _skill(skills, "s", "GET http://homeserver:8620/x")
    monkeypatch.setattr(addresswatch, "answers", lambda *a, **k: False)
    asyncio.run(addresswatch.run_check([skills]))
    second = asyncio.run(addresswatch.run_check([skills]))
    assert second["filed"] == []
    assert len(db.list_todos(project["id"])) == 1


def test_a_todo_already_ticked_off_is_not_reopened(skills, monkeypatch):
    project = _meta_project()
    _skill(skills, "s", "GET http://homeserver:8620/x")
    monkeypatch.setattr(addresswatch, "answers", lambda *a, **k: False)
    asyncio.run(addresswatch.run_check([skills]))
    todo = db.list_todos(project["id"])[0]
    db.set_todo_done(todo["id"], True)

    assert asyncio.run(addresswatch.run_check([skills]))["filed"] == []
    rows = db.list_todos(project["id"])
    assert len(rows) == 1 and rows[0]["done"] == 1


def test_the_todo_text_carries_no_date(skills, monkeypatch):
    # A date in the text would defeat `db.add_todo`'s dedupe and file a fresh
    # row every single morning, which is the nagging this replaces.
    address = addresswatch.Address("homeserver", 8620, "filaments", "filaments/SKILL.md")
    assert "2026" not in addresswatch.todo_text(address)


def test_an_alive_address_files_nothing(skills, monkeypatch):
    project = _meta_project()
    _skill(skills, "s", "`homeserver:8500`")
    monkeypatch.setattr(addresswatch, "answers", lambda *a, **k: True)
    assert asyncio.run(addresswatch.run_check([skills]))["filed"] == []
    assert db.list_todos(project["id"]) == []


def test_no_meta_project_is_not_a_crash(skills, monkeypatch):
    _skill(skills, "s", "`homeserver:8620`")
    monkeypatch.setattr(addresswatch, "answers", lambda *a, **k: False)
    assert asyncio.run(addresswatch.run_check([skills]))["ok"] is True


# --------------------------------------------------------------------------
# The record, and the wiring
# --------------------------------------------------------------------------

def test_the_sweep_is_recorded_for_the_settings_card(skills, monkeypatch):
    _skill(skills, "s", (
        "<!-- address-watch-ignore: homeserver:9500 -->\n\n"
        "`homeserver:9500` `homeserver:8500` `homeserver:8620`\n"
    ))
    monkeypatch.setattr(addresswatch, "answers", lambda host, port, **k: port == 8500)
    asyncio.run(addresswatch.run_check([skills]))

    stored = addresswatch.last_result()
    assert stored["checked"] == 2
    assert stored["alive"] == ["homeserver:8500"]
    assert stored["dead"] == [{"address": "homeserver:8620", "skill": "s"}]
    assert stored["ignored"] == ["homeserver:9500"]
    assert stored["checked_at"].startswith("20")


def test_a_garbled_record_reads_back_as_an_empty_one():
    db.set_setting(addresswatch.RESULT_KEY, "{not json")
    assert addresswatch.last_result() == {
        "checked_at": "", "checked": 0, "alive": [], "dead": [], "ignored": []
    }
    db.set_setting(addresswatch.RESULT_KEY, json.dumps([1, 2]))
    assert addresswatch.last_result()["checked"] == 0


def test_run_check_does_nothing_at_all_when_the_setting_is_off(monkeypatch):
    db.set_setting("address_watch", "0")
    called = []
    monkeypatch.setattr(addresswatch, "check", lambda *a, **k: called.append(1))
    out = asyncio.run(addresswatch.run_check())
    assert called == [] and out["ok"] is False


def test_the_watch_is_on_by_default():
    assert addresswatch.enabled() is True


def test_a_thrown_check_never_escapes_run_check(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("sockets on fire")

    monkeypatch.setattr(addresswatch, "check", boom)
    assert asyncio.run(addresswatch.run_check())["ok"] is False


def test_the_worker_checks_once_a_day(monkeypatch):
    calls = []

    async def fake_run_check():
        calls.append(1)
        return {"ok": True, "checked": 0, "alive": [], "dead": [], "ignored": [], "filed": []}

    monkeypatch.setattr(worker.addresswatch, "run_check", fake_run_check)
    monkeypatch.setattr(worker, "_address_checked_day", None, raising=False)
    asyncio.run(worker._daily_address_check())  # noqa: SLF001
    asyncio.run(worker._daily_address_check())  # noqa: SLF001
    assert calls == [1]


def test_a_failing_check_waits_for_tomorrow_rather_than_retrying_every_tick(monkeypatch):
    calls = []

    async def boom():
        calls.append(1)
        raise RuntimeError("down")

    monkeypatch.setattr(worker.addresswatch, "run_check", boom)
    monkeypatch.setattr(worker, "_address_checked_day", None, raising=False)
    asyncio.run(worker._daily_address_check())  # noqa: SLF001
    asyncio.run(worker._daily_address_check())  # noqa: SLF001
    assert calls == [1]


@pytest.mark.asyncio
async def test_the_worker_tick_actually_calls_the_check(monkeypatch):
    """Without this, deleting the one line from `_tick` costs nothing: every
    other test here drives `_daily_address_check` directly."""
    called: list[bool] = []

    async def fake_check():
        called.append(True)

    async def no_start():
        return False

    monkeypatch.setattr(worker, "_daily_address_check", fake_check)
    monkeypatch.setattr(worker, "_start_one", no_start)
    await worker._tick()  # noqa: SLF001
    assert called == [True]


def test_the_setting_round_trips_through_the_settings_form():
    assert "address_watch" in settings_form.KNOWN_KEYS
    assert settings_form.REGISTRY["address_watch"].checkbox is True


def test_the_settings_page_shows_the_toggle_and_the_last_sweep(client, skills, monkeypatch):
    _skill(skills, "filaments", "`homeserver:8620`")
    monkeypatch.setattr(addresswatch, "answers", lambda *a, **k: False)
    asyncio.run(addresswatch.run_check([skills]))

    html = client.get("/settings").text
    assert 'name="address_watch"' in html
    assert "Watch skill addresses" in html
    assert "homeserver:8620" in html
    assert "filaments" in html


def test_the_settings_page_says_so_before_the_first_sweep(client):
    html = client.get("/settings").text
    assert "Nothing checked yet" in html
