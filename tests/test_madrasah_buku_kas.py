"""Exercises the Buku Kas (cash ledger) feature against a real (SQLite,
in-memory) async session -- ATK/honor manual entries, and automatic
"masuk" entries when SPP is paid.
"""
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.madrasah.modules.madrasah.application import services
from tenants.madrasah.modules.madrasah.application.schemas import BukuKasIn
from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase
from tenants.madrasah.modules.madrasah.infrastructure.models import SantriMadrasah, TagihanSyahriyah


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MadrasahBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_buku_kas_entry_and_list_filters_by_month(session):
    await services.create_buku_kas_entry(
        session, BukuKasIn(tanggal="2026-01-10", tipe="keluar", kategori="ATK", jumlah="50000", keterangan="Kertas"), dicatat_oleh=None
    )
    await services.create_buku_kas_entry(
        session, BukuKasIn(tanggal="2026-02-05", tipe="keluar", kategori="Honor Guru", jumlah="200000", keterangan="Honor Januari"), dicatat_oleh=None
    )

    januari = await services.list_buku_kas(session, "2026-01")
    assert len(januari) == 1
    assert januari[0].kategori == "ATK"

    semua = await services.list_buku_kas(session)
    assert len(semua) == 2


@pytest.mark.asyncio
async def test_pay_spp_manual_creates_masuk_entry_in_buku_kas(session):
    santri = SantriMadrasah(nama="Ahmad")
    session.add(santri)
    await session.flush()
    tagihan = TagihanSyahriyah(bulan_tahun="2026-01", nominal="50000", status_bayar=False, santri_id=santri.id, diajukan_oleh="guru-1")
    session.add(tagihan)
    await session.flush()

    await services.pay_spp_manual(session, tagihan.id)

    rows = await services.list_buku_kas(session)
    assert len(rows) == 1
    assert rows[0].tipe == "masuk"
    assert rows[0].kategori == "SPP"
    assert rows[0].jumlah == Decimal(tagihan.nominal)


@pytest.mark.asyncio
async def test_laporan_keuangan_computes_saldo(session):
    await services.create_buku_kas_entry(
        session, BukuKasIn(tanggal="2026-01-01", tipe="masuk", kategori="SPP", jumlah="100000", keterangan=""), dicatat_oleh=None
    )
    await services.create_buku_kas_entry(
        session, BukuKasIn(tanggal="2026-01-02", tipe="keluar", kategori="ATK", jumlah="30000", keterangan=""), dicatat_oleh=None
    )

    laporan = await services.laporan_keuangan(session, "2026-01")
    assert laporan["total_masuk"] == "100000.00"
    assert laporan["total_keluar"] == "30000.00"
    assert laporan["saldo"] == "70000.00"
    assert len(laporan["entries"]) == 2
