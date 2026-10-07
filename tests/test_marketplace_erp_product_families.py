"""Parents organize SKU variants without taking ownership of inventory/history."""
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import ProdukIn, ProdukKeluargaIn, ProdukOut, ProdukPatch, ProdukVarianOpsi
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import ProdukVarian, StokLedger


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def family(session):
    return await services.create_produk_keluarga(session, ProdukKeluargaIn(nama='Kaos', tiers=['Warna', 'Ukuran']))


def options(color='Merah', size='M'):
    return [ProdukVarianOpsi(tier='Ukuran', opsi=size), ProdukVarianOpsi(tier='Warna', opsi=color)]


@pytest.mark.asyncio
async def test_grouping_old_sku_preserves_title_price_stock_and_ledger(session):
    product = await services.create_produk(session, ProdukIn(sku_induk='RED-M', nama='Kaos - Merah M', harga_dasar=Decimal('15000'), stok=8, berat_gram=350, preorder=True, hari_proses=7))
    ledger_before = list((await session.execute(select(StokLedger))).scalars())
    parent = await family(session)
    output = await services.update_produk(session, product.id, ProdukPatch(keluarga_id=parent['id'], opsi_varian=options()))
    dto = ProdukOut.model_validate(output)
    assert dto.nama_induk == 'Kaos' and dto.nama == 'Kaos - Merah M'
    assert dto.stok == 8 and dto.harga_dasar == 15000 and dto.berat_gram == 350
    assert dto.preorder is True and dto.hari_proses == 7
    await services.rename_produk_keluarga(session, parent['id'], 'Kaos polos')
    assert (await services.get_produk(session, product.id)).nama_induk == 'Kaos polos'
    assert product.nama == 'Kaos - Merah M' and product.stok == 8
    assert [option.tier for option in dto.opsi_varian] == ['Warna', 'Ukuran']
    assert list((await session.execute(select(StokLedger))).scalars()) == ledger_before
    await services.update_produk(session, product.id, ProdukPatch(aktif=False))
    assert (await services.get_produk(session, product.id)).keluarga_id == parent['id']
    await services.update_produk(session, product.id, ProdukPatch(keluarga_id=None))
    assert product.keluarga_id is None and product.opsi_varian == [] and product.stok == 8
    assert (await session.execute(select(ProdukVarian))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_duplicate_combination_rejected_before_product_price_changes(session):
    parent = await family(session)
    for sku in ['A', 'B']:
        await services.create_produk(session, ProdukIn(sku_induk=sku, nama=sku, harga_dasar=Decimal('100'), stok=2))
    a, b = sorted(await services.list_produk(session), key=lambda row: row.sku_induk)
    await services.update_produk(session, a.id, ProdukPatch(keluarga_id=parent['id'], opsi_varian=options()))
    with pytest.raises(HTTPException) as exc:
        await services.update_produk(session, b.id, ProdukPatch(keluarga_id=parent['id'], opsi_varian=options('merah', 'm'), harga_dasar=Decimal('999')))
    assert exc.value.status_code == 409 and b.harga_dasar == 100 and b.keluarga_id is None
    with pytest.raises(HTTPException) as exc:
        await services.delete_produk_keluarga(session, parent['id'])
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_create_variant_and_parent_has_no_inventory_of_its_own(session):
    parent = await family(session)
    product = await services.create_produk(session, ProdukIn(sku_induk='A', nama='SKU A', harga_dasar=Decimal('100'), keluarga_id=parent['id'], opsi_varian=options()))
    assert product.nama_induk == 'Kaos'
    assert (await services.list_produk(session))[0].opsi_varian[0]['opsi'] == 'Merah'
    assert list((await session.execute(select(StokLedger))).scalars()) == []
    await services.delete_produk(session, product.id)
    await services.delete_produk_keluarga(session, parent['id'])
    assert await services.list_produk_keluarga(session) == []


@pytest.mark.asyncio
async def test_incomplete_variant_options_and_missing_parent_rejected(session):
    parent = await family(session)
    product = await services.create_produk(session, ProdukIn(sku_induk='A', nama='A', harga_dasar=Decimal('100')))
    for change in [ProdukPatch(keluarga_id=parent['id'], opsi_varian=options()[:1]), ProdukPatch(keluarga_id='missing', opsi_varian=options())]:
        with pytest.raises(HTTPException):
            await services.update_produk(session, product.id, change)
    assert product.keluarga_id is None


@pytest.mark.parametrize('tiers', [[], ['Warna', 'warna'], [' ']])
def test_invalid_parent_tiers_rejected(tiers):
    with pytest.raises(ValidationError):
        ProdukKeluargaIn(nama='Kaos', tiers=tiers)
