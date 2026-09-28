"""B0 security: madrasah /seed-now gate + GDrive OAuth callback hardening.

No HTTP test client in this suite (same convention as test_madrasah_security_hardening.py):
exercise route dependency wiring and pure helpers directly.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from jose import jwt

# shared.database raises at import when DATABASE_URL is unset (CI leaves it
# unset). transaction_router pulls that in — use a placeholder so collection
# succeeds; these tests never open a real DB connection.
if not (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")):
    os.environ["DATABASE_URL"] = "postgresql://placeholder@localhost/placeholder"

from modules.siabumdes.adapters.api.v1 import transaction_router as tx
from tenants.madrasah.adapters.api.v1.madrasah_router import (
    _madrasah_seed_secret_ok,
    authorize_madrasah_seed,
    madrasah_router,
)

def test_seed_now_route_has_authorize_dependency():
    route = next(r for r in madrasah_router.routes if getattr(r, "path", None) == "/seed-now")
    dep_names = [getattr(dep.call, "__name__", "") for dep in route.dependant.dependencies]
    assert "authorize_madrasah_seed" in dep_names


def test_seed_secret_header_accepts_matching_env(monkeypatch):
    monkeypatch.setenv("MADRASAH_SEED_SECRET", "s3cret-value")
    req = SimpleNamespace(headers={"X-Madrasah-Seed-Secret": "s3cret-value"})
    assert _madrasah_seed_secret_ok(req) is True


def test_seed_secret_header_rejects_mismatch(monkeypatch):
    monkeypatch.setenv("MADRASAH_SEED_SECRET", "s3cret-value")
    req = SimpleNamespace(headers={"X-Madrasah-Seed-Secret": "wrong"})
    assert _madrasah_seed_secret_ok(req) is False


def test_seed_secret_header_rejects_when_env_empty(monkeypatch):
    monkeypatch.delenv("MADRASAH_SEED_SECRET", raising=False)
    req = SimpleNamespace(headers={"X-Madrasah-Seed-Secret": "anything"})
    assert _madrasah_seed_secret_ok(req) is False


@pytest.mark.asyncio
async def test_authorize_seed_allows_valid_secret_without_user(monkeypatch):
    monkeypatch.setenv("MADRASAH_SEED_SECRET", "bootstrap")
    req = SimpleNamespace(headers={"X-Seed-Secret": "bootstrap"})
    result = await authorize_madrasah_seed(req, session=MagicMock())
    assert result is None


@pytest.mark.asyncio
async def test_authorize_seed_production_blocks_anonymous_without_secret(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("MADRASAH_SEED_SECRET", raising=False)
    req = SimpleNamespace(headers={}, cookies={}, client=SimpleNamespace(host="1.2.3.4"))

    async def _boom(*_a, **_k):
        raise HTTPException(status_code=401, detail="Tidak terautentikasi")

    with patch(
        "tenants.madrasah.adapters.api.v1.madrasah_router.get_current_user_madrasah",
        new=_boom,
    ):
        with pytest.raises(HTTPException) as exc:
            await authorize_madrasah_seed(req, session=MagicMock())
    assert exc.value.status_code == 403
    assert "production" in str(exc.value.detail).lower() or "seed-now" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_authorize_seed_accepts_app_admin(monkeypatch):
    monkeypatch.delenv("MADRASAH_SEED_SECRET", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    admin = SimpleNamespace(role="admin", tugas_list=[])
    req = SimpleNamespace(headers={})

    async def _user(*_a, **_k):
        return admin

    with patch(
        "tenants.madrasah.adapters.api.v1.madrasah_router.get_current_user_madrasah",
        new=_user,
    ):
        with patch(
            "tenants.madrasah.modules.madrasah.application.services.effective_roles",
            return_value={"admin"},
        ):
            result = await authorize_madrasah_seed(req, session=MagicMock())
    assert result is admin


def test_gdrive_oauth_callback_requires_admin_role_dependency():
    route = next(r for r in tx.router.routes if r.path == "/api/admin/gdrive/oauth-callback")
    # require_roles returns an inner `_inner` callable as the dependency
    role_deps = [
        dep for dep in route.dependant.dependencies if getattr(dep.call, "__name__", "") == "_inner"
    ]
    assert role_deps, "oauth-callback must require admin via require_roles"


def test_gdrive_oauth_state_roundtrip(monkeypatch):
    monkeypatch.setattr(tx, "JWT_SECRET", "test-secret-for-gdrive-state")
    state = tx._make_gdrive_oauth_state()
    tx._verify_gdrive_oauth_state(state)  # does not raise


def test_gdrive_oauth_state_rejects_tampered(monkeypatch):
    monkeypatch.setattr(tx, "JWT_SECRET", "test-secret-for-gdrive-state")
    state = tx._make_gdrive_oauth_state()
    with pytest.raises(HTTPException) as exc:
        tx._verify_gdrive_oauth_state(state + "x")
    assert exc.value.status_code == 403


def test_gdrive_oauth_state_rejects_wrong_purpose(monkeypatch):
    monkeypatch.setattr(tx, "JWT_SECRET", "test-secret-for-gdrive-state")
    bad = jwt.encode(
        {"purpose": "other", "exp": 9999999999},
        "test-secret-for-gdrive-state",
        algorithm=tx.JWT_ALGORITHM,
    )
    with pytest.raises(HTTPException) as exc:
        tx._verify_gdrive_oauth_state(bad)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_gdrive_oauth_callback_response_omits_refresh_token(monkeypatch):
    monkeypatch.setattr(tx, "JWT_SECRET", "test-secret-for-gdrive-state")
    state = tx._make_gdrive_oauth_state()

    fake_creds = SimpleNamespace(refresh_token="rt-super-secret")
    fake_flow = MagicMock()
    fake_flow.credentials = fake_creds
    fake_flow.fetch_token = MagicMock()

    req = SimpleNamespace(base_url="https://api.example/")
    admin = SimpleNamespace(role="admin")

    with patch.object(tx, "oauth_flow", return_value=fake_flow):
        with patch.object(tx.logger, "warning") as warn:
            out = await tx.gdrive_oauth_callback(req, code="auth-code", state=state, _=admin)

    assert "refresh_token" not in out
    assert out.get("ok") is True
    assert "TIDAK dikirim" in out["detail"] or "tidak" in out["detail"].lower()
    # Token may appear in server log for ops bootstrap — never in HTTP body.
    assert warn.called
    assert "rt-super-secret" not in str(out)


@pytest.mark.asyncio
async def test_gdrive_oauth_callback_rejects_bad_state(monkeypatch):
    monkeypatch.setattr(tx, "JWT_SECRET", "test-secret-for-gdrive-state")
    req = SimpleNamespace(base_url="https://api.example/")
    with pytest.raises(HTTPException) as exc:
        await tx.gdrive_oauth_callback(req, code="auth-code", state="nope", _=SimpleNamespace())
    assert exc.value.status_code == 403
