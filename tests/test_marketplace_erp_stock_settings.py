from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn, ItemPesananIn, PesananIn, ProdukIn, ProdukPatch, StokAdjustIn
from tenants.marketplace_erp.modules.marketplace_erp.application.stock_settings import get_settings, require_warehouse
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import StokLedger, StokReservasi, Gudang


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_default_reference_stock_and_order_do_not_touch_warehouse(session):
    assert (await get_settings(session))['mode'] == 'per_toko'
    master = await services.create_produk(session, ProdukIn(sku_induk='REF', nama='Master', harga_dasar=Decimal('100'), stok_referensi=40))
    await services.update_produk(session, master.id, ProdukPatch(stok_referensi=70))
    shop = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform='shopee', nama_toko='Shop'))
    order = await services.create_pesanan(session, PesananIn(platform='shopee', id_eksternal='O', akun_id=shop.id, items=[ItemPesananIn(nama_produk='Master', harga_satuan=Decimal('100'), produk_id=master.id, qty=5)]))
    await services.ubah_status_pesanan(session, order.id, 'to_ship')
    await services.ubah_status_pesanan(session, order.id, 'shipped')
    assert master.stok_referensi == 70 and master.stok == 0
    for model in [StokLedger, StokReservasi, Gudang]:
        assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.asyncio
async def test_dormant_warehouse_blocks_adjust_and_push_guard(session):
    master = await services.create_produk(session, ProdukIn(sku_induk='REF', nama='Master', harga_dasar=Decimal('100')))
    with pytest.raises(HTTPException) as error:
        await services.adjust_stok(session, StokAdjustIn(produk_id=master.id, qty_delta=5))
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        await require_warehouse(session)
    assert error.value.status_code == 409
    with pytest.raises(HTTPException):
        await services.create_produk(session, ProdukIn(sku_induk='PHYSICAL', nama='Master', harga_dasar=Decimal('100'), stok=5))
