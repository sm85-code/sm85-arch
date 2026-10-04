"""Settlement pulled from Shopee: get_escrow_list (what was released) + get_escrow_detail (the money per order)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase

# Shape taken from open.shopee.com (v2.payment.get_escrow_list / get_escrow_detail).
DETAIL = {
    "order_sn": "X1",
    "order_income": {
        "escrow_amount": 80000.5,
        "escrow_amount_after_adjustment": 79000.5,
        "order_original_price": 100000,
        "voucher_from_seller": 5000,
        "commission_fee": 8000,
        "service_fee": 3000,
        "seller_transaction_fee": 1500,
        "final_shipping_fee": -2500,
        "shopee_shipping_rebate": 2500,
        "total_adjustment_amount": -1000,
        "items": [{"item_id": 1}],
        "order_adjustment": [{"amount": -1000}],
    },
}


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _toko(session, nama):
    return await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko=nama))


def test_normalisasi_escrow_maps_the_money_fields():
    rilis = datetime(2026, 9, 30, tzinfo=timezone.utc)
    row = erp_shopee.normalisasi_escrow("X1", rilis, 79000.5, DETAIL)
    assert row["order_sn"] == "X1" and row["dirilis_at"] == rilis
    assert row["jumlah_cair"] == Decimal("79000.5") and row["escrow"] == Decimal("79000.5")  # after adjustment wins
    assert (row["penjualan"], row["voucher_penjual"], row["komisi"], row["layanan"], row["transaksi"]) == (
        Decimal(100000), Decimal(5000), Decimal(8000), Decimal(3000), Decimal("1500"),
    )
    assert (row["ongkir"], row["subsidi_ongkir"], row["penyesuaian"]) == (Decimal(-2500), Decimal(2500), Decimal(-1000))
    assert '"items"' not in row["rincian"] and "order_adjustment" not in row["rincian"] and "commission_fee" in row["rincian"]


def test_normalisasi_escrow_survives_missing_detail_and_bad_numbers():
    row = erp_shopee.normalisasi_escrow("X2", None, 5000, None)
    assert row["jumlah_cair"] == Decimal(5000) and row["komisi"] == Decimal(0)
    assert erp_shopee._uang("abc") == Decimal(0) and erp_shopee._uang(None) == Decimal(0)


def _fake_shopee(monkeypatch, *, daftar, detail_dipanggil):
    monkeypatch.setattr(erp_shopee, "SHOPEE_LIVE_SYNC", True)
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_ID", "1")
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_KEY", "k")
    daftar_dipanggil = []

    async def fake(session, akun, path, *, params=None, **_):
        if path == erp_shopee._PATH_ESCROW_LIST:
            daftar_dipanggil.append(dict(params))
            if params["page_no"] == 1:
                return {"response": {"more": True, "escrow_list": daftar[:2]}}
            return {"response": {"more": False, "escrow_list": daftar[2:]}}
        detail_dipanggil.append(params["order_sn"])
        return {"response": {**DETAIL, "order_sn": params["order_sn"]}}

    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    return daftar_dipanggil


def _e(sn, jumlah=1000):
    return {"order_sn": sn, "payout_amount": jumlah, "escrow_release_time": 1_790_000_000}


@pytest.mark.asyncio
async def test_sync_settlement_pages_the_list_skips_stored_orders_and_reports_the_rest(monkeypatch):
    detail = []
    daftar = _fake_shopee(monkeypatch, daftar=[_e("A"), _e("B"), _e("C"), _e("D")], detail_dipanggil=detail)
    akun = SimpleNamespace(access_token="at", id_toko_eksternal="5")
    sekarang = 1_790_100_000
    out = await erp_shopee.sync_settlement(None, akun, sekarang - 3 * 86400, sekarang, sudah={"B"}, maks_detail=2)

    assert out["ditemukan"] == 4 and sorted(detail) == ["A", "C"] and out["sisa"] == 1  # D waits for the next pull
    assert sorted(r["order_sn"] for r in out["rows"]) == ["A", "C"]
    assert [p["page_no"] for p in daftar] == [1, 2] and daftar[0]["page_size"] == 100
    assert all(p["release_time_to"] - p["release_time_from"] <= erp_shopee.ESCROW_WINDOW_SECONDS for p in daftar)


@pytest.mark.asyncio
async def test_sync_settlement_splits_a_long_period_into_windows(monkeypatch):
    daftar = _fake_shopee(monkeypatch, daftar=[], detail_dipanggil=[])
    akun = SimpleNamespace(access_token="at", id_toko_eksternal="5")
    sekarang = 1_790_100_000
    await erp_shopee.sync_settlement(None, akun, sekarang - 40 * 86400, sekarang)
    froms = sorted({p["release_time_from"] for p in daftar})
    assert len(froms) == 3 and froms[0] == sekarang - 40 * 86400  # 40 days -> three windows of at most 15 days


@pytest.mark.asyncio
async def test_saved_rows_are_listed_summed_per_shop_sorted_and_not_duplicated(session):
    a, b = await _toko(session, "Toko A"), await _toko(session, "Toko B")
    kini = datetime.now(timezone.utc)

    def baris(sn, cair, hari, komisi=100):
        return {**erp_shopee.normalisasi_escrow(sn, kini - timedelta(days=hari), cair, DETAIL), "komisi": Decimal(komisi)}

    assert await services.simpan_settlement_pesanan(session, a, [baris("A1", 500, 1), baris("A2", 700, 2)]) == {"baru": 2, "diperbarui": 0}
    await services.simpan_settlement_pesanan(session, b, [baris("B1", 900, 10)])
    assert await services.simpan_settlement_pesanan(session, a, [baris("A1", 550, 1)]) == {"baru": 0, "diperbarui": 1}

    semua = await services.list_settlement_pesanan(session)
    assert semua["total"] == 3 and [i["order_sn"] for i in semua["items"]] == ["A1", "A2", "B1"]  # newest release first
    assert next(i for i in semua["items"] if i["order_sn"] == "A1")["jumlah_cair"] == Decimal(550)
    cair = await services.list_settlement_pesanan(session, urut="cair:desc")
    assert [i["order_sn"] for i in cair["items"]] == ["B1", "A2", "A1"]
    assert (await services.list_settlement_pesanan(session, akun_id=b.id))["total"] == 1
    assert (await services.list_settlement_pesanan(session, dari=kini - timedelta(days=5)))["total"] == 2
    assert (await services.list_settlement_pesanan(session, q="b1"))["total"] == 1
    for kunci in services.KUNCI_URUT_SETTLEMENT:  # every column sorts without an error
        await services.list_settlement_pesanan(session, urut=f"{kunci}:asc")

    ring = await services.ringkasan_settlement_pesanan(session)
    per_toko = {t["nama_toko"]: t for t in ring["toko"]}
    assert per_toko["Toko A"]["pesanan"] == 2 and per_toko["Toko A"]["jumlah_cair"] == Decimal(1250)
    assert ring["total"]["pesanan"] == 3 and ring["total"]["jumlah_cair"] == Decimal(2150) and ring["total"]["komisi"] == Decimal(300)


@pytest.mark.asyncio
async def test_sinkron_settlement_akun_stores_new_orders_only_and_validates_days(session, monkeypatch):
    akun = await _toko(session, "Toko A")
    panggilan = []

    async def fake(session, akun, dari, sampai, sudah=frozenset(), **_):
        panggilan.append(set(sudah))
        rows = [erp_shopee.normalisasi_escrow(sn, None, 100, DETAIL) for sn in ("N1", "N2") if sn not in sudah]
        return {"rows": rows, "ditemukan": 2, "sisa": 0}

    monkeypatch.setattr(erp_shopee, "sync_settlement", fake)
    assert (await services.sinkron_settlement_akun(session, akun, 15))["baru"] == 2
    assert (await services.sinkron_settlement_akun(session, akun, 15))["baru"] == 0 and panggilan[1] == {"N1", "N2"}
    from fastapi import HTTPException

    for hari in (0, 91):
        with pytest.raises(HTTPException) as exc:
            await services.sinkron_settlement_akun(session, akun, hari)
        assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_deleting_a_shop_removes_its_settlement_rows(session):
    a = await _toko(session, "Toko A")
    await services.simpan_settlement_pesanan(session, a, [erp_shopee.normalisasi_escrow("Z1", None, 1, DETAIL)])
    await services.delete_akun_marketplace(session, a.id)
    assert (await services.list_settlement_pesanan(session))["total"] == 0
