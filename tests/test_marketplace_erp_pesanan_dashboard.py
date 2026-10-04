"""Pesanan list filters (shop, stage, search, date, sort, paging), filter-chip counts and the dashboard tables."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase

SEKARANG = datetime(2026, 10, 4, 5, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _toko(session, nama):
    akun = await services.create_akun_marketplace(
        session, AkunMarketplaceIn(platform="shopee", nama_toko=nama, id_toko_eksternal="shop-" + nama.replace(" ", "-").lower())
    )
    return akun


def _order(sn, status, mp, *, hari=0, total=100, pembeli="b***", produk=("Kursi", 1, 100), resi=None):
    nama, qty, harga = produk
    return {
        "id_eksternal": sn, "status": status, "status_mentah": mp, "nama_pembeli": pembeli, "total": Decimal(total),
        "kurir": "SPX", "nomor_resi": resi, "dipesan_at": SEKARANG - timedelta(days=hari),
        "items": [{"nama_produk": nama, "harga_satuan": Decimal(harga), "qty": qty, "id_eksternal_kandidat": []}],
    }


@pytest_asyncio.fixture
async def data(session):
    a, b = await _toko(session, "Toko A"), await _toko(session, "Toko B")
    await services.impor_pesanan_marketplace(session, a, [
        _order("A1", "unpaid", "UNPAID", hari=1, total=50),
        _order("A2", "to_ship", "READY_TO_SHIP", hari=2, total=200, pembeli="Budi"),
        _order("A3", "to_ship", "PROCESSED", hari=3, total=300, resi="SPX123", produk=("Meja Kayu", 2, 150)),
        _order("A4", "shipped", "SHIPPED", hari=10, total=400),
        _order("A5", "completed", "COMPLETED", hari=40, total=500),
    ])
    await services.impor_pesanan_marketplace(session, b, [
        _order("B1", "to_ship", "READY_TO_SHIP", hari=1, total=1000),
        _order("B2", "cancelled", "CANCELLED", hari=2, total=700),
    ])
    return a, b


def _sn(hasil):
    return [p.id_eksternal for p in hasil["items"]]


@pytest.mark.asyncio
async def test_stages_split_to_ship_into_needs_processing_and_waiting_for_courier(session, data):
    for tahap, harapan in [
        ("belum_bayar", ["A1"]), ("perlu_diproses", ["A2", "B1"]), ("menunggu_kurir", ["A3"]),
        ("dikirim", ["A4"]), ("selesai", ["A5"]), ("dibatalkan", ["B2"]),
    ]:
        assert sorted(_sn(await services.daftar_pesanan(session, tahap=tahap))) == harapan, tahap


@pytest.mark.asyncio
async def test_filter_by_shop_search_date_sort_and_paging(session, data):
    a, b = data
    assert sorted(_sn(await services.daftar_pesanan(session, akun_id=b.id))) == ["B1", "B2"]
    assert _sn(await services.daftar_pesanan(session, q="budi")) == ["A2"]
    assert _sn(await services.daftar_pesanan(session, q="meja")) == ["A3"]  # product name
    assert _sn(await services.daftar_pesanan(session, q="spx123")) == ["A3"]  # tracking number
    hasil = await services.daftar_pesanan(session, dari=SEKARANG - timedelta(days=5), sampai=SEKARANG)
    assert sorted(_sn(hasil)) == ["A1", "A2", "A3", "B1", "B2"]  # by the day the buyer ordered, not when the ERP saw it
    assert _sn(await services.daftar_pesanan(session, urut="total_besar"))[:2] == ["B1", "B2"]
    assert _sn(await services.daftar_pesanan(session, urut="terlama"))[0] == "A5"
    hal2 = await services.daftar_pesanan(session, urut="terbaru", halaman=2, per_halaman=3)
    assert hal2["total"] == 7 and len(hal2["items"]) == 3


@pytest.mark.asyncio
async def test_print_filter_only_covers_orders_with_a_printable_label(session, data):
    assert _sn(await services.daftar_pesanan(session, resi="belum")) == ["A3"]
    assert _sn(await services.daftar_pesanan(session, resi="sudah")) == []
    with pytest.raises(Exception):
        await services.daftar_pesanan(session, tahap="acak")


@pytest.mark.asyncio
async def test_staff_only_sees_orders_of_their_shops(session, data):
    a, _ = data
    hasil = await services.daftar_pesanan(session, akun_diizinkan={a.id})
    assert sorted(_sn(hasil)) == ["A1", "A2", "A3", "A4", "A5"]
    assert (await services.daftar_pesanan(session, akun_diizinkan=set()))["total"] == 0
    ring = await services.ringkasan_pesanan(session, akun_diizinkan={a.id})
    assert [t["nama_toko"] for t in ring["toko"]] == ["Toko A"]


@pytest.mark.asyncio
async def test_chip_counts_each_ignore_their_own_filter(session, data):
    a, b = data
    semua = await services.ringkasan_pesanan(session)
    assert semua["tahap"] == {"semua": 7, "belum_bayar": 1, "perlu_diproses": 2, "menunggu_kurir": 1, "dikirim": 1, "selesai": 1, "dibatalkan": 1}
    assert {t["nama_toko"]: t["jumlah"] for t in semua["toko"]} == {"Toko A": 5, "Toko B": 2}
    # With shop B chosen the stage counts follow it, but the shop chips still show every shop.
    dengan_b = await services.ringkasan_pesanan(session, akun_id=b.id)
    assert dengan_b["tahap"]["semua"] == 2 and dengan_b["tahap"]["perlu_diproses"] == 1
    assert {t["nama_toko"]: t["jumlah"] for t in dengan_b["toko"]} == {"Toko A": 5, "Toko B": 2}
    # With a stage chosen the shop counts follow it, but the stage chips keep showing every stage.
    cuma_siap = await services.ringkasan_pesanan(session, tahap="perlu_diproses")
    assert {t["nama_toko"]: t["jumlah"] for t in cuma_siap["toko"]} == {"Toko A": 1, "Toko B": 1}
    assert cuma_siap["tahap"]["semua"] == 7


def test_shopee_create_time_becomes_the_order_date():
    row = erp_shopee.normalisasi_pesanan({"order_sn": "X", "order_status": "READY_TO_SHIP", "create_time": 1759550400, "item_list": []})
    assert row["dipesan_at"] == datetime.fromtimestamp(1759550400, tz=timezone.utc)
    assert erp_shopee.normalisasi_pesanan({"order_sn": "Y", "order_status": "UNPAID"})["dipesan_at"] is None


@pytest.mark.asyncio
async def test_an_order_pulled_before_the_column_existed_gets_its_date_on_the_next_pull(session):
    a = await _toko(session, "Toko A")
    sama = _order("Z1", "to_ship", "READY_TO_SHIP", hari=5)
    await services.impor_pesanan_marketplace(session, a, [{**sama, "dipesan_at": None}])
    assert (await services.daftar_pesanan(session))["items"][0].dipesan_at is None
    await services.impor_pesanan_marketplace(session, a, [sama])
    assert (await services.daftar_pesanan(session))["items"][0].dipesan_at is not None


@pytest.mark.asyncio
async def test_dashboard_tables(session, data):
    a, b = data
    sepi = await _toko(session, "Toko Sepi")
    d = await services.laporan_dashboard(session, dari=SEKARANG - timedelta(days=30), sampai=SEKARANG)

    # A5 (40 days ago) is outside the period; unpaid and cancelled orders do not count as revenue.
    assert d["total_omzet"] == Decimal("200") + Decimal("300") + Decimal("400") + Decimal("1000")
    assert d["total_pesanan"] == 4 and d["rata_rata_pesanan"] == Decimal("475")
    assert d["jumlah_toko"] == 3

    per_toko = {t["nama_toko"]: t for t in d["per_toko"]}
    assert per_toko["Toko A"]["pesanan"] == 3 and per_toko["Toko A"]["omzet"] == Decimal("900")
    assert (per_toko["Toko A"]["belum_bayar"], per_toko["Toko A"]["perlu_diproses"], per_toko["Toko A"]["menunggu_kurir"], per_toko["Toko A"]["dikirim"], per_toko["Toko A"]["selesai"]) == (1, 1, 1, 1, 0)
    assert per_toko["Toko B"]["dibatalkan"] == 1 and per_toko["Toko B"]["omzet"] == Decimal("1000")
    assert per_toko["Toko Sepi"]["pesanan"] == 0 and per_toko["Toko Sepi"]["pesanan_terbaru"] is None  # shops without orders still listed
    assert d["per_toko"][0]["nama_toko"] == "Toko B"  # biggest revenue first

    tahap = {t["tahap"]: t for t in d["per_tahap"]}
    assert tahap["perlu_diproses"]["jumlah"] == 2 and tahap["perlu_diproses"]["nilai"] == Decimal("1200")

    kursi = next(p for p in d["produk_terlaris"] if p["nama_produk"] == "Kursi")
    assert kursi["qty_terjual"] == 3 and kursi["pesanan"] == 3
    assert {t["nama_toko"]: t["qty"] for t in kursi["toko"]} == {"Toko A": 2, "Toko B": 1}
    meja = next(p for p in d["produk_terlaris"] if p["nama_produk"] == "Meja Kayu")
    assert meja["qty_terjual"] == 2 and meja["omzet"] == Decimal("300")

    assert sum(h["pesanan"] for h in d["per_hari"]) == 4
    assert d["per_hari"][0]["tanggal"] > d["per_hari"][-1]["tanggal"]  # newest day first
    assert d["data_sejak"] == SEKARANG - timedelta(days=40)


@pytest.mark.asyncio
async def test_orders_sort_by_every_column_both_ways_with_old_values_still_working(session, data):
    async def urut(teks, **kw):
        return _sn(await services.daftar_pesanan(session, urut=teks, **kw))

    assert (await urut("total:desc"))[:2] == ["B1", "B2"]
    assert (await urut("total:asc"))[0] == "A1"
    assert (await urut("tanggal:asc"))[0] == "A5"
    assert (await urut("tanggal:desc"))[0] in ("A1", "B1")
    assert (await urut("nomor:asc"))[:3] == ["A1", "A2", "A3"]
    assert (await urut("nomor:desc"))[0] == "B2"
    toko = await urut("toko:asc")
    assert toko[:5] == sorted(toko[:5]) and set(toko[:5]) == {"A1", "A2", "A3", "A4", "A5"}  # Toko A before Toko B
    assert (await urut("toko:desc"))[0].startswith("B")
    assert (await urut("status:asc"))[0] == "A1"  # belum bayar first
    assert (await urut("status:desc"))[0] == "B2"  # dibatalkan first
    assert len(await urut("kurir:asc")) == 7
    # the previous values keep working
    assert (await urut("terlama"))[0] == "A5" and (await urut("total_besar"))[0] == "B1"
    with pytest.raises(Exception):
        await urut("harga:asc")
    with pytest.raises(Exception):
        await urut("total:naik")


@pytest.mark.asyncio
async def test_orders_without_order_time_are_listed_for_completion_until_filled(session):
    a, b = await _toko(session, "Toko A"), await _toko(session, "Toko B")
    await services.impor_pesanan_marketplace(session, a, [{**_order("OLD1", "unpaid", "UNPAID"), "dipesan_at": None}])
    await services.impor_pesanan_marketplace(session, a, [_order("OK1", "to_ship", "READY_TO_SHIP")])
    await services.impor_pesanan_marketplace(session, b, [{**_order("OTHER", "unpaid", "UNPAID"), "dipesan_at": None}])
    assert await services.id_pesanan_tanpa_waktu_pesan(session, a) == ["OLD1"]  # only this shop, only the missing ones
    await services.impor_pesanan_marketplace(session, a, [_order("OLD1", "unpaid", "UNPAID")])
    assert await services.id_pesanan_tanpa_waktu_pesan(session, a) == []
