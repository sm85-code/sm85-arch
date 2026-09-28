"""SIABUMDES B1 — JWT tenant isolation (aud/iss + optional per-tenant secrets)."""
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


def test_bumdes_token_rejected_by_madrasah_decode(monkeypatch):
    sec = _reload_security(monkeypatch, JWT_SECRET="shared-secret-for-tests")
    token = sec.create_access_token("u1", "admin", 1, tenant="bumdes")
    with pytest.raises(HTTPException) as exc:
        sec.decode_access_token(token, expected_tenant="madrasah")
    assert exc.value.status_code == 401


def test_madrasah_token_rejected_by_bumdes_decode(monkeypatch):
    sec = _reload_security(monkeypatch, JWT_SECRET="shared-secret-for-tests")
    token = sec.create_access_token("u1", "admin", 1, tenant="madrasah")
    with pytest.raises(HTTPException) as exc:
        sec.decode_access_token(token, expected_tenant="bumdes")
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
        JWT_SECRET_BUMDES="bumdes-only-secret",
        JWT_SECRET_MADRASAH="madrasah-only-secret",
    )
    bumdes_token = sec.create_access_token("u1", "admin", 1, tenant="bumdes")
    madrasah_token = sec.create_access_token("u2", "admin", 1, tenant="madrasah")

    assert sec.decode_access_token(bumdes_token, expected_tenant="bumdes")["sub"] == "u1"
    assert sec.decode_access_token(madrasah_token, expected_tenant="madrasah")["sub"] == "u2"

    # Cross-tenant fails even before aud check (wrong secret), and aud check
    # would also reject if secrets were shared.
    with pytest.raises(HTTPException):
        sec.decode_access_token(bumdes_token, expected_tenant="madrasah")
    with pytest.raises(HTTPException):
        sec.decode_access_token(madrasah_token, expected_tenant="bumdes")


def test_fallback_to_shared_secret_when_tenant_unset(monkeypatch):
    sec = _reload_security(
        monkeypatch,
        JWT_SECRET="shared-secret-for-tests",
        JWT_SECRET_BUMDES=None,
        JWT_SECRET_MADRASAH=None,
    )
    assert sec.jwt_secret_for("bumdes") == "shared-secret-for-tests"
    assert sec.jwt_secret_for("madrasah") == "shared-secret-for-tests"
    token = sec.create_access_token("u1", "admin", 1, tenant="bumdes")
    payload = sec.decode_access_token(token, expected_tenant="bumdes")
    assert payload["aud"] == "bumdes"


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
    payload = sec.decode_access_token(legacy, expected_tenant="bumdes")
    assert payload["sub"] == "legacy-user"
    assert "aud" not in payload
