"""Listing projection must match shop/item/model and preserve ERP overrides."""
import json
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn, ProdukIn, ProdukListingIn, ProdukListingOut
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import PengaturanStok
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import KatalogShopee


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        # This suite exercises the explicitly enabled warehouse workflow.
        session.add(PengaturanStok(id="global", gudang_aktif=True))
        await session.flush()
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_listing_variant_uses_exact_shop_and_model_without_changing_stock(session):
    a = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform='shopee', nama_toko='A'))
    b = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform='shopee', nama_toko='B'))
    p = await services.create_produk(session, ProdukIn(sku_induk='ERP', nama='Legacy - Merah', harga_dasar=Decimal('100'), stok=9))
    listing = await services.create_listing(session, ProdukListingIn(produk_id=p.id, akun_id=a.id, platform='shopee', id_eksternal='12:34', harga_jual=Decimal('150'), stok_listing=4))
    for shop, title in [(a, 'Kaos'), (b, 'Wrong shop')]:
        session.add(KatalogShopee(akun_id=shop.id, item_id='12', nama=title, sku='INDUK', berat_gram=500, panjang_cm=20, lebar_cm=10, tinggi_cm=5,
            detail_json=json.dumps({'is_pre_order': True, 'days_to_ship': 7}),
            varian_json=json.dumps([{'model_id': '34', 'nama': 'Merah', 'sku': 'M', 'opsi': [{'tier': 'Warna', 'opsi': 'Merah'}], 'harga': '120', 'berat_gram': 600, 'preorder': False}])))
    await session.flush()
    output = ProdukListingOut.model_validate((await services.list_listing(session))[0])
    detail = output.detail_marketplace
    assert detail.nama_produk == 'Kaos'
    assert detail.model_id == '34' and detail.opsi == [{'tier': 'Warna', 'opsi': 'Merah'}]
    assert detail.berat_gram == 600 and detail.panjang_cm == 20
    assert detail.preorder is False and detail.hari_kirim is None
    assert 'panjang_cm' in detail.ikut_produk and 'berat_gram' not in detail.ikut_produk
    assert output.harga_jual == 150 and output.stok_listing == 4
    assert detail.harga == 120 and p.stok == 9 and p.nama == 'Legacy - Merah'
    listing.id_eksternal = '12:missing'
    assert (await services.list_listing(session))[0].detail_marketplace is None


@pytest.mark.asyncio
async def test_legacy_variant_names_are_never_guessed(session):
    a = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform='shopee', nama_toko='A'))
    p = await services.create_produk(session, ProdukIn(sku_induk='ERP', nama='Kaos - Merah', harga_dasar=Decimal('100')))
    await services.create_listing(session, ProdukListingIn(produk_id=p.id, akun_id=a.id, platform='shopee', id_eksternal='12:34'))
    session.add(KatalogShopee(akun_id=a.id, item_id='12', nama='Kaos', varian_json=json.dumps([{'nama': 'Merah', 'sku': 'ERP'}])))
    await session.flush()
    assert (await services.list_listing(session))[0].detail_marketplace is None
