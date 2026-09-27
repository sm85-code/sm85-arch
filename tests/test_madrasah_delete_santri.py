"""Regresi untuk bug "hapus santri gagal 500 di production": delete_santri
harus berhasil walau santri masih direferensikan di riwayat penempatan,
absensi, progres hafalan, pesan, atau tagihan yang belum lunas -- bukan
gagal dengan IntegrityError karena FK constraint di DB masih RESTRICT
(constraint aslinya dibuat sebelum ondelete=CASCADE ada di models.py).

Tapi kalau ada tagihan yang SUDAH lunas, penghapusan harus ditolak supaya
laporan keuangan (buku kas/jurnal) tidak jadi tidak konsisten.
"""
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.madrasah.modules.madrasah.application import services
from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase
from tenants.madrasah.modules.madrasah.infrastructure.models import (
    AbsensiMadrasah,
    PesanMadrasah,
    ProgresHafalan,
    RiwayatPenempatanSantri,
    RombelMadrasah,
    SantriMadrasah,
    TagihanSyahriyah,
    UserMadrasah,
)


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
async def test_delete_santri_referenced_everywhere_still_succeeds(session):
    guru = UserMadrasah(nama="Guru Madrasah", no_hp="081200000002", password_hash="x", role="wali_kelas")
    session.add(guru)
    await session.flush()

    rombel = RombelMadrasah(nama="Kelas A", wali_kelas_id=guru.id)
    santri = SantriMadrasah(nama="Ahmad")
    session.add_all([rombel, santri])
    await session.flush()
    santri.rombel_id = rombel.id
    await session.flush()

    session.add(RiwayatPenempatanSantri(santri_id=santri.id, rombel_id=rombel.id, tanggal_masuk=date.today()))
    session.add(AbsensiMadrasah(tanggal=date.today(), status="hadir", santri_id=santri.id, guru_id=guru.id))
    session.add(ProgresHafalan(tanggal=date.today(), santri_id=santri.id))
    session.add(PesanMadrasah(santri_id=santri.id, dari_user_id=guru.id, isi="Halo"))
    session.add(TagihanSyahriyah(bulan_tahun="2026-01", nominal="150000", status_bayar=False, santri_id=santri.id))
    await session.flush()

    await services.delete_santri(session, santri.id)
    await session.flush()

    assert await session.get(SantriMadrasah, santri.id) is None


@pytest.mark.asyncio
async def test_delete_santri_dengan_tagihan_lunas_ditolak(session):
    santri = SantriMadrasah(nama="Ahmad")
    session.add(santri)
    await session.flush()
    session.add(TagihanSyahriyah(bulan_tahun="2026-01", nominal="150000", status_bayar=True, santri_id=santri.id))
    await session.flush()

    with pytest.raises(services.MadrasahForbiddenError):
        await services.delete_santri(session, santri.id)

    assert await session.get(SantriMadrasah, santri.id) is not None


@pytest.mark.asyncio
async def test_delete_santri_not_found_raises(session):
    with pytest.raises(services.MadrasahNotFoundError):
        await services.delete_santri(session, "tidak-ada")
