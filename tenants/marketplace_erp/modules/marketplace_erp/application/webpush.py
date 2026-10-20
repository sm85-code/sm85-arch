"""Web Push (RFC 8030, 8291, 8292) with `cryptography` and `httpx` only: VAPID signing and aes128gcm payload encryption.

VAPID_PUBLIC_KEY is the uncompressed P-256 public key and VAPID_PRIVATE_KEY the raw 32-byte private key, both base64url
(the format browsers and the usual key generators use). VAPID_SUBJECT is a `mailto:` or https contact for the push service.
"""
from __future__ import annotations

import base64
import json
import os
import time
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# A subscription endpoint comes from the browser, so only the real push services are ever contacted.
PUSH_HOSTS = (
    "fcm.googleapis.com",
    "push.services.mozilla.com",
    "push.apple.com",
    "notify.windows.com",
)
RECORD_SIZE = 4096
MAX_PAYLOAD = 3000


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def endpoint_valid(endpoint: str) -> bool:
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and len(endpoint) <= 1000 and any(host == h or host.endswith("." + h) for h in PUSH_HOSTS)


def generate_keys() -> tuple[str, str]:
    """(public, private) in the environment format."""
    private = ec.generate_private_key(ec.SECP256R1())
    raw_private = private.private_numbers().private_value.to_bytes(32, "big")
    raw_public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64e(raw_public), b64e(raw_private)


def configured() -> tuple[str, str, str] | None:
    public, private = os.getenv("VAPID_PUBLIC_KEY", "").strip(), os.getenv("VAPID_PRIVATE_KEY", "").strip()
    subject = os.getenv("VAPID_SUBJECT", "").strip()
    if not (public and private and subject):
        return None
    return public, private, subject


def vapid_authorization(endpoint: str, public: str, private: str, subject: str, *, now: float | None = None) -> str:
    key = ec.derive_private_key(int.from_bytes(b64d(private), "big"), ec.SECP256R1())
    parts = urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int((now or time.time()) + 12 * 3600), "sub": subject}
    signing = b64e(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()) + "." + b64e(json.dumps(claims, separators=(",", ":")).encode())
    r, s = decode_dss_signature(key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
    return f"vapid t={signing}.{b64e(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}, k={public}"


def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: bytes | None = None, ephemeral: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """aes128gcm body (RFC 8291) for one subscriber; `salt` and `ephemeral` are only fixed in tests."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("Payload terlalu besar")
    ua_public = b64d(p256dh)
    sender = ephemeral or ec.generate_private_key(ec.SECP256R1())
    sender_public = sender.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    secret = sender.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = HKDF(hashes.SHA256(), 32, b64d(auth), b"WebPush: info\x00" + ua_public + sender_public).derive(secret)
    salt = salt or os.urandom(16)
    key = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(sender_public)]) + sender_public
    return header + AESGCM(key).encrypt(nonce, payload + b"\x02", None)


async def send(subscription: dict, payload: dict, *, ttl: int = 3600, client: httpx.AsyncClient | None = None) -> int:
    """POST one notification; the push service's status code (201 delivered, 404/410 = subscription gone)."""
    settings = configured()
    if not settings or not endpoint_valid(subscription["endpoint"]):
        return 0
    public, private, subject = settings
    body = encrypt(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), subscription["p256dh"], subscription["auth"])
    headers = {
        "Authorization": vapid_authorization(subscription["endpoint"], public, private, subject),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(ttl),
        "Urgency": "high",
    }
    owned = client is None
    client = client or httpx.AsyncClient(timeout=10, follow_redirects=False)
    try:
        return (await client.post(subscription["endpoint"], content=body, headers=headers)).status_code
    except httpx.HTTPError:
        return 0
    finally:
        if owned:
            await client.aclose()
