"""Shopee catalogue snapshot: per-shop rows (never merged across shops), filter by shop, kept off Produk/stock."""
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import KatalogShopee, Produk


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


def _item(item_id, nama, harga, **extra):
    return {
        "item_id": item_id,
        "item_name": nama,
        "item_status": "NORMAL",
        "weight": "0.35",
        "description": f"deskripsi {nama}",
        "image": {"image_url_list": [f"https://img.example/{item_id}-1", f"https://img.example/{item_id}-2"]},
        "dimension": {"package_length": 20, "package_width": 10, "package_height": 5},
        "price_info": [{"current_price": harga}],
        "stock_info_v2": {"summary_info": {"total_available_stock": 7}},
        **extra,
    }


async def _toko(session, nama):
    return await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko=nama))


def test_normalisasi_katalog_converts_weight_to_grams_and_keeps_photos():
    k = erp_shopee.normalisasi_katalog(_item(1, "Kursi", 150000))
    assert k["berat_gram"] == 350
    assert k["foto"] == ["https://img.example/1-1", "https://img.example/1-2"]
    assert (k["panjang_cm"], k["lebar_cm"], k["tinggi_cm"]) == (Decimal("20"), Decimal("10"), Decimal("5"))
    assert k["harga_min"] == k["harga_max"] == Decimal("150000")
    assert k["stok_shopee"] == 7 and k["varian"] == []


def test_normalisasi_katalog_reads_extended_description_and_variants():
    item = _item(2, "Meja", 0, has_model=True)
    item["description"] = ""
    item["description_info"] = {
        "extended_description": {
            "field_list": [{"field_type": "text", "text": "baris 1"}, {"field_type": "image"}, {"field_type": "text", "text": "baris 2"}]
        }
    }
    model_resp = {
        "tier_variation": [{"option_list": [{"option": "Merah"}, {"option": "Biru"}]}],
        "model": [
            {"model_id": 11, "tier_index": [0], "model_sku": "M-R", "price_info": [{"current_price": 100}],
             "stock_info_v2": {"summary_info": {"total_available_stock": 2}}},
            {"model_id": 12, "tier_index": [1], "model_sku": "M-B", "price_info": [{"current_price": 120}],
             "stock_info_v2": {"summary_info": {"total_available_stock": 3}}},
        ],
    }
    k = erp_shopee.normalisasi_katalog(item, model_resp)
    assert k["deskripsi"] == "baris 1\nbaris 2"
    assert [v["nama"] for v in k["varian"]] == ["Merah", "Biru"]
    assert (k["harga_min"], k["harga_max"], k["stok_shopee"]) == (Decimal("100"), Decimal("120"), 5)


@pytest.mark.asyncio
async def test_same_title_in_two_shops_stays_two_rows_filterable_per_shop(session):
    a, b = await _toko(session, "Toko A"), await _toko(session, "Toko B")
    await services.simpan_katalog_shopee(session, a, [erp_shopee.normalisasi_katalog(_item(1, "Kursi", 150000))])
    await services.simpan_katalog_shopee(session, b, [erp_shopee.normalisasi_katalog(_item(9, "Kursi", 120000))])

    semua = await services.list_katalog_shopee(session)
    assert semua["total"] == 2
    assert sorted((i["nama_toko"], i["harga_min"]) for i in semua["items"]) == [("Toko A", Decimal("150000")), ("Toko B", Decimal("120000"))]
    cuma_b = await services.list_katalog_shopee(session, akun_id=b.id)
    assert [i["nama_toko"] for i in cuma_b["items"]] == ["Toko B"]
    assert await services.jumlah_katalog_per_toko(session) == {a.id: 1, b.id: 1}
    # Reference data only: no master product, no stock.
    assert (await session.execute(select(func.count()).select_from(Produk))).scalar_one() == 0


@pytest.mark.asyncio
async def test_pulling_again_updates_in_place_and_drops_vanished_items(session):
    a = await _toko(session, "Toko A")
    await services.simpan_katalog_shopee(
        session, a, [erp_shopee.normalisasi_katalog(_item(1, "Kursi", 100)), erp_shopee.normalisasi_katalog(_item(2, "Meja", 200))]
    )
    out = await services.simpan_katalog_shopee(session, a, [erp_shopee.normalisasi_katalog(_item(1, "Kursi Baru", 110))])
    assert out == {"katalog_baru": 0, "katalog_diperbarui": 1, "katalog_dihapus": 1}
    rows = (await session.execute(select(KatalogShopee))).scalars().all()
    assert [(r.item_id, r.nama, r.harga_min) for r in rows] == [("1", "Kursi Baru", Decimal("110"))]


@pytest.mark.asyncio
async def test_an_item_already_sent_to_the_store_is_kept_when_it_leaves_shopee(session):
    a = await _toko(session, "Toko A")
    await services.simpan_katalog_shopee(session, a, [erp_shopee.normalisasi_katalog(_item(1, "Kursi", 100))])
    row = (await session.execute(select(KatalogShopee))).scalar_one()
    row.dikirim_toko_id = "toko-1"
    await session.flush()
    out = await services.simpan_katalog_shopee(session, a, [])
    assert out["katalog_dihapus"] == 0
    assert (await session.execute(select(func.count()).select_from(KatalogShopee))).scalar_one() == 1


@pytest.mark.asyncio
async def test_search_and_unsent_filter(session):
    a = await _toko(session, "Toko A")
    await services.simpan_katalog_shopee(
        session, a, [erp_shopee.normalisasi_katalog(_item(1, "Kursi Rotan", 1)), erp_shopee.normalisasi_katalog(_item(2, "Meja Kayu", 1))]
    )
    hasil = await services.list_katalog_shopee(session, q="rotan")
    assert [i["nama"] for i in hasil["items"]] == ["Kursi Rotan"]
    row = (await session.execute(select(KatalogShopee).where(KatalogShopee.item_id == "1"))).scalar_one()
    row.dikirim_toko_id = "x"
    await session.flush()
    belum = await services.list_katalog_shopee(session, belum_dikirim=True)
    assert [i["nama"] for i in belum["items"]] == ["Meja Kayu"]


@pytest.mark.asyncio
async def test_deleting_a_shop_removes_its_catalogue_only(session):
    a, b = await _toko(session, "Toko A"), await _toko(session, "Toko B")
    await services.simpan_katalog_shopee(session, a, [erp_shopee.normalisasi_katalog(_item(1, "Kursi", 1))])
    await services.simpan_katalog_shopee(session, b, [erp_shopee.normalisasi_katalog(_item(2, "Meja", 1))])
    await services.delete_akun_marketplace(session, a.id)
    assert [k.akun_id for k in (await session.execute(select(KatalogShopee))).scalars()] == [b.id]


@pytest_asyncio.fixture
async def store_session():
    from tenants.store.modules.store.infrastructure import models  # noqa: F401 -- registers the tables
    from tenants.store.modules.store.infrastructure.database import StoreBase

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(StoreBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_sending_to_the_store_makes_a_draft_with_zero_stock_photos_and_variants(session, store_session, monkeypatch):
    from sqlalchemy.orm import selectinload

    from tenants.marketplace_erp.adapters.api.v1 import marketplace_erp_router as r
    from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import KatalogKirimIn
    from tenants.store.modules.store.infrastructure.models import ProdukStore

    async def foto_palsu(url):
        return f"key/{url.rsplit('/', 1)[-1]}"

    monkeypatch.setattr(r, "import_foto_dari_url", foto_palsu)
    a = await _toko(session, "Toko A")
    item = _item(5, "Meja", 0, has_model=True)
    model_resp = {
        "tier_variation": [{"option_list": [{"option": "Merah"}, {"option": "Biru"}]}],
        "model": [
            {"model_id": 1, "tier_index": [0], "price_info": [{"current_price": 100}], "stock_info_v2": {"summary_info": {"total_available_stock": 9}}},
            {"model_id": 2, "tier_index": [1], "price_info": [{"current_price": 120}], "stock_info_v2": {"summary_info": {"total_available_stock": 9}}},
        ],
    }
    await services.simpan_katalog_shopee(session, a, [erp_shopee.normalisasi_katalog(item, model_resp)])
    kid = (await session.execute(select(KatalogShopee.id))).scalar_one()

    out = await r.kirim_katalog_ke_toko(KatalogKirimIn(ids=[kid]), session, store_session, None)
    assert out["hasil"][0]["hasil"] == "dibuat" and out["hasil"][0]["foto"] == 2

    p = (await store_session.execute(select(ProdukStore).options(selectinload(ProdukStore.varian), selectinload(ProdukStore.foto)))).scalar_one()
    assert (p.nama, p.stok, p.aktif, p.harga, p.sumber, p.platform_asal) == ("Meja", 0, False, Decimal("100"), "erp", "shopee")
    assert p.berat_gram == 350 and len(p.foto) == 2
    assert sorted((v.nama, v.stok, v.harga) for v in p.varian) == [("Biru", 0, Decimal("120")), ("Merah", 0, Decimal("100"))]

    # Editing the store copy, then sending again, must not overwrite it unless asked.
    p.nama = "Meja (diedit di admin)"
    await store_session.flush()
    lagi = await r.kirim_katalog_ke_toko(KatalogKirimIn(ids=[kid]), session, store_session, None)
    assert lagi["hasil"][0]["hasil"] == "dilewati"
    await store_session.refresh(p)
    assert p.nama == "Meja (diedit di admin)"
