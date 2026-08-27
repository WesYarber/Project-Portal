"""Web push to Wes's enrolled devices (RFC 8030 delivery, RFC 8291 aes128gcm
encryption, RFC 8292 VAPID auth).

Implemented directly on `cryptography` primitives rather than pywebpush: the
whole protocol is ~100 lines, every byte of it is pinned by RFC 8291's own
Appendix A test vector in tests/test_webpush.py, and one audited dependency
beats three unaudited ones.

Payloads are sent in the Declarative Web Push shape (`Content-Type:
application/notification+json`, body `{"web_push": 8030, "notification":
{...}}`): on iOS 18.4+ the OS displays them without ever waking a service
worker - which is what makes a lock-screen notification from the portal
survive Safari's aggressive PWA process killing. Browsers without declarative
support (Chrome) get the same JSON handed to app/static/sw.js, which shows it
from the classic `push` event. The push service ignores the content type
either way, so one payload shape serves both worlds.

Best-effort end to end, like every other notify channel: a push failure is
logged and swallowed, never raised into the worker or a request handler.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app import config, db, netinfo

log = logging.getLogger("portal.webpush")

# Who to blame: push services may contact this address about misbehaving
# senders (RFC 8292 requires a sub claim). Configured, not hard-coded - see
# `contact_email` in app/site.py. The fallback is well-formed and inert rather
# than absent, because the claim is mandatory and a missing one is a rejected
# push, not a degraded one.
def vapid_sub() -> str:
    configured = (config.SITE.contact_email or "").strip()
    if not configured:
        return f"mailto:portal@{config.SITE.host}"
    return configured if "://" in configured or configured.startswith("mailto:") else f"mailto:{configured}"
# A notification that arrives a day late is still context worth having; older
# than that and the question it announced has usually been answered.
TTL_SEC = 24 * 3600
JWT_LIFETIME_SEC = 12 * 3600
# RFC 8188 record size. Everything the portal sends fits in one record.
RECORD_SIZE = 4096
# What that leaves for the JSON before encryption. RFC 8291 spends 86 bytes of
# the record on the aes128gcm header (16 salt + 4 length + 1 + 65 key), 16 more
# on the GCM tag and 1 on the record's padding delimiter; the rest is ours.
# Rounded down for slack, because a push service rejects an oversized body
# rather than truncating it.
MAX_PLAINTEXT = RECORD_SIZE - 128


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64u_decode(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --- the VAPID keypair ------------------------------------------------------

def _keys_path() -> Path:
    # Beside the DB, not in it: a keypair is a credential, and Wes can rotate
    # it by deleting the file (every device then needs to re-enroll).
    return config.DATA_DIR / "vapid.json"


def _load_or_create() -> ec.EllipticCurvePrivateKey:
    """The portal's application-server keypair, generated once and kept.

    A new keypair invalidates every existing subscription (the browser binds
    the subscription to the key it was created with), so an unreadable file
    falls through to a fresh key rather than raising - re-enrolling is the
    recovery either way.
    """
    path = _keys_path()
    try:
        raw = json.loads(path.read_text())
        secret = int.from_bytes(_b64u_decode(raw["private"]), "big")
        return ec.derive_private_key(secret, ec.SECP256R1())
    except Exception:  # noqa: BLE001 - missing or corrupt file → new key
        pass
    key = ec.generate_private_key(ec.SECP256R1())
    secret = key.private_numbers().private_value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "private": _b64u(secret.to_bytes(32, "big")),
                "public": _b64u(_public_bytes(key)),
            },
            indent=2,
        )
    )
    return key


def _public_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    """The uncompressed P-256 point (65 bytes, 0x04-prefixed) - the wire form
    both the subscribe call and the encryption header use."""
    return key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def public_key_b64() -> str:
    """What the browser passes as `applicationServerKey`."""
    return _b64u(_public_bytes(_load_or_create()))


# --- VAPID (RFC 8292) -------------------------------------------------------

def vapid_auth(endpoint: str, now: Optional[int] = None) -> str:
    """The Authorization header for one push endpoint.

    The audience is the push service's origin, not the full endpoint - Apple
    and Mozilla both reject a JWT whose aud carries the path.
    """
    key = _load_or_create()
    parts = urlsplit(endpoint)
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int(now if now is not None else time.time()) + JWT_LIFETIME_SEC,
        "sub": vapid_sub(),
    }
    header = {"typ": "JWT", "alg": "ES256"}
    signing_input = (
        _b64u(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64u(json.dumps(claims, separators=(",", ":")).encode())
    )
    der = key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
    # JOSE wants the raw 64-byte r||s form, not the DER cryptography emits.
    r, s = decode_dss_signature(der)
    token = signing_input + "." + _b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={token}, k={_b64u(_public_bytes(key))}"


# --- encryption (RFC 8291) --------------------------------------------------

def encrypt(
    plaintext: bytes,
    p256dh: str,
    auth: str,
    *,
    _as_private: Optional[ec.EllipticCurvePrivateKey] = None,
    _salt: Optional[bytes] = None,
) -> bytes:
    """One aes128gcm message for one subscription.

    `_as_private` and `_salt` exist so the test can drive the exact RFC 8291
    Appendix A vector through this code; production always generates fresh
    ones (the ephemeral key is what makes each message forward-secret).
    """
    ua_public = _b64u_decode(p256dh)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    as_key = _as_private or ec.generate_private_key(ec.SECP256R1())
    as_public = _public_bytes(as_key)
    ecdh_secret = as_key.exchange(ec.ECDH(), ua_key)
    ikm = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_b64u_decode(auth),
        info=b"WebPush: info\x00" + ua_public + as_public,
    ).derive(ecdh_secret)
    salt = _salt or os.urandom(16)
    cek = HKDF(
        algorithm=hashes.SHA256(), length=16, salt=salt, info=b"Content-Encoding: aes128gcm\x00"
    ).derive(ikm)
    nonce = HKDF(
        algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00"
    ).derive(ikm)
    # 0x02 marks the (only) record as the last one; no padding beyond that.
    record = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = (
        salt
        + RECORD_SIZE.to_bytes(4, "big")
        + len(as_public).to_bytes(1, "big")
        + as_public
    )
    return header + record


# --- payload ----------------------------------------------------------------

def portal_url() -> str:
    """Where a tapped notification should open. The tailnet HTTPS address when
    it exists (the only one that works off wifi), else the LAN one."""
    snap = netinfo.cached() or {}
    return (
        snap.get("https_url")
        or snap.get("lan_url")
        or f"http://{config.HOST_LABEL}:{config.PORT}/"
    )


def payload(
    title: str,
    message: str,
    navigate: str,
    app_badge: Optional[int] = None,
    actions: Optional[list] = None,
) -> bytes:
    """One Declarative Web Push notification, as JSON bytes.

    `app_badge` paints a number on the home-screen icon (0 clears it). It is
    omitted rather than guessed when the caller does not know whose device this
    is: iOS leaves the badge alone for a payload that carries no `app_badge`,
    and an unchanged stale number beats a confidently wrong one.

    `actions` are the buttons on the notification itself - see
    `quickreplies.push_actions`. Each navigates to a URL rather than firing a
    background fetch, which is what the spec allows, so an answer button has to
    land on a page that submits itself.

    Shrinking, in that order, if the result will not fit a push service's 4 KB
    record: buttons first (the notification still works, it just costs a tap),
    then the body. A payload that overflows is refused outright by the push
    service, so a question that happened to be long would silently never reach
    a phone at all.
    """
    def build(body: str, acts: Optional[list]) -> bytes:
        notification: dict = {"title": title, "body": body, "navigate": navigate}
        if app_badge is not None:
            notification["app_badge"] = max(0, int(app_badge))
        if acts:
            notification["actions"] = list(acts)
        return json.dumps(
            {"web_push": 8030, "notification": notification}, ensure_ascii=False
        ).encode()

    out = build(message, actions)
    if len(out) <= MAX_PLAINTEXT and actions:
        return out
    if actions:
        out = build(message, None)
        if len(out) <= MAX_PLAINTEXT:
            log.info("push payload too long for its buttons; sent without them")
            return out
    if len(out) <= MAX_PLAINTEXT:
        return out
    # Still too long with no buttons at all: the body is the only part left.
    # Trim by however much it overflows, plus room for the ellipsis.
    over = len(out) - MAX_PLAINTEXT
    trimmed = message.encode()[: max(0, len(message.encode()) - over - 8)].decode(
        errors="ignore"
    )
    log.warning("push body trimmed by %d bytes to fit the record", over)
    return build(trimmed.rstrip() + "…", None)


# --- sending ----------------------------------------------------------------

async def send_one(sub: dict, body: bytes, urgency: str = "normal") -> bool:
    """POST one encrypted message to one subscription's push service.

    A 404/410 means the browser dropped the subscription (app removed,
    permission revoked) - the row is deleted so we stop paying for a dead
    endpoint. Other failures keep the row and bump its failure count.
    """
    headers = {
        "Authorization": vapid_auth(sub["endpoint"]),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/notification+json",
        "TTL": str(TTL_SEC),
        "Urgency": urgency,
    }
    encrypted = encrypt(body, sub["p256dh"], sub["auth"])
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(sub["endpoint"], content=encrypted, headers=headers)
    if resp.status_code in (404, 410):
        log.info("push subscription expired (%s), dropping it", resp.status_code)
        db.delete_push_subscription(sub["endpoint"])
        return False
    ok = 200 <= resp.status_code < 300
    db.mark_push_result(sub["endpoint"], ok)
    if not ok:
        log.warning("push to %s… answered %s", sub["endpoint"][:48], resp.status_code)
    return ok


async def push_all(title: str, message: str, urgency: str = "normal") -> int:
    """Send to every enrolled device. Never raises; returns accepted sends.

    Deliberately unrouted: this is the settings page's test button and anything
    else that means *every* device, not "the devices of the people this
    concerns". Routed sends go through `push_to`; see app/routing.py.
    """
    return await push_to(db.list_push_subscriptions(), title, message, urgency)


def _person_of(sub) -> Optional[int]:
    """The person a device belongs to, or None for one enrolled before the
    column existed. Rows are sqlite3.Row here and plain dicts in tests, and
    neither answers `.get` the way the other does."""
    try:
        value = sub["person_id"]
    except (KeyError, IndexError):
        return None
    return None if value is None else int(value)


async def push_to(
    subs,
    title: str,
    message: str,
    urgency: str = "normal",
    badges: Optional[dict] = None,
    actions: Optional[list] = None,
) -> int:
    """Send to the given subscriptions. Never raises; returns accepted sends.

    `badges` maps a person's id to the number their home-screen icon should
    show. It is a map rather than one number because the count is per person -
    Wes's three open questions are not Karli's - and one send reaches both of
    their phones. A device whose person is not in the map (or has none, being
    enrolled before the column existed) is sent no `app_badge` at all, so its
    icon keeps whatever it had rather than wearing somebody else's number.
    """
    try:
        if not subs:
            return 0
        url = portal_url()
        # One body per distinct badge value, not per device: two of Wes's
        # phones get byte-identical JSON, and the encryption below is what is
        # actually per-device anyway.
        bodies: dict[Optional[int], bytes] = {}

        def body_for(sub) -> bytes:
            count = (badges or {}).get(_person_of(sub))
            if count not in bodies:
                bodies[count] = payload(title, message, url, count, actions)
            return bodies[count]

        sent = 0
        for sub in subs:
            body = body_for(sub)
            try:
                if await send_one(dict(sub), body, urgency):
                    sent += 1
            except Exception as exc:  # noqa: BLE001 - one dead device must not mute the rest
                log.warning("push to %s… failed: %s", sub["endpoint"][:48], exc)
                try:
                    db.mark_push_result(sub["endpoint"], False)
                except Exception:  # noqa: BLE001
                    pass
        return sent
    except Exception:  # noqa: BLE001 - a notify channel never raises
        log.exception("web push failed")
        return 0
