"""Tahap 3: multi-gudang + transfer, staff-akun scoping, pengiriman manual,
settlement reconciliation, laporan ringkas. All local-data features, no
marketplace API dependency (see tenants/marketplace_erp/IDEAL_FOLLOWUPS.md).
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import (
    AkunMarketplaceIn,
    GudangIn,
    ItemPesananIn,
    PengirimanIn,
    PesananIn,
    ProdukIn,
    SettlementIn,
    SettlementPatch,
    StaffAkunIn,
    StokTransferIn,
    UserCreateIn,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import (
    akun_ids_diizinkan,
    pastikan_akses_akun,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    await engine.dispose()


async def _sku(session, sku="SKU-T3", stok=10):
    return await services.create_produk(
        session, ProdukIn(sku_induk=sku, nama="Barang", harga_dasar=Decimal("10000"), stok=stok)
    )


async def _akun(session, platform="shopee", nama="Toko A"):
    return await services.create_akun_marketplace(session, AkunMarketplaceIn(platform=platform, nama_toko=nama))


async def _staff(session, email="staff@test.com"):
    return await services.create_user(
        session, UserCreateIn(nama="Staff", email=email, password="rahasia123", role="staff")
    )


async def _pesanan_to_ship(session, produk, akun, id_eksternal="ORD-1", qty=2):
    pesanan = await services.create_pesanan(
        session,
        PesananIn(
            platform=akun.platform,
            id_eksternal=id_eksternal,
            akun_id=akun.id,
            items=[
                ItemPesananIn(nama_produk=produk.nama, harga_satuan=produk.harga_dasar, qty=qty, produk_id=produk.id)
            ],
        ),
    )
    return await services.ubah_status_pesanan(session, pesanan.id, "to_ship")


# --- Multi-gudang + transfer --------------------------------------------------


@pytest.mark.asyncio
async def test_create_gudang_rejects_duplicate_kode(session):
    await services.create_gudang(session, GudangIn(kode="JKT", nama="Gudang Jakarta"))
    with pytest.raises(HTTPException) as exc_info:
        await services.create_gudang(session, GudangIn(kode="JKT", nama="Gudang Jakarta 2"))
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_transfer_stok_moves_between_warehouses(session):
    produk = await _sku(session, stok=20)
    gudang_asal = await services.ensure_default_gudang(session)
    gudang_tujuan = await services.create_gudang(session, GudangIn(kode="JKT", nama="Gudang Jakarta"))

    # Initial stock landed in DEFAULT via create_produk's ledger entry.
    await services.transfer_stok(
        session,
        StokTransferIn(produk_id=produk.id, dari_gudang_id=gudang_asal.id, ke_gudang_id=gudang_tujuan.id, qty=5),
    )
    ledger = await services.list_stok_ledger(session, produk_id=produk.id)
    reasons = {row.reason for row in ledger}
    assert "transfer_out" in reasons and "transfer_in" in reasons
    # Produk.stok (available-everywhere cache) is untouched by a transfer.
    refreshed = await services.get_produk(session, produk.id)
    assert refreshed.stok == 20


@pytest.mark.asyncio
async def test_transfer_stok_rejects_insufficient_source_balance(session):
    produk = await _sku(session, stok=5)
    gudang_asal = await services.ensure_default_gudang(session)
    gudang_tujuan = await services.create_gudang(session, GudangIn(kode="JKT", nama="Gudang Jakarta"))
    with pytest.raises(HTTPException) as exc_info:
        await services.transfer_stok(
            session,
            StokTransferIn(
                produk_id=produk.id, dari_gudang_id=gudang_asal.id, ke_gudang_id=gudang_tujuan.id, qty=999
            ),
        )
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_transfer_stok_rejects_same_source_and_destination(session):
    produk = await _sku(session, stok=5)
    gudang = await services.ensure_default_gudang(session)
    with pytest.raises(HTTPException) as exc_info:
        await services.transfer_stok(
            session, StokTransferIn(produk_id=produk.id, dari_gudang_id=gudang.id, ke_gudang_id=gudang.id, qty=1)
        )
    assert exc_info.value.status_code == 400


# --- Staff-akun scoping --------------------------------------------------------


@pytest.mark.asyncio
async def test_staff_sees_only_assigned_akun(session):
    staff = await _staff(session)
    akun_a = await _akun(session, nama="Toko A")
    await _akun(session, nama="Toko B")
    await services.assign_staff_akun(session, StaffAkunIn(user_id=staff.id, akun_id=akun_a.id))

    allowed = await akun_ids_diizinkan(staff, session)
    assert allowed == [akun_a.id]


@pytest.mark.asyncio
async def test_pastikan_akses_akun_rejects_unassigned_shop(session):
    staff = await _staff(session)
    akun_a = await _akun(session, nama="Toko A")
    akun_b = await _akun(session, nama="Toko B")
    await services.assign_staff_akun(session, StaffAkunIn(user_id=staff.id, akun_id=akun_a.id))

    await pastikan_akses_akun(staff, session, akun_a.id)  # no raise
    with pytest.raises(HTTPException) as exc_info:
        await pastikan_akses_akun(staff, session, akun_b.id)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_owner_is_never_scoped(session):
    owner = await services.create_user(
        session, UserCreateIn(nama="Owner2", email="owner2@test.com", password="rahasia123", role="owner")
    )
    akun = await _akun(session)
    assert await akun_ids_diizinkan(owner, session) is None
    await pastikan_akses_akun(owner, session, akun.id)  # no raise, no assignment needed


@pytest.mark.asyncio
async def test_assign_staff_akun_rejects_duplicate(session):
    staff = await _staff(session)
    akun = await _akun(session)
    await services.assign_staff_akun(session, StaffAkunIn(user_id=staff.id, akun_id=akun.id))
    with pytest.raises(HTTPException) as exc_info:
        await services.assign_staff_akun(session, StaffAkunIn(user_id=staff.id, akun_id=akun.id))
    assert exc_info.value.status_code == 409


# --- Pengiriman -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_pengiriman_requires_to_ship_or_later(session):
    produk = await _sku(session)
    akun = await _akun(session)
    pesanan = await services.create_pesanan(
        session, PesananIn(platform=akun.platform, id_eksternal="ORD-2", akun_id=akun.id)
    )
    with pytest.raises(HTTPException) as exc_info:
        await services.set_pengiriman(session, pesanan.id, PengirimanIn(kurir="JNE", nomor_resi="RESI123"))
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_set_pengiriman_ok_once_to_ship(session):
    produk = await _sku(session)
    akun = await _akun(session)
    pesanan = await _pesanan_to_ship(session, produk, akun, id_eksternal="ORD-3")
    updated = await services.set_pengiriman(session, pesanan.id, PengirimanIn(kurir="JNE", nomor_resi="RESI123"))
    assert updated.kurir == "JNE"
    assert updated.nomor_resi == "RESI123"
    assert updated.tanggal_kirim is not None


# --- Settlement ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_settlement_matched_when_net_reconciles(session):
    akun = await _akun(session)
    now = datetime.now(timezone.utc)
    settlement = await services.create_settlement(
        session,
        SettlementIn(
            akun_id=akun.id,
            periode_mulai=now - timedelta(days=7),
            periode_selesai=now,
            gross_sales=Decimal("100000"),
            fee_platform=Decimal("5000"),
            fee_payment=Decimal("2000"),
            net=Decimal("93000"),
        ),
    )
    assert settlement.status == "matched"
    assert settlement.platform == akun.platform


@pytest.mark.asyncio
async def test_create_settlement_flags_discrepancy(session):
    akun = await _akun(session)
    now = datetime.now(timezone.utc)
    settlement = await services.create_settlement(
        session,
        SettlementIn(
            akun_id=akun.id,
            periode_mulai=now - timedelta(days=7),
            periode_selesai=now,
            gross_sales=Decimal("100000"),
            fee_platform=Decimal("5000"),
            net=Decimal("50000"),  # way off from 95000 expected
        ),
    )
    assert settlement.status == "discrepancy"


@pytest.mark.asyncio
async def test_update_settlement_can_promote_to_paid(session):
    akun = await _akun(session)
    now = datetime.now(timezone.utc)
    settlement = await services.create_settlement(
        session,
        SettlementIn(
            akun_id=akun.id, periode_mulai=now - timedelta(days=1), periode_selesai=now, gross_sales=Decimal("0")
        ),
    )
    updated = await services.update_settlement(session, settlement.id, SettlementPatch(status="paid"))
    assert updated.status == "paid"


@pytest.mark.asyncio
async def test_create_settlement_rejects_bad_period(session):
    akun = await _akun(session)
    now = datetime.now(timezone.utc)
    with pytest.raises(HTTPException) as exc_info:
        await services.create_settlement(
            session,
            SettlementIn(akun_id=akun.id, periode_mulai=now, periode_selesai=now - timedelta(days=1)),
        )
    assert exc_info.value.status_code == 400


# --- Laporan ringkas ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_laporan_ringkas_aggregates_omzet_and_top_produk(session):
    produk = await _sku(session, stok=50)
    akun = await _akun(session)
    await _pesanan_to_ship(session, produk, akun, id_eksternal="ORD-L1", qty=3)
    await _pesanan_to_ship(session, produk, akun, id_eksternal="ORD-L2", qty=2)

    now = datetime.now(timezone.utc)
    laporan = await services.laporan_ringkas(session, dari=now - timedelta(days=1), sampai=now + timedelta(days=1))

    assert laporan["total_omzet"] == Decimal("50000")  # (3+2) * 10000
    assert laporan["jumlah_pesanan_per_status"]["to_ship"] == 2
    assert laporan["produk_terlaris"][0]["qty_terjual"] == 5


@pytest.mark.asyncio
async def test_laporan_ringkas_flags_stok_kritis(session):
    await _sku(session, sku="SKU-LOW", stok=1)
    await _sku(session, sku="SKU-HIGH", stok=100)
    now = datetime.now(timezone.utc)
    laporan = await services.laporan_ringkas(
        session, dari=now - timedelta(days=1), sampai=now + timedelta(days=1), batas_stok_kritis=5
    )
    skus_kritis = {row["sku_induk"] for row in laporan["stok_kritis"]}
    assert skus_kritis == {"SKU-LOW"}
