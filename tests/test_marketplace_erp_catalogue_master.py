import json
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application.catalogue_master import copy_batch
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    AkunMarketplace,
    KatalogShopee,
    Produk,
    ProdukKeluarga,
    ProdukListing,
    StokLedger,
)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add(AkunMarketplace(id="shop", platform="shopee", nama_toko="Toko"))
        await session.flush()
        yield session
    await engine.dispose()


async def source(session, variants, **fields):
    row = KatalogShopee(
        id="catalogue",
        akun_id="shop",
        item_id="123",
        nama="Produk Induk",
        sku="",
        foto_json='["https://example.com/item.jpg"]',
        varian_json=json.dumps(variants),
        detail_json=json.dumps({"has_model": bool(variants), "is_pre_order": False}),
        harga_min=Decimal("100"),
        berat_gram=500,
        **fields,
    )
    session.add(row)
    await session.flush()
    return row


@pytest.mark.asyncio
async def test_copy_preserves_variants_without_importing_marketplace_stock_and_retry_is_idempotent(session):
    await source(
        session,
        [
            {
                "model_id": "1",
                "sku": "RED",
                "opsi": [{"tier": "Warna", "opsi": "Merah"}],
                "harga": "90",
                "harga_asli": "100",
                "stok": 80,
            },
            {
                "model_id": "2",
                "sku": "",
                "opsi": [{"tier": "Warna", "opsi": "Biru"}],
                "harga": "120",
                "stok": 90,
                "berat_gram": 750,
                "preorder": True,
                "hari_kirim": 7,
            },
        ],
    )
    result = await copy_batch(session, ["catalogue"])
    assert result["hasil"][0]["ok"] and result["hasil"][0]["sku_dibuat"] == 1
    rows = (await session.scalars(select(Produk))).all()
    assert len(rows) == 2 and all(r.stok == 0 and r.nama == "Produk Induk" for r in rows)
    red = next(r for r in rows if r.sku_induk == "RED")
    blue = next(r for r in rows if r.sku_induk != "RED")
    assert red.stok_referensi == 80 and blue.stok_referensi == 90
    assert red.harga_dasar == 100 and red.berat_gram == 500
    assert blue.berat_gram == 750 and blue.preorder and blue.hari_proses == 7
    retry = await copy_batch(session, ["catalogue"])
    assert all(not r["baru"] for r in retry["hasil"][0]["produk"])
    assert await session.scalar(select(func.count()).select_from(ProdukKeluarga)) == 1
    assert await session.scalar(select(func.count()).select_from(ProdukListing)) == 0
    assert await session.scalar(select(func.count()).select_from(StokLedger)) == 0


@pytest.mark.asyncio
async def test_reuses_existing_sku_without_overwriting_stock_or_details(session):
    session.add(Produk(sku_induk="EXIST", nama="Master Pilihan Saya", stok=23, harga_dasar=250))
    row = await source(session, [])
    row.sku = "EXIST"
    result = await copy_batch(session, ["catalogue"])
    assert result["hasil"][0]["produk"][0]["baru"] is False
    existing = await session.scalar(select(Produk).where(Produk.sku_induk == "EXIST"))
    assert existing.stok == 23 and existing.nama == "Master Pilihan Saya" and existing.harga_dasar == 250


@pytest.mark.asyncio
async def test_failure_rolls_back_all_variants_of_one_item(session):
    await source(
        session,
        [
            {"model_id": "1", "sku": "FIRST", "opsi": [{"tier": "Warna", "opsi": "Merah"}], "harga": "100"},
            {
                "model_id": "2",
                "sku": "SECOND",
                "opsi": [{"tier": "Warna", "opsi": "Biru"}],
                "harga": "100",
                "preorder": True,
                "hari_kirim": 99,
            },
        ],
    )
    result = await copy_batch(session, ["catalogue"])
    assert result["hasil"][0]["ok"] is False
    assert await session.scalar(select(func.count()).select_from(Produk)) == 0
    assert await session.scalar(select(func.count()).select_from(ProdukKeluarga)) == 0
