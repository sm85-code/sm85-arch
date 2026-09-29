"""B0 security: madrasah /seed-now gate.

(The SIABUMDES GDrive OAuth callback tests moved with SIABUMDES to
sm85-code/backend-siabumdes.)

No HTTP test client in this suite (same convention as test_madrasah_security_hardening.py):
exercise route dependency wiring and pure helpers directly.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

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
