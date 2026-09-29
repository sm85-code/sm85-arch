"""Marketplace ERP seed-now gate (madrasah-style)."""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

# shared.database / JWT imports may need a placeholder DATABASE_URL in CI.
if not (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")):
    os.environ["DATABASE_URL"] = "postgresql://placeholder@localhost/placeholder"

from tenants.marketplace_erp.adapters.api.v1.marketplace_erp_router import (
    _marketplace_erp_seed_secret_ok,
    authorize_marketplace_erp_seed,
    marketplace_erp_router,
)


def test_seed_now_route_has_authorize_dependency():
    route = next(r for r in marketplace_erp_router.routes if getattr(r, "path", None) == "/seed-now")
    dep_names = [getattr(dep.call, "__name__", "") for dep in route.dependant.dependencies]
    assert "authorize_marketplace_erp_seed" in dep_names


def test_seed_secret_header_accepts_matching_env(monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ERP_SEED_SECRET", "s3cret-value")
    req = SimpleNamespace(headers={"X-Marketplace-Erp-Seed-Secret": "s3cret-value"})
    assert _marketplace_erp_seed_secret_ok(req) is True


def test_seed_secret_header_accepts_generic_x_seed_secret(monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ERP_SEED_SECRET", "s3cret-value")
    req = SimpleNamespace(headers={"X-Seed-Secret": "s3cret-value"})
    assert _marketplace_erp_seed_secret_ok(req) is True


def test_seed_secret_header_rejects_mismatch(monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ERP_SEED_SECRET", "s3cret-value")
    req = SimpleNamespace(headers={"X-Marketplace-Erp-Seed-Secret": "wrong"})
    assert _marketplace_erp_seed_secret_ok(req) is False


def test_seed_secret_header_rejects_when_env_empty(monkeypatch):
    monkeypatch.delenv("MARKETPLACE_ERP_SEED_SECRET", raising=False)
    req = SimpleNamespace(headers={"X-Marketplace-Erp-Seed-Secret": "anything"})
    assert _marketplace_erp_seed_secret_ok(req) is False


@pytest.mark.asyncio
async def test_authorize_seed_allows_valid_secret_without_user(monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ERP_SEED_SECRET", "bootstrap")
    req = SimpleNamespace(headers={"X-Seed-Secret": "bootstrap"})
    result = await authorize_marketplace_erp_seed(req, session=MagicMock())
    assert result is None


@pytest.mark.asyncio
async def test_authorize_seed_production_blocks_anonymous_without_secret(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("MARKETPLACE_ERP_SEED_SECRET", raising=False)
    req = SimpleNamespace(headers={}, cookies={}, client=SimpleNamespace(host="1.2.3.4"))

    async def _boom(*_a, **_k):
        raise HTTPException(status_code=401, detail="Tidak terautentikasi")

    with patch(
        "tenants.marketplace_erp.adapters.api.v1.marketplace_erp_router.get_current_user_marketplace_erp",
        new=_boom,
    ):
        with pytest.raises(HTTPException) as exc:
            await authorize_marketplace_erp_seed(req, session=MagicMock())
    assert exc.value.status_code == 403
    assert "seed-now" in str(exc.value.detail).lower() or "production" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_authorize_seed_accepts_owner(monkeypatch):
    monkeypatch.delenv("MARKETPLACE_ERP_SEED_SECRET", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    owner = SimpleNamespace(role="owner")
    req = SimpleNamespace(headers={})

    async def _user(*_a, **_k):
        return owner

    with patch(
        "tenants.marketplace_erp.adapters.api.v1.marketplace_erp_router.get_current_user_marketplace_erp",
        new=_user,
    ):
        result = await authorize_marketplace_erp_seed(req, session=MagicMock())
    assert result is owner
