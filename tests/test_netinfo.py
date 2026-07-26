"""How the portal reports its own reachability (app/netinfo.py).

Nothing here shells out: `_tailscale` is the only function that runs a
subprocess, and everything downstream of it is a pure function over the JSON,
which is why the module is split that way.

The fixtures are trimmed copies of real `tailscale` output, with the hosts and
addresses renamed, and they keep the detail that made the whole feature
necessary: the portal's node carries `tag:core`, and the filter it was handed
only admits `tag:tagged-devices`, so render-box - a machine signed in as the
owner's own account - is dropped.
"""
from __future__ import annotations

import json
import time

import pytest
from starlette.testclient import TestClient

from app import config, db, netinfo

SELF_IP = "100.64.0.6"
PHONE_IP = "100.64.0.7"
RENDERBOX_IP = "100.64.0.5"


def status_payload():
    return {
        "Self": {
            "HostName": "testhost",
            "DNSName": "testhost.tailnet1234.ts.net.",
            "TailscaleIPs": [SELF_IP, "fd7a:115c:a1e0::6"],
            "Tags": ["tag:core"],
        },
        "Peer": {
            "a": {
                "HostName": "render-box",
                "DNSName": "render-box.tailnet1234.ts.net.",
                "TailscaleIPs": [RENDERBOX_IP],
                "OS": "linux",
                "Online": True,
            },
            "b": {
                # iOS really does report its hostname as "localhost"; the
                # tailnet name is the only usable label for the owner's phone.
                "HostName": "localhost",
                "DNSName": "iphone-15-pro-max.tailnet1234.ts.net.",
                "TailscaleIPs": [PHONE_IP],
                "OS": "iOS",
                "Tags": ["tag:tagged-devices"],
                "Online": True,
            },
            "c": {
                "HostName": "testhost-old",
                "DNSName": "testhost-old.tailnet1234.ts.net.",
                "TailscaleIPs": ["100.64.0.8"],
                "OS": "linux",
                "Online": False,
            },
        },
    }


def netmap_payload(srcs=None):
    return {
        "PacketFilter": [
            {
                "IPProto": [6, 17, 1, 58],
                "Dsts": [
                    {"Net": "0.0.0.0/0", "Ports": {"First": 0, "Last": 65535}},
                    {"Net": "::/0", "Ports": {"First": 0, "Last": 65535}},
                ],
                "Srcs": srcs if srcs is not None else [PHONE_IP + "/32", SELF_IP + "/32"],
            }
        ]
    }


def serve_payload(port=config.PORT, host_port="testhost.tailnet1234.ts.net:443"):
    return {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {host_port: {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{port}"}}}},
    }


# --- status -----------------------------------------------------------------

def test_a_peer_is_named_by_its_tailnet_name_not_its_hostname():
    peers = netinfo.parse_status(status_payload())["peers"]
    assert "iphone-15-pro-max" in [p["host"] for p in peers]
    assert "localhost" not in [p["host"] for p in peers]


def test_a_peer_with_no_dns_name_falls_back_to_its_hostname():
    raw = status_payload()
    del raw["Peer"]["a"]["DNSName"]
    assert "render-box" in [p["host"] for p in netinfo.parse_status(raw)["peers"]]


def test_status_reads_self_and_strips_the_trailing_dot():
    parsed = netinfo.parse_status(status_payload())
    assert parsed["self"]["dns"] == "testhost.tailnet1234.ts.net"
    assert parsed["self"]["tags"] == ["tag:core"]
    assert SELF_IP in parsed["self"]["ips"]


def test_status_lists_peers_online_first_then_by_name():
    peers = netinfo.parse_status(status_payload())["peers"]
    assert [p["host"] for p in peers] == ["iphone-15-pro-max", "render-box", "testhost-old"]
    assert peers[-1]["online"] is False


@pytest.mark.parametrize("raw", [None, [], "nonsense", {}])
def test_status_survives_junk(raw):
    parsed = netinfo.parse_status(raw)
    assert parsed["peers"] == []


def test_status_ignores_a_non_dict_peer():
    raw = status_payload()
    raw["Peer"]["bad"] = "not a peer"
    assert len(netinfo.parse_status(raw)["peers"]) == 3


# --- serve ------------------------------------------------------------------

def test_serve_reports_the_https_url():
    parsed = netinfo.parse_serve(serve_payload())
    assert parsed["https"] is True
    assert parsed["url"] == "https://testhost.tailnet1234.ts.net/"
    assert parsed["target"] == f"http://127.0.0.1:{config.PORT}"


def test_serve_on_a_non_443_port_keeps_the_port_in_the_url():
    parsed = netinfo.parse_serve(serve_payload(host_port="testhost.tailnet1234.ts.net:8443"))
    assert parsed["url"] == "https://testhost.tailnet1234.ts.net:8443/"


def test_serve_fronting_a_different_service_is_not_the_portal():
    """A serve config for something else on this host must not be reported as
    the portal being reachable over HTTPS - the link would open Jellyfin."""
    parsed = netinfo.parse_serve(serve_payload(port=8096))
    assert parsed["https"] is False
    assert parsed["url"] == ""


def test_serve_tolerates_a_trailing_slash_on_the_proxy_target():
    raw = serve_payload()
    host = "testhost.tailnet1234.ts.net:443"
    raw["Web"][host]["Handlers"]["/"]["Proxy"] = f"http://127.0.0.1:{config.PORT}/"
    assert netinfo.parse_serve(raw)["https"] is True


@pytest.mark.parametrize("raw", [None, {}, {"Web": {}}, {"Web": {"h:443": {}}}, "junk"])
def test_serve_absent_means_no_https(raw):
    assert netinfo.parse_serve(raw)["https"] is False


# --- the packet filter, i.e. the actual answer to the 8500 puzzle -----------

def test_filter_collects_the_allowed_sources():
    allowed = netinfo.parse_filter(netmap_payload(), [SELF_IP])
    assert allowed == [PHONE_IP + "/32", SELF_IP + "/32"]


def test_filter_missing_is_unknown_not_empty():
    """None and [] mean completely different things: 'we could not read the
    filter' must never render as 'no machine can reach the portal'."""
    assert netinfo.parse_filter(None, [SELF_IP]) is None
    assert netinfo.parse_filter({"PacketFilter": "junk"}, [SELF_IP]) is None
    assert netinfo.parse_filter({"PacketFilter": []}, [SELF_IP]) == []


def test_filter_ignores_a_rule_that_does_not_cover_our_port():
    raw = netmap_payload()
    raw["PacketFilter"][0]["Dsts"] = [{"Net": "0.0.0.0/0", "Ports": {"First": 22, "Last": 22}}]
    assert netinfo.parse_filter(raw, [SELF_IP]) == []


def test_filter_ignores_a_rule_aimed_at_a_different_host():
    raw = netmap_payload()
    raw["PacketFilter"][0]["Dsts"] = [{"Net": "100.99.99.99/32", "Ports": {"First": 0, "Last": 65535}}]
    assert netinfo.parse_filter(raw, [SELF_IP]) == []


def test_filter_dedupes_sources_across_rules():
    raw = netmap_payload()
    raw["PacketFilter"].append(dict(raw["PacketFilter"][0]))
    assert netinfo.parse_filter(raw, [SELF_IP]) == [PHONE_IP + "/32", SELF_IP + "/32"]


@pytest.mark.parametrize(
    "dst",
    [
        {"Net": "not-a-network", "Ports": {"First": 0, "Last": 65535}},
        {"Net": "0.0.0.0/0", "Ports": {"First": "x", "Last": "y"}},
        {"Net": "0.0.0.0/0"},
        "junk",
    ],
)
def test_filter_survives_a_malformed_destination(dst):
    assert netinfo.parse_filter({"PacketFilter": [{"Dsts": [dst], "Srcs": ["1.2.3.4/32"]}]}, [SELF_IP]) == []


# --- reach ------------------------------------------------------------------

def test_the_phone_reaches_the_portal_and_render_box_does_not():
    """The finding this whole module exists for. render-box is signed in as the
    owner's own account and is still blocked, because testhost is tag:core and
    the ACL only admits tag:tagged-devices."""
    peers = netinfo.parse_status(status_payload())["peers"]
    annotated = netinfo.annotate_reach(peers, netinfo.parse_filter(netmap_payload(), [SELF_IP]))
    reach = {p["host"]: p["reach"] for p in annotated}
    assert reach["iphone-15-pro-max"] == "yes"
    assert reach["render-box"] == "blocked"


def test_a_wide_open_acl_reaches_everything():
    peers = netinfo.parse_status(status_payload())["peers"]
    annotated = netinfo.annotate_reach(peers, ["0.0.0.0/0"])
    assert {p["reach"] for p in annotated} == {"yes"}


def test_no_filter_reading_marks_every_peer_unknown():
    peers = netinfo.parse_status(status_payload())["peers"]
    annotated = netinfo.annotate_reach(peers, None)
    assert {p["reach"] for p in annotated} == {"unknown"}


def test_blocked_peers_excludes_offline_machines():
    snap = {
        "acl_known": True,
        "peers": [
            {"host": "render-box", "reach": "blocked", "online": True},
            {"host": "old-laptop", "reach": "blocked", "online": False},
            {"host": "phone", "reach": "yes", "online": True},
        ],
    }
    assert [p["host"] for p in netinfo.blocked_peers(snap)] == ["render-box"]


def test_blocked_peers_says_nothing_when_the_acl_is_unknown():
    snap = {"acl_known": False, "peers": [{"host": "x", "reach": "unknown", "online": True}]}
    assert netinfo.blocked_peers(snap) == []
    assert netinfo.blocked_peers(None) == []


# --- snapshot + cache -------------------------------------------------------

@pytest.fixture
def fake_cli(monkeypatch):
    calls = []

    def run(*args):
        calls.append(args)
        if args[:1] == ("status",):
            return status_payload()
        if args[:2] == ("serve", "status"):
            return serve_payload()
        if args[:2] == ("debug", "netmap"):
            return netmap_payload()
        return None

    monkeypatch.setattr(netinfo, "_tailscale", run)
    return calls


def test_snapshot_puts_the_whole_picture_together(fake_cli):
    snap = netinfo.snapshot()
    assert snap["https"] is True
    assert snap["https_url"] == "https://testhost.tailnet1234.ts.net/"
    assert snap["lan_url"] == f"http://{config.HOST_LABEL}:{config.PORT}/"
    assert snap["acl_known"] is True
    assert [p["host"] for p in netinfo.blocked_peers(snap)] == ["render-box"]


def test_snapshot_without_tailscale_reports_nothing_rather_than_raising(monkeypatch):
    monkeypatch.setattr(netinfo, "_tailscale", lambda *a: None)
    snap = netinfo.snapshot()
    assert snap["self"] is None
    assert snap["https"] is False
    # No self address means no way to know which filter rules apply to us, and
    # guessing would be worse than saying nothing.
    assert snap["acl_known"] is False


def test_snapshot_never_asks_for_the_netmap_without_a_self_address(monkeypatch):
    calls = []
    monkeypatch.setattr(netinfo, "_tailscale", lambda *a: calls.append(a))
    netinfo.snapshot()
    assert ("debug", "netmap") not in calls


def test_cache_roundtrips_and_ages(fake_cli, monkeypatch):
    snap = netinfo.snapshot()
    netinfo.store(snap)
    monkeypatch.setattr(time, "time", lambda: snap["fetched_at"] + 300)
    cached = netinfo.cached()
    assert cached["age_sec"] == 300
    assert cached["https_url"] == snap["https_url"]


def test_cache_empty_and_corrupt_read_as_nothing():
    assert netinfo.cached() is None
    db.set_setting(netinfo.CACHE_KEY, "{not json")
    assert netinfo.cached() is None
    db.set_setting(netinfo.CACHE_KEY, "[1, 2]")
    assert netinfo.cached() is None


def test_tailscale_helper_returns_none_when_the_binary_is_missing(monkeypatch):
    monkeypatch.setattr(netinfo.shutil, "which", lambda name: None)
    assert netinfo._tailscale("status", "--json") is None


def test_tailscale_helper_returns_none_on_a_nonzero_exit(monkeypatch):
    class Proc:
        returncode = 1
        stdout = ""
        stderr = "Access denied: cert access denied"

    monkeypatch.setattr(netinfo.shutil, "which", lambda name: "/usr/bin/tailscale")
    monkeypatch.setattr(netinfo.subprocess, "run", lambda *a, **k: Proc())
    assert netinfo._tailscale("cert") is None


def test_tailscale_helper_returns_none_on_unparseable_output(monkeypatch):
    class Proc:
        returncode = 0
        stdout = "not json at all"
        stderr = ""

    monkeypatch.setattr(netinfo.shutil, "which", lambda name: "/usr/bin/tailscale")
    monkeypatch.setattr(netinfo.subprocess, "run", lambda *a, **k: Proc())
    assert netinfo._tailscale("status", "--json") is None


# --- the pages --------------------------------------------------------------

@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def test_settings_access_panel_names_the_https_url_and_the_blocked_machine(client, fake_cli):
    netinfo.store(netinfo.snapshot())
    body = client.get("/settings").text
    assert "https://testhost.tailnet1234.ts.net/" in body
    assert 'data-panel="access"' in body
    assert "render-box" in body
    assert "reach-blocked" in body


def test_settings_renders_with_no_tailnet_reading_at_all(client):
    body = client.get("/settings").text
    assert body.count("panel-access") == 1
    assert "No tailnet reading yet" in body


def test_settings_offers_the_setup_script_when_https_is_off(client, monkeypatch, fake_cli):
    monkeypatch.setattr(netinfo, "parse_serve", lambda raw, target_port=config.PORT: {"https": False, "url": "", "target": ""})
    netinfo.store(netinfo.snapshot())
    body = client.get("/settings").text
    assert "tailscale-https.sh" in body


def test_every_page_carries_the_secure_url_for_the_microphone_hint(client, fake_cli):
    netinfo.store(netinfo.snapshot())
    body = client.get("/").text
    assert 'data-secure-url="https://testhost.tailnet1234.ts.net/"' in body


def test_no_secure_url_attribute_when_there_is_no_https(client):
    assert "data-secure-url" not in client.get("/").text


def test_api_tailnet_serves_the_cache_without_shelling_out(client, monkeypatch):
    monkeypatch.setattr(netinfo, "_tailscale", lambda *a: pytest.fail("must not run tailscale"))
    db.set_setting(netinfo.CACHE_KEY, json.dumps({"fetched_at": 0, "https": True}))
    assert client.get("/api/tailnet").json()["https"] is True


def test_api_tailnet_refresh_takes_a_live_reading(client, fake_cli):
    body = client.get("/api/tailnet?refresh=1").json()
    assert body["https_url"] == "https://testhost.tailnet1234.ts.net/"
    # ...and stores it, so the next plain read is warm.
    assert netinfo.cached()["https_url"] == body["https_url"]


# --- the poller -------------------------------------------------------------

@pytest.mark.asyncio
async def test_poller_waits_before_its_first_reading(monkeypatch):
    """This service restarts itself on every self-update; a run of restarts
    must not become a run of subprocesses."""
    import asyncio

    slept = []

    async def sleep(sec):
        slept.append(sec)
        if len(slept) > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(netinfo.asyncio, "sleep", sleep)
    monkeypatch.setattr(netinfo, "snapshot", lambda: {"self": {"dns": "x"}, "fetched_at": 1})
    with pytest.raises(asyncio.CancelledError):
        await netinfo.poll_loop()
    assert slept[0] == netinfo.STARTUP_DELAY_SEC
    assert netinfo.cached() is not None


@pytest.mark.asyncio
async def test_a_failed_reading_does_not_blank_a_good_one(monkeypatch, fake_cli):
    import asyncio

    netinfo.store(netinfo.snapshot())
    good = netinfo.cached()["https_url"]

    async def sleep(sec):
        if sec == netinfo.POLL_INTERVAL_SEC:
            raise asyncio.CancelledError

    monkeypatch.setattr(netinfo.asyncio, "sleep", sleep)
    monkeypatch.setattr(netinfo, "snapshot", lambda: {"self": None, "fetched_at": 2, "https_url": ""})
    with pytest.raises(asyncio.CancelledError):
        await netinfo.poll_loop()
    assert netinfo.cached()["https_url"] == good


@pytest.mark.asyncio
async def test_a_raising_snapshot_never_kills_the_poller(monkeypatch):
    import asyncio

    calls = []

    async def sleep(sec):
        calls.append(sec)
        if len(calls) > 2:
            raise asyncio.CancelledError

    def boom():
        raise RuntimeError("tailscaled is wedged")

    monkeypatch.setattr(netinfo.asyncio, "sleep", sleep)
    monkeypatch.setattr(netinfo, "snapshot", boom)
    with pytest.raises(asyncio.CancelledError):
        await netinfo.poll_loop()
    assert netinfo.POLL_INTERVAL_SEC in calls


def test_startup_registers_the_tailnet_poller():
    import inspect

    from app import main

    assert "netinfo.poll_loop()" in inspect.getsource(main.on_startup)
