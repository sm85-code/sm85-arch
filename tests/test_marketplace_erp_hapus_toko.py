"""Deleting a shop, optionally together with its orders (clearing test data before a real shop is connected)."""
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _toko(session, nama, sn_awal):
    akun = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko=nama))
    rows = [
        {"id_eksternal": f"{sn_awal}{i}", "status": "to_ship", "status_mentah": "PROCESSED", "nama_pembeli": "b",
         "total": Decimal("10"), "items": []}
        for i in range(2)
    ]
    await services.impor_pesanan_marketplace(session, akun, rows)
    return akun


@pytest.mark.asyncio
async def test_deleting_a_shop_with_its_orders_leaves_other_shops_untouched(session):
    uji = await _toko(session, "TES Sandbox", "T")
    asli = await _toko(session, "Toko Asli", "A")
    out = await services.delete_akun_marketplace(session, uji.id, hapus_pesanan=True)
    assert out == {"pesanan_dihapus": 2, "listing_dihapus": 0}
    sisa = await services.list_pesanan(session, platform="shopee")
    assert sorted(p.id_eksternal for p in sisa) == ["A0", "A1"]
    assert [a.nama_toko for a in await services.list_akun_marketplace(session)] == [asli.nama_toko]


@pytest.mark.asyncio
async def test_by_default_orders_stay_when_the_shop_is_removed(session):
    uji = await _toko(session, "TES Sandbox", "T")
    out = await services.delete_akun_marketplace(session, uji.id)
    assert out["pesanan_dihapus"] == 0
    assert len(await services.list_pesanan(session, platform="shopee")) == 2
    assert await services.list_akun_marketplace(session) == []
