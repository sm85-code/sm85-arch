"""Shopee adapter: sign helper + authorize URL structure (no live Partner Key)."""
from __future__ import annotations

import hashlib
import hmac
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee


def test_sign_request_matches_hmac_sha256(monkeypatch):
    monkeypatch.setenv("SHOPEE_PARTNER_ID", "12345")
    monkeypatch.setenv("SHOPEE_PARTNER_KEY", "test-partner-key")
    # Reload module-level cached env by calling with patched getattr via re-read
    erp_shopee.SHOPEE_PARTNER_ID = "12345"
    erp_shopee.SHOPEE_PARTNER_KEY = "test-partner-key"
    ts = 1_700_000_000
    path = "/api/v2/shop/auth_partner"
    got = erp_shopee.sign_request(path, ts)
    expected = hmac.new(
        b"test-partner-key",
        f"12345{path}{ts}".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert got == expected


def test_build_authorize_url_shape(monkeypatch):
    erp_shopee.SHOPEE_PARTNER_ID = "12345"
    erp_shopee.SHOPEE_PARTNER_KEY = "test-partner-key"
    erp_shopee.SHOPEE_ENV = "sandbox"
    url = erp_shopee.build_authorize_url(redirect_uri="https://example.com/cb")
    parsed = urlparse(url)
    assert parsed.netloc == "partner.test-stable.shopeemobile.com"
    assert parsed.path == "/api/v2/shop/auth_partner"
    q = parse_qs(parsed.query)
    assert q["partner_id"] == ["12345"]
    assert q["redirect"] == ["https://example.com/cb"]
    assert re.fullmatch(r"[0-9a-f]{64}", q["sign"][0])


def test_build_authorize_url_requires_partner(monkeypatch):
    erp_shopee.SHOPEE_PARTNER_ID = ""
    erp_shopee.SHOPEE_PARTNER_KEY = ""
    with pytest.raises(HTTPException) as exc:
        erp_shopee.build_authorize_url(redirect_uri="https://example.com/cb")
    assert exc.value.status_code == 501


def test_apply_token_payload_sets_akun_fields():
    akun = SimpleNamespace(
        access_token=None,
        refresh_token=None,
        id_toko_eksternal=None,
        token_kedaluwarsa=None,
        status="belum_terhubung",
    )
    erp_shopee.apply_token_payload(
        akun,
        {"access_token": "at", "refresh_token": "rt", "expire_in": 3600, "shop_id": 99},
    )
    assert akun.access_token == "at"
    assert akun.refresh_token == "rt"
    assert akun.id_toko_eksternal == "99"
    assert akun.status == "terhubung"
    assert akun.token_kedaluwarsa is not None


def test_map_shopee_status():
    assert erp_shopee.map_shopee_status("READY_TO_SHIP") == "to_ship"
    assert erp_shopee.map_shopee_status("CANCELLED") == "cancelled"


def test_live_sync_off_by_default(monkeypatch):
    erp_shopee.SHOPEE_LIVE_SYNC = False
    erp_shopee.SHOPEE_PARTNER_ID = "1"
    erp_shopee.SHOPEE_PARTNER_KEY = "k"
    assert erp_shopee.live_sync_enabled() is False


def test_marketplace_erp_never_answers_503_from_application_code():
    """DigitalOcean App Platform replaces an application 503 with its own HTML 504 page, so the owner
    saw "504 Gateway Timeout" instead of "Shopee belum dikonfigurasi" (found in production on
    /oauth/shopee/start). "Not configured yet" is 501."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "tenants" / "marketplace_erp"
    offenders = [
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if "HTTP_503" in p.read_text() or "status_code=503" in p.read_text()
    ]
    assert offenders == []
