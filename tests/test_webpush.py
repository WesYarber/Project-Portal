"""Web push (app/webpush.py): VAPID, RFC 8291 encryption, delivery and the
enrollment surface.

The encryption is pinned two independent ways: RFC 8291's own Appendix A test
vector byte-for-byte (proving interop with every conforming push service), and
a round-trip through a decrypt implemented here from the receiver's side of
the spec (proving each new random message is well-formed, not just the one
the vector fixes).
"""
from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from starlette.testclient import TestClient

from app import config, db, netinfo, notify, webpush


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def client(temp_data_dir):
    from app import main

    # Not a context manager on purpose: lifespan would start the worker.
    return TestClient(main.app)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --- the VAPID keypair ------------------------------------------------------


def test_vapid_keypair_is_created_once_and_persists(temp_data_dir):
    first = webpush.public_key_b64()
    assert (temp_data_dir / "vapid.json").exists()
    assert webpush.public_key_b64() == first


def test_public_key_is_an_uncompressed_p256_point(temp_data_dir):
    raw = b64d(webpush.public_key_b64())
    assert len(raw) == 65
    assert raw[0] == 0x04


def test_a_corrupt_keys_file_regenerates_instead_of_crashing(temp_data_dir):
    (temp_data_dir / "vapid.json").write_text("not json{")
    key = webpush.public_key_b64()
    assert len(b64d(key)) == 65
    # And the fresh key was persisted over the corpse.
    saved = json.loads((temp_data_dir / "vapid.json").read_text())
    assert saved["public"] == key


# --- VAPID auth (RFC 8292) --------------------------------------------------


def test_vapid_auth_is_a_valid_es256_jwt_for_the_endpoint_origin(temp_data_dir):
    header_value = webpush.vapid_auth(
        "https://web.push.apple.com/QOX8vNXV0sYCV7v3Ako", now=1_700_000_000
    )
    assert header_value.startswith("vapid t=")
    token_part, key_part = header_value[len("vapid ") :].split(", ")
    token = token_part[len("t=") :]
    pub_b64 = key_part[len("k=") :]
    assert pub_b64 == webpush.public_key_b64()

    head_b64, claims_b64, sig_b64 = token.split(".")
    assert json.loads(b64d(head_b64)) == {"typ": "JWT", "alg": "ES256"}
    claims = json.loads(b64d(claims_b64))
    # Origin only - Apple rejects an aud that carries the endpoint path.
    assert claims["aud"] == "https://web.push.apple.com"
    assert claims["sub"].startswith("mailto:")
    assert claims["exp"] == 1_700_000_000 + webpush.JWT_LIFETIME_SEC

    # The signature must verify against the advertised public key, in the
    # JOSE raw-r||s form.
    sig = b64d(sig_b64)
    assert len(sig) == 64
    der = encode_dss_signature(
        int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    )
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64d(pub_b64))
    pub.verify(der, f"{head_b64}.{claims_b64}".encode(), ec.ECDSA(hashes.SHA256()))


def test_vapid_exp_defaults_to_the_near_future(temp_data_dir):
    header_value = webpush.vapid_auth("https://fcm.googleapis.com/fcm/send/abc")
    token = header_value.split("t=")[1].split(",")[0]
    claims = json.loads(b64d(token.split(".")[1]))
    assert time.time() < claims["exp"] <= time.time() + webpush.JWT_LIFETIME_SEC + 5


# --- encryption (RFC 8291) --------------------------------------------------

# Appendix A of RFC 8291, verbatim (whitespace removed).
RFC_PLAINTEXT = b64d("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24")
RFC_AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
RFC_UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
RFC_UA_PRIVATE = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
RFC_SALT = b64d("DGv6ra1nlYgDCS1FRnbzlw")
RFC_AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
RFC_HEADER = (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
    "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
)
RFC_CIPHERTEXT = (
    "8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ"
)


def test_rfc8291_appendix_a_vector_byte_for_byte():
    as_private = ec.derive_private_key(
        int.from_bytes(b64d(RFC_AS_PRIVATE), "big"), ec.SECP256R1()
    )
    body = webpush.encrypt(
        RFC_PLAINTEXT, RFC_UA_PUBLIC, RFC_AUTH, _as_private=as_private, _salt=RFC_SALT
    )
    assert body == b64d(RFC_HEADER) + b64d(RFC_CIPHERTEXT)


def _decrypt(body: bytes, ua_private: ec.EllipticCurvePrivateKey, auth_secret: bytes) -> bytes:
    """The receiver's half of RFC 8291, written from the spec."""
    salt = body[:16]
    record_size = int.from_bytes(body[16:20], "big")
    assert record_size == webpush.RECORD_SIZE
    idlen = body[20]
    as_public = body[21 : 21 + idlen]
    record = body[21 + idlen :]
    as_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    ecdh_secret = ua_private.exchange(ec.ECDH(), as_key)
    ua_public = ua_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    ikm = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=auth_secret,
        info=b"WebPush: info\x00" + ua_public + as_public,
    ).derive(ecdh_secret)
    cek = HKDF(
        algorithm=hashes.SHA256(), length=16, salt=salt, info=b"Content-Encoding: aes128gcm\x00"
    ).derive(ikm)
    nonce = HKDF(
        algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00"
    ).derive(ikm)
    plaintext = AESGCM(cek).decrypt(nonce, record, None)
    assert plaintext.endswith(b"\x02")
    return plaintext[:-1]


def test_a_fresh_message_round_trips_through_a_spec_side_decrypt():
    ua_private = ec.generate_private_key(ec.SECP256R1())
    ua_public = ua_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    auth_secret = b"0123456789abcdef"
    message = json.dumps({"hello": "watermelon"}).encode()
    body = webpush.encrypt(message, b64u(ua_public), b64u(auth_secret))
    assert _decrypt(body, ua_private, auth_secret) == message
    # Ephemeral key + fresh salt: the same plaintext never encrypts the same way.
    assert webpush.encrypt(message, b64u(ua_public), b64u(auth_secret)) != body


# --- payload and navigate URL -----------------------------------------------


def test_payload_is_the_declarative_web_push_shape():
    data = json.loads(webpush.payload("Q7", "Should I?", "https://x.example/").decode())
    assert data["web_push"] == 8030
    assert data["notification"] == {
        "title": "Q7",
        "body": "Should I?",
        "navigate": "https://x.example/",
    }


def test_portal_url_prefers_the_tailnet_https_address(temp_data_dir):
    assert webpush.portal_url() == f"http://{config.HOST_LABEL}:{config.PORT}/"
    netinfo.store(
        {
            "fetched_at": int(time.time()),
            "lan_url": "http://testhost:8500/",
            "https": True,
            "https_url": "https://testhost.tailnet1234.ts.net/",
            "self": None,
            "peers": [],
            "acl_known": False,
        }
    )
    assert webpush.portal_url() == "https://testhost.tailnet1234.ts.net/"


# --- sending ----------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeClient:
    """Stands in for httpx.AsyncClient; records every POST."""

    calls: list[dict] = []
    outcome: object = 201  # an int status, or an Exception to raise

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def post(self, url, content=None, headers=None):
        _FakeClient.calls.append({"url": url, "content": content, "headers": headers})
        if isinstance(_FakeClient.outcome, Exception):
            raise _FakeClient.outcome
        return _FakeResponse(int(_FakeClient.outcome))


@pytest.fixture
def fake_push_service(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.outcome = 201
    monkeypatch.setattr(webpush.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


def _enroll(endpoint: str = "https://web.push.apple.com/sub1") -> str:
    ua_private = ec.generate_private_key(ec.SECP256R1())
    ua_public = ua_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    db.add_push_subscription(endpoint, b64u(ua_public), b64u(b"0123456789abcdef"), ua="iPhone")
    return endpoint


@pytest.mark.anyio
async def test_push_all_sends_an_encrypted_declarative_message(temp_data_dir, fake_push_service):
    endpoint = _enroll()
    sent = await webpush.push_all("Project Portal", "Run finished.", urgency="high")
    assert sent == 1
    (call,) = fake_push_service.calls
    assert call["url"] == endpoint
    headers = call["headers"]
    assert headers["Content-Encoding"] == "aes128gcm"
    assert headers["Content-Type"] == "application/notification+json"
    assert headers["TTL"] == str(webpush.TTL_SEC)
    assert headers["Urgency"] == "high"
    assert headers["Authorization"].startswith("vapid t=")
    # The body is ciphertext, not the JSON itself.
    assert b"Run finished." not in call["content"]
    assert len(call["content"]) > 86
    row = db.list_push_subscriptions()[0]
    assert row["last_ok_at"] is not None
    assert row["failures"] == 0


@pytest.mark.anyio
async def test_a_410_from_the_push_service_drops_the_subscription(temp_data_dir, fake_push_service):
    _enroll()
    fake_push_service.outcome = 410
    sent = await webpush.push_all("t", "m")
    assert sent == 0
    assert db.list_push_subscriptions() == []


@pytest.mark.anyio
async def test_a_500_counts_a_failure_but_keeps_the_row(temp_data_dir, fake_push_service):
    _enroll()
    fake_push_service.outcome = 500
    assert await webpush.push_all("t", "m") == 0
    row = db.list_push_subscriptions()[0]
    assert row["failures"] == 1


@pytest.mark.anyio
async def test_push_all_never_raises_even_when_the_network_does(temp_data_dir, fake_push_service):
    _enroll()
    fake_push_service.outcome = RuntimeError("network down")
    assert await webpush.push_all("t", "m") == 0
    assert db.list_push_subscriptions()[0]["failures"] == 1


@pytest.mark.anyio
async def test_push_all_without_subscriptions_touches_nothing(temp_data_dir, fake_push_service):
    assert await webpush.push_all("t", "m") == 0
    assert fake_push_service.calls == []


@pytest.mark.anyio
async def test_one_dead_device_does_not_mute_the_rest(temp_data_dir, monkeypatch):
    _enroll("https://push.example/dead")
    _enroll("https://push.example/alive")

    async def flaky(sub, body, urgency="normal"):
        if "dead" in sub["endpoint"]:
            raise RuntimeError("boom")
        return True

    monkeypatch.setattr(webpush, "send_one", flaky)
    assert await webpush.push_all("t", "m") == 1


# --- notify wiring ----------------------------------------------------------


# `notify` sends through `push_to` rather than `push_all` since notifications
# grew a routing layer on 2026-07-28: `push_all` means every device on the
# install and is now only the settings page's test button, while a real
# notification pushes to the devices of the people it is addressed to. See
# app/routing.py and tests/test_routing.py.


@pytest.mark.anyio
async def test_notify_pushes_to_enrolled_devices(temp_data_dir, monkeypatch):
    pushed = []

    async def fake_push_to(subs, title, message, urgency="normal"):
        pushed.append((title, message, urgency))
        return 1

    monkeypatch.setattr(webpush, "push_to", fake_push_to)
    await notify.notify("Project Portal", "A run finished.")
    assert pushed == [("Project Portal", "A run finished.", "normal")]


@pytest.mark.anyio
async def test_notify_marks_questions_urgent(temp_data_dir, monkeypatch):
    pushed = []

    async def fake_push_to(subs, title, message, urgency="normal"):
        pushed.append(urgency)
        return 1

    monkeypatch.setattr(webpush, "push_to", fake_push_to)
    project = db.create_project("P", stage="active")
    question = db.create_question(project["id"], "Which one?")
    await notify.notify("Question", "Which one?", question_id=question["id"], question_slot=1)
    assert pushed == ["high"]


@pytest.mark.anyio
async def test_the_test_button_still_reaches_every_device(temp_data_dir, monkeypatch):
    """`push_all` is deliberately unrouted. The settings page's test push has
    to reach the phone somebody is holding whether or not they are on any
    project, or "send test push" answers a different question than the one the
    person pressing it is asking."""
    reached = []

    async def fake_push_to(subs, title, message, urgency="normal"):
        reached.extend(s["endpoint"] for s in subs)
        return len(subs)

    db.add_push_subscription("https://push/a", "p", "a", person_id=None)
    db.add_push_subscription("https://push/b", "p", "a", person_id=99)
    monkeypatch.setattr(webpush, "push_to", fake_push_to)
    await webpush.push_all("Project Portal", "Test push.")
    assert reached == ["https://push/a", "https://push/b"]


# --- the HTTP surface -------------------------------------------------------


def test_pubkey_endpoint_serves_the_application_server_key(client):
    resp = client.get("/push/pubkey")
    assert resp.status_code == 200
    assert len(b64d(resp.json()["key"])) == 65


def test_subscribe_stores_and_reenrolling_replaces_keys_in_place(client):
    body = {
        "endpoint": "https://web.push.apple.com/abc",
        "keys": {"p256dh": "AAA", "auth": "BBB"},
    }
    assert client.post("/push/subscribe", json=body).status_code == 200
    body["keys"] = {"p256dh": "CCC", "auth": "DDD"}
    assert client.post("/push/subscribe", json=body).status_code == 200
    (row,) = db.list_push_subscriptions()
    assert row["p256dh"] == "CCC"
    assert row["auth"] == "DDD"


def test_subscribe_rejects_junk(client):
    assert client.post("/push/subscribe", content=b"not json").status_code == 400
    assert client.post("/push/subscribe", json={"endpoint": "https://x/"}).status_code == 400
    assert (
        client.post(
            "/push/subscribe",
            json={"endpoint": "http://plain.example/", "keys": {"p256dh": "A", "auth": "B"}},
        ).status_code
        == 400
    )
    assert db.list_push_subscriptions() == []


def test_unsubscribe_and_the_remove_button(client):
    client.post(
        "/push/subscribe",
        json={"endpoint": "https://p.example/1", "keys": {"p256dh": "A", "auth": "B"}},
    )
    client.post(
        "/push/subscribe",
        json={"endpoint": "https://p.example/2", "keys": {"p256dh": "A", "auth": "B"}},
    )
    assert client.post("/push/unsubscribe", json={"endpoint": "https://p.example/1"}).status_code == 200
    (row,) = db.list_push_subscriptions()
    resp = client.post(f"/push/remove/{row['id']}", follow_redirects=False)
    assert resp.status_code == 303
    assert db.list_push_subscriptions() == []


def test_sw_js_is_served_from_the_origin_root(client):
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert "addEventListener('push'" in resp.text


def test_test_push_button_reports_the_send_count(client, monkeypatch):
    async def fake_push_all(title, message, urgency="normal"):
        return 2

    monkeypatch.setattr(webpush, "push_all", fake_push_all)
    resp = client.post("/settings/test-push", follow_redirects=False)
    assert resp.status_code == 303
    assert "push_sent=2" in resp.headers["location"]


def test_settings_page_shows_the_push_card(client):
    page = client.get("/settings").text
    assert "phone push" in page
    assert "enable-push" in page
    assert "none enrolled" in page

    client.post(
        "/push/subscribe",
        json={"endpoint": "https://p.example/1", "keys": {"p256dh": "A", "auth": "B"}},
    )
    page = client.get("/settings").text
    assert "1 device" in page
    assert "remove-push" in page
    assert "send test push" in page
