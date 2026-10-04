"""Shopee Push Mechanism (webhook): Shopee POSTs an event to our URL instead of us asking for changes.

Facts from the Open Platform docs (push category + each push's page): every push is JSON
``{"data": {...}, "shop_id": N, "code": C, "timestamp": T}``; Shopee waits only 3 seconds for the answer and retries
after 5 min, 30 min and 3 h when it does not get a success; order_status_push is code 3, order_trackingno_push 4,
shop authorization 1, authorization cancelled 2, authorization expiry 12, return updates 29.

The Authorization header is the HMAC-SHA256 (hex) of ``<callback url>|<raw body>`` with the App partner key
(Open Platform developer guide 18). A separate SHOPEE_PUSH_KEY is accepted too when set. The raw body is used
as received; it is not re-serialized. Every attempt is logged (see ``diagnosis``).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

# URL yang lolos Verify di Open Platform. Env SHOPEE_PUSH_URL menimpa ini bila diisi.
CALLBACK_URL = "https://api.ampelkuning.com/api/marketplace-erp/shopee/push"
# Jenis yang ERP terima: otorisasi, batal otorisasi, status pesanan, resi, masa berlaku otorisasi.
PUSH_AKTIF = [1, 2, 3, 4, 12]

JENIS_PUSH = {
    1: "shop_authorization",
    2: "shop_authorization_canceled",
    3: "order_status",
    4: "order_trackingno",
    12: "open_api_authorization_expiry",
    29: "return_updates",
}
# Pushes that mean "an order changed": the ERP pulls that shop's changes right away.
KODE_PESANAN = frozenset({3, 4})


def push_key() -> str:
    return os.getenv("SHOPEE_PUSH_KEY", "").strip()


def tanda_tangan(key: str, url: str, body: bytes) -> str:
    return hmac.new(key.encode(), url.encode() + b"|" + body, hashlib.sha256).hexdigest()


def kandidat_url(url_terlihat: str, header: dict[str, str]) -> list[str]:
    """The callback URL as Shopee has it: the configured one first, then what the request looked like
    (behind the platform's proxy the scheme/host may differ from what was typed into Open Platform)."""
    hasil: list[str] = []
    atur = os.getenv("SHOPEE_PUSH_URL", "").strip() or CALLBACK_URL
    hasil.append(atur)
    host = header.get("x-forwarded-host") or header.get("host") or ""
    proto = (header.get("x-forwarded-proto") or "").split(",")[0].strip()
    path = url_terlihat.split("://", 1)[-1].split("/", 1)[-1] if "://" in url_terlihat else url_terlihat.lstrip("/")
    if host and proto:
        hasil.append(f"{proto}://{host}/{path}")
    if host:
        hasil.append(f"https://{host}/{path}")
    hasil.append(url_terlihat)
    return list(dict.fromkeys(hasil))


def sidik_jari_kunci(key: str) -> dict[str, Any]:
    """Length plus the first and last two characters: enough to tell two keys apart, useless for using one."""
    return {"panjang": len(key), "awal": key[:2], "akhir": key[-2:]}


def _hmac_hex(key: bytes, pesan: bytes) -> str:
    return hmac.new(key, pesan, hashlib.sha256).hexdigest()


def _variasi_url(urls: list[str]) -> list[str]:
    """The callback URL as typed in Open Platform may differ in small ways from what the server sees."""
    hasil: list[str] = []
    for u in urls:
        for v in (u, u.rstrip("/"), u.rstrip("/") + "/"):
            hasil.append(v)
            if v.startswith("https://"):
                hasil.append("http://" + v[len("https://") :])
    return list(dict.fromkeys(hasil))


def _skema(key_bytes: bytes, urls: list[str], body: bytes) -> dict[str, str]:
    """name -> hex signature of every way the Authorization header could plausibly be built from this key."""
    hasil: dict[str, str] = {}
    for u in _variasi_url(urls):
        ub = u.encode()
        hasil[f"url|badan  [{u}]"] = _hmac_hex(key_bytes, ub + b"|" + body)
        hasil[f"urlbadan  [{u}]"] = _hmac_hex(key_bytes, ub + body)
    hasil["badan"] = _hmac_hex(key_bytes, body)
    return hasil


def _kunci_varian(key: str) -> dict[str, bytes]:
    """The key as text (how Shopee's API signing uses keys) and, when it is hex, also as raw bytes."""
    hasil = {"teks": key.encode()}
    try:
        hasil["hex"] = bytes.fromhex(key)
    except ValueError:
        pass
    return hasil


def verifikasi(
    key: str, urls: list[str], body: bytes, authorization: str | None, kunci_lain: dict[str, str] | None = None
) -> tuple[bool, dict[str, Any]]:
    """(valid, diagnosis). A push is valid when its Authorization is the HMAC-SHA256 (hex) of ``<url>|<body>``
    (developer guide 18) made with the App partner key or SHOPEE_PUSH_KEY. Near variants of the same secret
    (trailing slash, body only) are accepted too. The diagnosis holds the first 8 characters of signatures, never a key."""
    kunci = {}
    if key:
        kunci["push_key"] = key
    for label, lain in (kunci_lain or {}).items():
        if lain:
            kunci[label] = lain
    diagnosis: dict[str, Any] = {"ada_key": bool(kunci), "ada_authorization": bool(authorization), "url_dicoba": urls}
    if key:
        diagnosis["kunci_server"] = sidik_jari_kunci(key)
    if not kunci or not authorization:
        return False, diagnosis
    diterima = authorization.strip().lower()
    diagnosis["diterima"] = diterima[:8]
    utama = key or next(iter(kunci.values()))
    diagnosis["hitung"] = {urls[0]: tanda_tangan(utama, urls[0], body)[:8]} if urls else {}
    for label, rahasia in kunci.items():
        for nama_kunci, kb in _kunci_varian(rahasia).items():
            for nama, sah in _skema(kb, urls, body).items():
                if hmac.compare_digest(sah, diterima):
                    diagnosis["cocok"] = f"{label} (kunci {nama_kunci}), {nama}"
                    return True, diagnosis
    return False, diagnosis


def urai(body: bytes) -> dict[str, Any] | None:
    """The push as a dict with at least ``code``; None when it is not a JSON object."""
    try:
        data = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def ringkas(push: dict[str, Any]) -> dict[str, Any]:
    """What the log shows of one push: the kind, the shop and (for orders) the order number and status."""
    data = push.get("data") if isinstance(push.get("data"), dict) else {}
    kode = push.get("code")
    return {
        "kode": kode if isinstance(kode, int) else None,
        "jenis": JENIS_PUSH.get(kode) if isinstance(kode, int) else None,
        "shop_id": str(push["shop_id"]) if push.get("shop_id") is not None else None,
        "order_sn": str(data.get("ordersn") or data.get("order_sn") or "") or None,
        "status": str(data.get("status") or "") or None,
    }
