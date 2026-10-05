"""Shopee order pull: payload normalisation, paging, and idempotent import into the OMS."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import (
    AkunMarketplaceIn,
    ProdukIn,
    ProdukListingIn,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _setup(session, stok=10):
    akun = await services.create_akun_marketplace(
        session, AkunMarketplaceIn(platform="shopee", nama_toko="TES Sandbox")
    )
    produk = await services.create_produk(
        session, ProdukIn(sku_induk="SKU-1", nama="Kaos", harga_dasar=Decimal("50000"), stok=stok)
    )
    await services.create_listing(
        session,
        ProdukListingIn(produk_id=produk.id, akun_id=akun.id, platform="shopee", id_eksternal="9001"),
    )
    return akun, produk


def _row(sn="SN1", status="to_ship", item_id="9001", qty=2, **kw):
    return {
        "id_eksternal": sn,
        "status": status,
        "status_mentah": (status or "NEW_UNKNOWN").upper(),
        "nama_pembeli": "budi",
        "total": Decimal("100000"),
        "items": [
            {
                "nama_produk": "Kaos - M",
                "harga_satuan": Decimal("50000"),
                "qty": qty,
                "id_eksternal_kandidat": [item_id, "777"],
            }
        ],
        **kw,
    }


# --- normalisation ------------------------------------------------------------


def test_normalisasi_pesanan_maps_fields():
    out = erp_shopee.normalisasi_pesanan(
        {
            "order_sn": "2410ABC",
            "order_status": "READY_TO_SHIP",
            "buyer_username": "budi",
            "total_amount": 101500.5,
            "item_list": [
                {
                    "item_id": 9001,
                    "item_name": "Kaos",
                    "model_id": 777,
                    "model_name": "M",
                    "model_quantity_purchased": 2,
                    "model_original_price": 60000,
                    "model_discounted_price": 50000,
                },
                {"item_id": 1, "item_name": "Tanpa qty", "model_quantity_purchased": 0},
            ],
        }
    )
    assert out["id_eksternal"] == "2410ABC" and out["status"] == "to_ship"
    assert out["nama_pembeli"] == "budi" and out["total"] == Decimal("101500.5")
    assert len(out["items"]) == 1  # zero-qty line dropped
    item = out["items"][0]
    assert item["nama_produk"] == "Kaos" and item["model_name"] == "M" and item["qty"] == 2
    assert item["harga_satuan"] == Decimal("50000")  # discounted price wins
    assert item["id_eksternal_kandidat"] == ["9001:777", "9001"]  # variant listing key first, then the item


def test_normalisasi_pesanan_unknown_status_is_none():
    assert erp_shopee.normalisasi_pesanan({"order_sn": "X", "order_status": "SOMETHING_NEW"})["status"] is None


# --- paging -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_pesanan_pages_list_then_batches_detail(monkeypatch):
    monkeypatch.setattr(erp_shopee, "SHOPEE_LIVE_SYNC", True)
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_ID", "1")
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_KEY", "k")
    akun = SimpleNamespace(access_token="at", id_toko_eksternal="5")
    calls = []

    async def fake_request(session, akun, path, *, params=None, **_):
        calls.append((path, dict(params)))
        if path == erp_shopee._PATH_ORDER_LIST:
            if "cursor" not in params:
                return {"response": {"more": True, "next_cursor": "c2", "order_list": [{"order_sn": f"A{i}"} for i in range(60)]}}
            return {"response": {"more": False, "order_list": [{"order_sn": "B1"}]}}
        if path == erp_shopee._PATH_TRACKING:
            return {"response": {"tracking_number": f"RESI-{params['order_sn']}"}}
        sns = params["order_sn_list"].split(",")
        return {"response": {"order_list": [{"order_sn": sn, "order_status": "COMPLETED"} for sn in sns]}}

    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake_request)
    rows = await erp_shopee.sync_pesanan(None, akun, lewati_resi={"A0"})

    assert len(rows) == 61 and {r["status"] for r in rows} == {"completed"}
    # tracking numbers are fetched for processed orders, except those already stored
    assert "nomor_resi" not in next(r for r in rows if r["id_eksternal"] == "A0")
    assert next(r for r in rows if r["id_eksternal"] == "B1")["nomor_resi"] == "RESI-B1"
    list_calls = [p for path, p in calls if path == erp_shopee._PATH_ORDER_LIST]
    detail_calls = [p for path, p in calls if path == erp_shopee._PATH_ORDER_DETAIL]
    assert [c.get("cursor") for c in list_calls] == [None, "c2"]
    assert all(c["time_to"] - c["time_from"] < 15 * 24 * 3600 for c in list_calls)
    assert [len(c["order_sn_list"].split(",")) for c in detail_calls] == [50, 11]
    assert "item_list" in detail_calls[0]["response_optional_fields"]


# --- import into the OMS ------------------------------------------------------


def test_jalur_status():
    assert services._jalur_status("unpaid", "completed") == ["to_ship", "shipped", "completed"]
    assert services._jalur_status("to_ship", "cancelled") == ["cancelled"]
    assert services._jalur_status("shipped", "to_ship") == []  # never backwards
    assert services._jalur_status("unpaid", "unpaid") == []


@pytest.mark.asyncio
async def test_import_new_order_reserves_stock_and_does_not_push_back(session):
    akun, produk = await _setup(session, stok=10)
    hasil = await services.impor_pesanan_marketplace(session, akun, [_row()])
    assert hasil == {"baru": 1, "diperbarui": 0, "tidak_berubah": 0, "dilewati": 0}

    (pesanan,) = await services.list_pesanan(session, platform="shopee")
    assert pesanan.status == "to_ship" and pesanan.akun_id == akun.id and pesanan.nama_pembeli == "budi"
    assert pesanan.items[0].produk_id == produk.id  # mapped through the listing
    assert pesanan.tersinkron_marketplace is True and "mengikuti" in pesanan.catatan_sinkron
    assert (await services.get_produk(session, produk.id)).stok == 8


@pytest.mark.asyncio
async def test_reimport_is_idempotent(session):
    akun, produk = await _setup(session, stok=10)
    await services.impor_pesanan_marketplace(session, akun, [_row()])
    hasil = await services.impor_pesanan_marketplace(session, akun, [_row()])
    assert hasil["baru"] == 0 and hasil["tidak_berubah"] == 1
    assert len(await services.list_pesanan(session, platform="shopee")) == 1
    assert (await services.get_produk(session, produk.id)).stok == 8  # reserved once, not twice


@pytest.mark.asyncio
async def test_status_moves_forward_on_later_pull(session):
    akun, produk = await _setup(session, stok=10)
    await services.impor_pesanan_marketplace(session, akun, [_row(status="to_ship")])
    hasil = await services.impor_pesanan_marketplace(session, akun, [_row(status="completed")])
    assert hasil["diperbarui"] == 1
    (pesanan,) = await services.list_pesanan(session, platform="shopee")
    assert pesanan.status == "completed"
    assert (await services.get_produk(session, produk.id)).stok == 8  # ship consumes, never restocks


@pytest.mark.asyncio
async def test_cancel_on_marketplace_releases_stock(session):
    akun, produk = await _setup(session, stok=10)
    await services.impor_pesanan_marketplace(session, akun, [_row(status="to_ship")])
    await services.impor_pesanan_marketplace(session, akun, [_row(status="cancelled")])
    (pesanan,) = await services.list_pesanan(session, platform="shopee")
    assert pesanan.status == "cancelled"
    assert (await services.get_produk(session, produk.id)).stok == 10


@pytest.mark.asyncio
async def test_never_moves_local_order_backwards(session):
    akun, _ = await _setup(session)
    await services.impor_pesanan_marketplace(session, akun, [_row(status="shipped")])
    await services.impor_pesanan_marketplace(session, akun, [_row(status="to_ship")])
    (pesanan,) = await services.list_pesanan(session, platform="shopee")
    assert pesanan.status == "shipped"


@pytest.mark.asyncio
async def test_oversell_keeps_order_unpaid_with_note_and_leaves_stock(session):
    akun, produk = await _setup(session, stok=1)
    hasil = await services.impor_pesanan_marketplace(session, akun, [_row(qty=5)])
    assert hasil["baru"] == 1  # the order is still imported
    (pesanan,) = await services.list_pesanan(session, platform="shopee")
    assert pesanan.status == "unpaid" and "belum bisa diterapkan" in pesanan.catatan_sinkron
    assert (await services.get_produk(session, produk.id)).stok == 1


@pytest.mark.asyncio
async def test_unmapped_item_and_unknown_status(session):
    akun, _ = await _setup(session)
    hasil = await services.impor_pesanan_marketplace(
        session, akun, [_row(sn="SN2", item_id="404"), _row(sn="SN3", status=None)]
    )
    assert hasil["baru"] == 1 and hasil["dilewati"] == 1
    (pesanan,) = await services.list_pesanan(session, platform="shopee")
    assert pesanan.items[0].produk_id is None and pesanan.status == "to_ship"


@pytest.mark.asyncio
async def test_sync_pesanan_fetches_detail_of_orders_to_complete(monkeypatch):
    """Orders stored without their real order time (outside the 15-day window) are fetched by number."""
    monkeypatch.setattr(erp_shopee, "SHOPEE_LIVE_SYNC", True)
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_ID", "1")
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_KEY", "k")
    akun = SimpleNamespace(access_token="at", id_toko_eksternal="5")

    async def fake_request(session, akun, path, *, params=None, **_):
        if path == erp_shopee._PATH_ORDER_LIST:
            return {"response": {"more": False, "order_list": [{"order_sn": "NEW1"}]}}
        sns = params["order_sn_list"].split(",")
        return {"response": {"order_list": [{"order_sn": sn, "order_status": "CANCELLED", "create_time": 1_753_400_000} for sn in sns]}}

    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake_request)
    rows = await erp_shopee.sync_pesanan(None, akun, lengkapi=["OLD1", "NEW1"])

    assert sorted(r["id_eksternal"] for r in rows) == ["NEW1", "OLD1"]  # no duplicate for NEW1
    assert next(r for r in rows if r["id_eksternal"] == "OLD1")["dipesan_at"].year == 2025
