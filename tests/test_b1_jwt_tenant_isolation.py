"""B1 — JWT tenant isolation (aud/iss + optional per-tenant secrets)."""
from __future__ import annotations

import importlib

import pytest
from fastapi import HTTPException


def _reload_security(monkeypatch, **env):
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    # Ensure shared fallback is always present for encode.
    if "JWT_SECRET" not in env:
        monkeypatch.setenv("JWT_SECRET", "shared-secret-for-tests")
    from shared import config as config_module

    importlib.reload(config_module)
    import shared.security as security_module

    importlib.reload(security_module)
    return security_module


def test_toko_token_rejected_by_madrasah_decode(monkeypatch):
    sec = _reload_security(monkeypatch, JWT_SECRET="shared-secret-for-tests")
    token = sec.create_access_token("u1", "admin", 1, tenant="toko")
    with pytest.raises(HTTPException) as exc:
        sec.decode_access_token(token, expected_tenant="madrasah")
    assert exc.value.status_code == 401


def test_madrasah_token_rejected_by_marketplace_erp_decode(monkeypatch):
    sec = _reload_security(monkeypatch, JWT_SECRET="shared-secret-for-tests")
    token = sec.create_access_token("u1", "admin", 1, tenant="madrasah")
    with pytest.raises(HTTPException) as exc:
        sec.decode_access_token(token, expected_tenant="marketplace_erp")
    assert exc.value.status_code == 401


def test_toko_token_round_trip_with_expected_tenant(monkeypatch):
    sec = _reload_security(monkeypatch, JWT_SECRET="shared-secret-for-tests")
    token = sec.create_access_token("u1", "admin_toko", 0, tenant="toko")
    payload = sec.decode_access_token(token, expected_tenant="toko")
    assert payload["sub"] == "u1"
    assert payload["aud"] == "toko"
    assert payload["iss"] == "sm85:toko"


def test_tenant_specific_secret_preferred(monkeypatch):
    sec = _reload_security(
        monkeypatch,
        JWT_SECRET="shared-secret-for-tests",
        JWT_SECRET_TOKO="toko-only-secret",
        JWT_SECRET_MADRASAH="madrasah-only-secret",
    )
    toko_token = sec.create_access_token("u1", "admin", 1, tenant="toko")
    madrasah_token = sec.create_access_token("u2", "admin", 1, tenant="madrasah")

    assert sec.decode_access_token(toko_token, expected_tenant="toko")["sub"] == "u1"
    assert sec.decode_access_token(madrasah_token, expected_tenant="madrasah")["sub"] == "u2"

    # Cross-tenant fails even before aud check (wrong secret), and aud check
    # would also reject if secrets were shared.
    with pytest.raises(HTTPException):
        sec.decode_access_token(toko_token, expected_tenant="madrasah")
    with pytest.raises(HTTPException):
        sec.decode_access_token(madrasah_token, expected_tenant="toko")


def test_fallback_to_shared_secret_when_tenant_unset(monkeypatch):
    sec = _reload_security(
        monkeypatch,
        JWT_SECRET="shared-secret-for-tests",
        JWT_SECRET_MADRASAH=None,
        JWT_SECRET_TOKO=None,
        JWT_SECRET_MARKETPLACE_ERP=None,
    )
    for tenant in ("madrasah", "toko", "marketplace_erp"):
        assert sec.jwt_secret_for(tenant) == "shared-secret-for-tests"
        token = sec.create_access_token("u1", "admin", 1, tenant=tenant)
        payload = sec.decode_access_token(token, expected_tenant=tenant)
        assert payload["aud"] == tenant
        assert payload["iss"] == f"sm85:{tenant}"


def test_legacy_token_without_aud_iss_still_accepted(monkeypatch):
    """Migration window: pre-B1 tokens (no aud/iss) must not lock out live users."""
    sec = _reload_security(monkeypatch, JWT_SECRET="shared-secret-for-tests")
    from datetime import datetime, timedelta, timezone

    from jose import jwt as jose_jwt

    now = datetime.now(timezone.utc)
    legacy = jose_jwt.encode(
        {
            "sub": "legacy-user",
            "role": "admin",
            "sv": 1,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        "shared-secret-for-tests",
        algorithm="HS256",
    )
    payload = sec.decode_access_token(legacy, expected_tenant="madrasah")
    assert payload["sub"] == "legacy-user"
    assert "aud" not in payload
