"""Product photo on order lines: stored from Shopee's image_info, or borrowed from the catalogue for older lines."""
import json
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn, PesananOut
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import KatalogShopee


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _toko(session, nama="A"):
    return await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko=nama))


def _order(sn, items):
    return {"order_sn": sn, "order_status": "READY_TO_SHIP", "buyer_username": "b", "total_amount": 100000, "create_time": 1_790_000_000, "item_list": items}


def _item(item_id=111, model_id=7, nama="Partisi Ruangan Minimalis", model="Merah", foto="https://img/a.jpg"):
    d = {"item_id": item_id, "item_name": nama, "model_id": model_id, "model_name": model, "model_quantity_purchased": 1, "model_original_price": 100000}
    if foto:
        d["image_info"] = {"image_url": foto}
    return d


def test_the_order_normaliser_reads_photo_and_shopee_ids():
    b = erp_shopee.normalisasi_pesanan(_order("S1", [_item(), _item(foto=None, model_id=None)]))["items"]
    assert (b[0]["foto"], b[0]["item_id"], b[0]["model_id"]) == ("https://img/a.jpg", "111", "7")
    assert (b[1]["foto"], b[1]["model_id"]) == (None, None)


@pytest.mark.asyncio
async def test_import_stores_the_photo_and_a_later_sync_fills_it_in_without_losing_it(session):
    akun = await _toko(session)
    await services.impor_pesanan_marketplace(session, akun, [erp_shopee.normalisasi_pesanan(_order("S1", [_item(foto=None)]))])
    p = (await services.list_pesanan(session, platform="shopee"))[0]
    assert p.items[0].foto_url is None and p.items[0].item_id_eksternal == "111"
    await services.impor_pesanan_marketplace(session, akun, [erp_shopee.normalisasi_pesanan(_order("S1", [_item()]))])
    p = (await services.list_pesanan(session, platform="shopee"))[0]
    assert p.items[0].foto_url == "https://img/a.jpg"
    await services.impor_pesanan_marketplace(session, akun, [erp_shopee.normalisasi_pesanan(_order("S1", [_item(foto=None)]))])
    p = (await services.list_pesanan(session, platform="shopee"))[0]
    assert p.items[0].foto_url == "https://img/a.jpg"  # an update without a photo does not erase it


@pytest.mark.asyncio
async def test_older_lines_borrow_the_catalogue_photo_of_their_own_shop_only(session):
    a, b = await _toko(session, "A"), await _toko(session, "B")
    varian = [{"nama": "Merah", "foto": "https://kat/merah.jpg", "model_id": "7"}, {"nama": "Biru", "foto": "https://kat/biru.jpg", "model_id": "8"}]
    session.add(KatalogShopee(akun_id=a.id, item_id="111", nama="Partisi Ruangan Minimalis", foto_json=json.dumps(["https://kat/utama.jpg"]), varian_json=json.dumps(varian), harga_min=Decimal("1")))
    session.add(KatalogShopee(akun_id=a.id, item_id="222", nama="Meja Lipat", foto_json=json.dumps(["https://kat/meja.jpg"])))
    session.add(KatalogShopee(akun_id=b.id, item_id="111", nama="Partisi Ruangan Minimalis", foto_json=json.dumps(["https://kat/toko-b.jpg"])))
    await session.flush()
    for akun, sn, items in (
        (a, "S1", [_item(model="Biru", foto=None), _item(item_id=None, model_id=None, nama="  meja lipat ", model="", foto=None), _item(item_id=999, nama="Tidak Ada", foto=None)]),
        (b, "S2", [_item(foto=None)]),
    ):
        await services.impor_pesanan_marketplace(session, akun, [erp_shopee.normalisasi_pesanan(_order(sn, items))])
    daftar = await services.list_pesanan(session, platform="shopee")
    await services.lengkapi_foto_item(session, daftar)
    per = {p.id_eksternal: [i.foto for i in p.items] for p in daftar}
    assert per["S1"] == ["https://kat/biru.jpg", "https://kat/meja.jpg", None]  # by item id (+variant photo), by name, no match
    assert per["S2"] == ["https://kat/toko-b.jpg"]  # shop B's own catalogue, not shop A's


@pytest.mark.asyncio
async def test_a_stored_photo_wins_and_the_api_shape_carries_it(session):
    akun = await _toko(session)
    session.add(KatalogShopee(akun_id=akun.id, item_id="111", nama="Partisi Ruangan Minimalis", foto_json=json.dumps(["https://kat/utama.jpg"])))
    await session.flush()
    await services.impor_pesanan_marketplace(session, akun, [erp_shopee.normalisasi_pesanan(_order("S1", [_item()]))])
    daftar = await services.list_pesanan(session, platform="shopee")
    await services.lengkapi_foto_item(session, daftar)
    out = PesananOut.model_validate(daftar[0]).model_dump()
    assert out["items"][0]["foto"] == "https://img/a.jpg" and out["items"][0]["item_id_eksternal"] == "111"


@pytest.mark.asyncio
async def test_recipient_contact_is_carried_from_shopee_to_order_response(session):
    akun = await _toko(session)
    order = _order("RECIPIENT1", [_item()])
    order["recipient_address"] = {
        "name": "Penerima Uji", "phone": "081234567890",
        "full_address": "Jl. Uji 10, Bandung", "district": "Coblong",
        "city": "Bandung", "state": "Jawa Barat", "zipcode": "40132",
    }
    await services.impor_pesanan_marketplace(session, akun, [erp_shopee.normalisasi_pesanan(order)])
    rows = await services.list_pesanan(session, platform="shopee")
    out = PesananOut.model_validate(rows[0]).model_dump()
    assert out["penerima"] == "Penerima Uji"
    assert out["telepon_penerima"] == "081234567890"
    assert out["alamat_penerima"] == "Jl. Uji 10, Bandung, Coblong, Jawa Barat, 40132"
    assert erp_shopee.normalisasi_pesanan(_order("EMPTY", [_item()]))["detail"]["telepon_penerima"] == ""
