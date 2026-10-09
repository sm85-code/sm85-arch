"""User-facing balance semantics, ledger paging and protected read endpoints."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1 import workflow_router
from tenants.marketplace_erp.modules.marketplace_erp.application import services, workflows
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import Produk, StokLedger


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_balance_uses_latest_record_across_pages_and_keeps_unknown_distinct_from_zero(monkeypatch):
    async def account(*args):
        return SimpleNamespace(id="shop", nama_toko="Toko")

    async def wallet(session, akun, dari, sampai, offset):
        assert (sampai - dari).days == 14
        rows = (
            [{"create_time": 10, "current_balance": "123"}]
            if offset == 0
            else [{"create_time": 20, "current_balance": "0"}]
        )
        return {"items": rows, "next_offset": offset + 1, "ada_lagi": offset == 0}

    async def info(*args):
        return {"region": "ID"}

    monkeypatch.setattr(services, "akun_shopee_pengelolaan", account)
    monkeypatch.setattr(workflows.adapter, "wallet", wallet)
    monkeypatch.setattr(workflows.shopee, "signed_shop_request", info)
    result = await workflows.shop_balance_snapshot(None, "shop")
    assert result["saldo_terakhir"] == "0"
    assert result["saldo_tersedia"] is None and result["saldo_tertahan"] is None
    assert result["transaksi_at"] == 20 and result["currency"] == "IDR"

    async def empty(*args):
        return {"items": [], "next_offset": 0, "ada_lagi": False}

    monkeypatch.setattr(workflows.adapter, "wallet", empty)
    assert (await workflows.shop_balance_snapshot(None, "shop"))["saldo_terakhir"] is None


@pytest.mark.asyncio
async def test_balance_refuses_unbounded_or_nonadvancing_provider_pages(monkeypatch):
    async def account(*args):
        return SimpleNamespace(id="shop", nama_toko="Toko")

    async def wallet(session, akun, dari, sampai, offset):
        return {"items": [{"create_time": 1, "current_balance": "100"}], "next_offset": offset + 1, "ada_lagi": True}

    monkeypatch.setattr(services, "akun_shopee_pengelolaan", account)
    monkeypatch.setattr(workflows.adapter, "wallet", wallet)
    with pytest.raises(HTTPException) as error:
        await workflows.shop_balance_snapshot(None, "shop")
    assert error.value.status_code == 502


@pytest.mark.asyncio
async def test_balance_route_rejects_owner_and_staff_before_provider_calls():
    for role in ("owner", "staff"):
        with pytest.raises(HTTPException) as error:
            await workflow_router.admin(SimpleNamespace(role=role))
        assert error.value.status_code == 403
    assert (await workflow_router.admin(SimpleNamespace(role="admin"))).role == "admin"
    route = next(r for r in workflow_router.router.routes if r.path.endswith("/saldo-toko"))
    assert any(d.call is workflow_router.admin for d in route.dependant.dependencies)


@pytest.mark.asyncio
async def test_ledger_paging_has_stable_order_and_inclusive_wib_dates(session):
    product = Produk(sku_induk="UX", nama="Produk", harga_dasar=0, stok=0)
    session.add(product)
    await session.flush()
    # Jan 2 WIB starts at Jan 1 17:00 UTC.
    start = datetime(2026, 1, 1, 17, tzinfo=timezone.utc)
    for i in range(13):
        session.add(
            StokLedger(
                id=f"{i:02}",
                produk_id=product.id,
                qty_delta=1,
                reason="manual",
                created_at=start + timedelta(minutes=i),
            )
        )
    session.add(
        StokLedger(
            id="before", produk_id=product.id, qty_delta=1, reason="manual", created_at=start - timedelta(seconds=1)
        )
    )
    await session.commit()
    first = await services.list_stok_ledger(
        session, produk_id=product.id, dari=date(2026, 1, 2), sampai=date(2026, 1, 2), limit=10
    )
    second = await services.list_stok_ledger(
        session, produk_id=product.id, dari=date(2026, 1, 2), sampai=date(2026, 1, 2), limit=10, offset=10
    )
    assert len(first) == 10 and len(second) == 3
    assert not {r.id for r in first}.intersection(r.id for r in second)
    assert "before" not in {r.id for r in first + second}


@pytest.mark.asyncio
async def test_balance_does_not_guess_between_conflicting_simultaneous_records(monkeypatch):
    async def account(*args):
        return SimpleNamespace(id="shop", nama_toko="Toko")

    async def wallet(*args):
        return {
            "items": [{"create_time": 20, "current_balance": "100"}, {"create_time": 20, "current_balance": "200"}],
            "next_offset": 0,
            "ada_lagi": False,
        }

    async def info(*args):
        return {"region": "ID"}

    monkeypatch.setattr(services, "akun_shopee_pengelolaan", account)
    monkeypatch.setattr(workflows.adapter, "wallet", wallet)
    monkeypatch.setattr(workflows.shopee, "signed_shop_request", info)
    assert (await workflows.shop_balance_snapshot(None, "shop"))["saldo_terakhir"] is None
