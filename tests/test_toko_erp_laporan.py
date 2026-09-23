"""Exercises the toko-erp (marketplace) financial/sales reporting functions
-- laporan_penjualan_erp, laporan_produk_terlaris_erp,
laporan_ringkasan_status_erp and laporan_per_akun -- against a real
(SQLite, in-memory) async session, same pattern as
tests/test_toko_erp_marketplace.py and tests/test_toko_pengiriman_laporan.py.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.toko.modules.erp.application import services as erp_services
from tenants.toko.modules.erp.application.schemas import AkunMarketplaceIn
from tenants.toko.modules.erp.infrastructure.models import ItemPesananERP, PesananERP
from tenants.toko.modules.toko.infrastructure.auth import require_roles_toko
from tenants.toko.modules.toko.infrastructure.database import TokoBase
from tenants.toko.modules.toko.infrastructure.models import UserToko


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(TokoBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    await engine.dispose()


async def _make_akun(session, *, platform: str, nama_toko: str):
    return await erp_services.create_akun_marketplace(session, AkunMarketplaceIn(platform=platform, nama_toko=nama_toko))


async def _make_pesanan(
    session,
    *,
    akun_id: str,
    platform: str,
    id_eksternal: str,
    status_: str,
    total: Decimal,
    nama_produk: str = "Barang",
    qty: int = 1,
    created_at=None,
) -> PesananERP:
    pesanan = PesananERP(
        platform=platform,
        akun_id=akun_id,
        id_eksternal=id_eksternal,
        status=status_,
        nama_pembeli="Budi",
        total=total,
    )
    if created_at is not None:
        pesanan.created_at = created_at
    session.add(pesanan)
    await session.flush()
    session.add(
        ItemPesananERP(
            pesanan_id=pesanan.id,
            nama_produk=nama_produk,
            harga_satuan=total / qty if qty else total,
            qty=qty,
            subtotal=total,
        )
    )
    await session.flush()
    return pesanan


# --- laporan_penjualan_erp ---------------------------------------------------


@pytest.mark.asyncio
async def test_laporan_penjualan_erp_only_counts_qualifying_statuses(session):
    today = date.today()
    akun = await _make_akun(session, platform="shopee", nama_toko="Toko Shopee A")

    await _make_pesanan(session, akun_id=akun.id, platform="shopee", id_eksternal="S-1", status_="completed", total=Decimal("10000"))
    await _make_pesanan(session, akun_id=akun.id, platform="shopee", id_eksternal="S-2", status_="unpaid", total=Decimal("999999"))
    await _make_pesanan(session, akun_id=akun.id, platform="shopee", id_eksternal="S-3", status_="cancelled", total=Decimal("999999"))

    laporan = await erp_services.laporan_penjualan_erp(session, today - timedelta(days=1), today + timedelta(days=1))

    assert laporan["grand_total"] == "10000.00"
    assert sum(h["jumlah_pesanan"] for h in laporan["harian"]) == 1


@pytest.mark.asyncio
async def test_laporan_penjualan_erp_filters_by_date_range(session):
    today = date.today()
    akun = await _make_akun(session, platform="shopee", nama_toko="Toko Shopee B")
    await _make_pesanan(
        session,
        akun_id=akun.id,
        platform="shopee",
        id_eksternal="S-10",
        status_="completed",
        total=Decimal("5000"),
        created_at=today - timedelta(days=10),
    )
    await _make_pesanan(session, akun_id=akun.id, platform="shopee", id_eksternal="S-11", status_="completed", total=Decimal("7000"))

    laporan = await erp_services.laporan_penjualan_erp(session, today - timedelta(days=1), today + timedelta(days=1))
    assert laporan["grand_total"] == "7000.00"


@pytest.mark.asyncio
async def test_laporan_penjualan_erp_filters_by_platform_and_akun(session):
    today = date.today()
    akun_shopee_1 = await _make_akun(session, platform="shopee", nama_toko="Toko Shopee 1")
    akun_shopee_2 = await _make_akun(session, platform="shopee", nama_toko="Toko Shopee 2")
    akun_lazada = await _make_akun(session, platform="lazada", nama_toko="Toko Lazada 1")

    await _make_pesanan(session, akun_id=akun_shopee_1.id, platform="shopee", id_eksternal="S-1", status_="completed", total=Decimal("10000"))
    await _make_pesanan(session, akun_id=akun_shopee_2.id, platform="shopee", id_eksternal="S-2", status_="completed", total=Decimal("20000"))
    await _make_pesanan(session, akun_id=akun_lazada.id, platform="lazada", id_eksternal="L-1", status_="completed", total=Decimal("30000"))

    dari, sampai = today - timedelta(days=1), today + timedelta(days=1)

    hanya_shopee = await erp_services.laporan_penjualan_erp(session, dari, sampai, platform="shopee")
    assert hanya_shopee["grand_total"] == "30000.00"

    hanya_akun_shopee_1 = await erp_services.laporan_penjualan_erp(session, dari, sampai, akun_id=akun_shopee_1.id)
    assert hanya_akun_shopee_1["grand_total"] == "10000.00"

    kombinasi = await erp_services.laporan_penjualan_erp(session, dari, sampai, platform="shopee", akun_id=akun_shopee_2.id)
    assert kombinasi["grand_total"] == "20000.00"


# --- laporan_produk_terlaris_erp ---------------------------------------------


@pytest.mark.asyncio
async def test_laporan_produk_terlaris_erp_ranks_by_qty(session):
    today = date.today()
    akun = await _make_akun(session, platform="shopee", nama_toko="Toko Shopee C")
    await _make_pesanan(
        session, akun_id=akun.id, platform="shopee", id_eksternal="S-20", status_="completed",
        total=Decimal("50000"), nama_produk="Sabun", qty=5,
    )
    await _make_pesanan(
        session, akun_id=akun.id, platform="shopee", id_eksternal="S-21", status_="completed",
        total=Decimal("20000"), nama_produk="Sampo", qty=2,
    )

    terlaris = await erp_services.laporan_produk_terlaris_erp(session, today - timedelta(days=1), today + timedelta(days=1))
    assert len(terlaris) == 2
    assert terlaris[0]["nama_produk"] == "Sabun"
    assert terlaris[0]["total_qty"] == 5
    assert terlaris[0]["total_omzet"] == "50000.00"


# --- laporan_ringkasan_status_erp --------------------------------------------


@pytest.mark.asyncio
async def test_laporan_ringkasan_status_erp_counts_per_status_with_filters(session):
    akun_shopee = await _make_akun(session, platform="shopee", nama_toko="Toko Shopee D")
    akun_lazada = await _make_akun(session, platform="lazada", nama_toko="Toko Lazada D")

    await _make_pesanan(session, akun_id=akun_shopee.id, platform="shopee", id_eksternal="S-30", status_="unpaid", total=Decimal("1000"))
    await _make_pesanan(session, akun_id=akun_shopee.id, platform="shopee", id_eksternal="S-31", status_="completed", total=Decimal("2000"))
    await _make_pesanan(session, akun_id=akun_lazada.id, platform="lazada", id_eksternal="L-30", status_="completed", total=Decimal("3000"))

    semua = await erp_services.laporan_ringkasan_status_erp(session)
    assert semua == {"unpaid": 1, "completed": 2}

    hanya_shopee = await erp_services.laporan_ringkasan_status_erp(session, platform="shopee")
    assert hanya_shopee == {"unpaid": 1, "completed": 1}

    hanya_akun_lazada = await erp_services.laporan_ringkasan_status_erp(session, akun_id=akun_lazada.id)
    assert hanya_akun_lazada == {"completed": 1}


# --- laporan_per_akun ---------------------------------------------------------


@pytest.mark.asyncio
async def test_laporan_per_akun_breaks_down_by_shop_across_platforms(session):
    today = date.today()
    akun_shopee_1 = await _make_akun(session, platform="shopee", nama_toko="Toko Shopee 1")
    akun_shopee_2 = await _make_akun(session, platform="shopee", nama_toko="Toko Shopee 2")
    akun_lazada = await _make_akun(session, platform="lazada", nama_toko="Toko Lazada 1")
    akun_tanpa_penjualan = await _make_akun(session, platform="blibli", nama_toko="Toko Blibli Baru")

    await _make_pesanan(session, akun_id=akun_shopee_1.id, platform="shopee", id_eksternal="S-40", status_="completed", total=Decimal("10000"))
    await _make_pesanan(session, akun_id=akun_shopee_1.id, platform="shopee", id_eksternal="S-41", status_="to_ship", total=Decimal("5000"))
    await _make_pesanan(session, akun_id=akun_shopee_2.id, platform="shopee", id_eksternal="S-42", status_="completed", total=Decimal("30000"))
    await _make_pesanan(session, akun_id=akun_lazada.id, platform="lazada", id_eksternal="L-40", status_="completed", total=Decimal("7000"))
    # unpaid tidak boleh ikut terhitung
    await _make_pesanan(session, akun_id=akun_lazada.id, platform="lazada", id_eksternal="L-41", status_="unpaid", total=Decimal("999999"))

    hasil = await erp_services.laporan_per_akun(session, today - timedelta(days=1), today + timedelta(days=1))

    per_akun = {r["akun_id"]: r for r in hasil}
    assert per_akun[akun_shopee_1.id]["total_penjualan"] == "15000.00"
    assert per_akun[akun_shopee_1.id]["nama_toko"] == "Toko Shopee 1"
    assert per_akun[akun_shopee_1.id]["platform"] == "shopee"
    assert per_akun[akun_shopee_2.id]["total_penjualan"] == "30000.00"
    assert per_akun[akun_lazada.id]["total_penjualan"] == "7000.00"
    # Akun tanpa pesanan terhitung dalam rentang tidak ditampilkan.
    assert akun_tanpa_penjualan.id not in per_akun


# --- Role guard: pembeli rejected from admin/erp report endpoints -----------


@pytest.mark.asyncio
async def test_require_roles_toko_rejects_pembeli_for_laporan_erp():
    guard = require_roles_toko("admin_toko", "owner")
    pembeli = UserToko(nama="Pembeli", email="p-laporan@test.com", password_hash="x", role="pembeli")

    with pytest.raises(HTTPException) as exc_info:
        await guard(user=pembeli)
    assert exc_info.value.status_code == 403
