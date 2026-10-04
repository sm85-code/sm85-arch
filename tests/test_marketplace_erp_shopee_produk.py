"""Shopee catalogue pull (link to Produk by SKU) and the guarded stock/price push."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1 import marketplace_erp_router as router
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


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(erp_shopee, "SHOPEE_LIVE_SYNC", True)
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_ID", "1")
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_KEY", "k")
    return SimpleNamespace(access_token="at", id_toko_eksternal="5")


# --- listing keys ----------------------------------------------------------------


def test_listing_key_roundtrip():
    assert erp_shopee.kunci_listing(9001) == "9001"
    assert erp_shopee.kunci_listing(9001, 777) == "9001:777"
    assert erp_shopee.kunci_listing(9001, 0) == "9001"
    assert erp_shopee.pecah_kunci_listing("9001") == (9001, 0)
    assert erp_shopee.pecah_kunci_listing("9001:777") == (9001, 777)
    with pytest.raises(ValueError):
        erp_shopee.pecah_kunci_listing("SKU-ABC")


# --- normalisation ---------------------------------------------------------------


def test_normalisasi_item_without_variants():
    (e,) = erp_shopee.normalisasi_item(
        {
            "item_id": 9001,
            "item_name": "Kaos Polos",
            "item_sku": " KAOS ",
            "item_status": "NORMAL",
            "has_model": False,
            "price_info": [{"original_price": 60000, "current_price": 50000}],
            "stock_info_v2": {"summary_info": {"total_available_stock": 7}},
        }
    )
    assert e == {
        "id_eksternal": "9001",
        "nama_produk": "Kaos Polos",
        "sku": "KAOS",
        "harga": Decimal("50000"),
        "stok": 7,
        "aktif": True,
    }


def test_normalisasi_item_expands_variants_with_names():
    rows = erp_shopee.normalisasi_item(
        {"item_id": 9001, "item_name": "Kaos", "item_status": "NORMAL", "has_model": True},
        {
            "tier_variation": [
                {"name": "Warna", "option_list": [{"option": "Merah"}, {"option": "Biru"}]},
                {"name": "Ukuran", "option_list": [{"option": "M"}, {"option": "L"}]},
            ],
            "model": [
                {
                    "model_id": 11,
                    "tier_index": [0, 1],
                    "model_sku": "KAOS-MRH-L",
                    "model_status": "MODEL_NORMAL",
                    "price_info": [{"current_price": 55000}],
                    "stock_info_v2": {"summary_info": {"total_available_stock": 3}},
                },
                {"model_id": 12, "tier_index": [1, 0], "model_sku": "", "model_status": "MODEL_UNAVAILABLE"},
            ],
        },
    )
    assert [r["id_eksternal"] for r in rows] == ["9001:11", "9001:12"]
    assert rows[0]["nama_produk"] == "Kaos - Merah / L" and rows[0]["sku"] == "KAOS-MRH-L"
    assert rows[0]["harga"] == Decimal("55000") and rows[0]["stok"] == 3 and rows[0]["aktif"] is True
    assert rows[1]["nama_produk"] == "Kaos - Biru / M" and rows[1]["aktif"] is False


@pytest.mark.asyncio
async def test_sync_produk_pages_and_batches(live, monkeypatch):
    calls = []

    async def fake_request(session, akun, path, *, params=None, **_):
        calls.append((path, dict(params)))
        if path == erp_shopee._PATH_ITEM_LIST:
            if params["offset"] == 0:
                return {"response": {"item": [{"item_id": i} for i in range(1, 61)], "has_next_page": True, "next_offset": 60}}
            return {"response": {"item": [{"item_id": 61}], "has_next_page": False}}
        if path == erp_shopee._PATH_ITEM_BASE:
            ids = [int(i) for i in params["item_id_list"].split(",")]
            return {"response": {"item_list": [{"item_id": i, "item_name": f"P{i}", "item_sku": f"S{i}", "has_model": i == 61} for i in ids]}}
        assert path == erp_shopee._PATH_MODEL_LIST and params["item_id"] == 61
        return {"response": {"tier_variation": [], "model": [{"model_id": 5, "model_sku": "S61-A", "tier_index": []}]}}

    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake_request)
    rows = await erp_shopee.sync_produk(None, live)

    assert len(rows) == 61 and rows[-1]["id_eksternal"] == "61:5"
    list_calls = [p for path, p in calls if path == erp_shopee._PATH_ITEM_LIST]
    assert [c["offset"] for c in list_calls] == [0, 60]
    assert list_calls[0]["item_status"] == ["NORMAL", "UNLIST", "BANNED", "REVIEWING"]
    assert [len(p["item_id_list"].split(",")) for path, p in calls if path == erp_shopee._PATH_ITEM_BASE] == [50, 11]


# --- import into ProdukListing ------------------------------------------------------


async def _akun_produk(session):
    akun = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="TES"))
    produk = await services.create_produk(
        session, ProdukIn(sku_induk="KAOS-1", nama="Kaos", harga_dasar=Decimal("40000"), stok=9)
    )
    return akun, produk


def _entry(eid="9001", sku="kaos-1", harga="50000", aktif=True):
    return {"id_eksternal": eid, "nama_produk": "Kaos", "sku": sku, "harga": Decimal(harga), "stok": 1, "aktif": aktif}


@pytest.mark.asyncio
async def test_import_links_by_sku_and_reports_the_rest(session):
    akun, produk = await _akun_produk(session)
    hasil = await services.impor_listing_marketplace(
        session, akun, [_entry(), _entry("9002", sku="TIDAK-ADA"), _entry("9003", sku="")]
    )
    assert (hasil["listing_baru"], hasil["tanpa_sku_cocok"]) == (1, 2)
    assert [c["id_eksternal"] for c in hasil["contoh_tanpa_sku"]] == ["9002", "9003"]
    (listing,) = await services.list_listing(session)
    assert listing.produk_id == produk.id and listing.akun_id == akun.id
    assert listing.harga_jual == Decimal("50000")  # keeps Shopee's price so the first push is a no-op
    assert (await services.get_produk(session, produk.id)).stok == 9  # pull never touches stock


@pytest.mark.asyncio
async def test_import_never_overwrites_owner_overrides(session):
    akun, produk = await _akun_produk(session)
    await services.create_listing(
        session,
        ProdukListingIn(
            produk_id=produk.id, akun_id=akun.id, platform="shopee", id_eksternal="9001",
            harga_jual=Decimal("45000"), stok_listing=2,
        ),
    )
    hasil = await services.impor_listing_marketplace(session, akun, [_entry(harga="99999")])
    assert hasil["sudah_ada"] == 1 and hasil["listing_baru"] == 0
    (listing,) = await services.list_listing(session)
    assert listing.harga_jual == Decimal("45000") and listing.stok_listing == 2


# --- push --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_baris_push_uses_overrides_else_produk_values(session):
    akun, produk = await _akun_produk(session)
    await services.create_listing(
        session, ProdukListingIn(produk_id=produk.id, akun_id=akun.id, platform="shopee", id_eksternal="9001")
    )
    await services.create_listing(
        session,
        ProdukListingIn(
            produk_id=produk.id, akun_id=akun.id, platform="shopee", id_eksternal="9001:5",
            harga_jual=Decimal("45000"), stok_listing=0,
        ),
    )
    rows = await services.baris_push_listing(session, akun)
    by_id = {r["id_eksternal"]: r for r in rows}
    assert (by_id["9001"]["stok"], by_id["9001"]["harga"]) == (9, Decimal("40000"))
    assert (by_id["9001:5"]["stok"], by_id["9001:5"]["harga"]) == (0, Decimal("45000"))  # override of 0 counts


@pytest.mark.asyncio
async def test_push_endpoint_is_dry_run_by_default(session, monkeypatch):
    akun, produk = await _akun_produk(session)
    await services.create_listing(
        session, ProdukListingIn(produk_id=produk.id, akun_id=akun.id, platform="shopee", id_eksternal="9001")
    )

    async def must_not_call(*a, **k):
        raise AssertionError("dry run must not call Shopee")

    monkeypatch.setattr(erp_shopee, "kirim_stok_harga", must_not_call)
    out = await router.push_stok_harga_akun(akun.id, session=session, _=None)
    assert out["dry_run"] is True and out["jumlah"] == 1 and out["rows"][0]["stok"] == 9


@pytest.mark.asyncio
async def test_push_endpoint_sends_only_when_explicit(session, monkeypatch):
    akun, produk = await _akun_produk(session)
    await services.create_listing(
        session, ProdukListingIn(produk_id=produk.id, akun_id=akun.id, platform="shopee", id_eksternal="9001")
    )
    sent = {}

    async def fake_send(s, a, rows):
        sent["rows"] = rows
        return {"stok_ok": 1, "harga_ok": 1, "gagal": []}

    monkeypatch.setattr(erp_shopee, "kirim_stok_harga", fake_send)
    out = await router.push_stok_harga_akun(akun.id, dry_run=False, session=session, _=None)
    assert out["dry_run"] is False and out["stok_ok"] == 1 and len(sent["rows"]) == 1


@pytest.mark.asyncio
async def test_kirim_stok_harga_groups_by_item_and_reports_failures(live, monkeypatch):
    calls = []

    async def fake_request(session, akun, path, *, method="GET", body=None, **_):
        calls.append((path, body))
        if path == erp_shopee._PATH_UPDATE_STOCK:
            return {"response": {"success_list": [{"model_id": 11}], "failure_list": [{"model_id": 12, "failed_reason": "terlalu besar"}]}}
        raise HTTPException(status_code=502, detail="Shopee gagal")  # price call fails for the whole item

    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake_request)
    rows = [
        {"id_eksternal": "9001:11", "stok": 3, "harga": Decimal("50000")},
        {"id_eksternal": "9001:12", "stok": -4, "harga": Decimal("51000.5")},
        {"id_eksternal": "bukan-shopee", "stok": 1, "harga": Decimal("1")},
    ]
    out = await erp_shopee.kirim_stok_harga(None, live, rows)

    assert [p for p, _ in calls] == [erp_shopee._PATH_UPDATE_STOCK, erp_shopee._PATH_UPDATE_PRICE]
    stock_body = calls[0][1]
    assert stock_body["item_id"] == 9001
    assert stock_body["stock_list"] == [
        {"model_id": 11, "seller_stock": [{"stock": 3}]},
        {"model_id": 12, "seller_stock": [{"stock": 0}]},  # negative stock never sent
    ]
    assert calls[1][1]["price_list"][1] == {"model_id": 12, "original_price": 51000.5}
    assert out["stok_ok"] == 1 and out["harga_ok"] == 0
    alasan = {(g["id_eksternal"], g["alasan"].split(":")[0]) for g in out["gagal"]}
    assert ("9001:12", "stok") in alasan and ("9001:11", "harga") in alasan and ("bukan-shopee", "id_eksternal bukan format Shopee") in alasan
