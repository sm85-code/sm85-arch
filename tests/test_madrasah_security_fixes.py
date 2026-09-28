"""Regresi untuk 2 celah otorisasi yang ditemukan lewat audit koneksi
frontend-backend:

1. Guru mapel bisa mengintip rekap absensi mapel yang bukan ampuannya
   (gm_rekap_absensi tidak memverifikasi penugasan GuruMapelRombel).
2. Bendahara bisa menandai SPP lunas tanpa pengajuan wali kelas terlebih
   dahulu (pay_spp_manual tidak mengecek diajukan_oleh) -- melewati
   workflow approval 2 langkah: wali kelas ajukan -> bendahara verifikasi.
"""
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.madrasah.modules.madrasah.application import services
from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase
from tenants.madrasah.modules.madrasah.infrastructure.models import (
    AbsensiMadrasah,
    GuruMapelRombel,
    MapelMadrasah,
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
async def test_guru_mapel_tidak_bisa_lihat_rekap_mapel_lain(session):
    guru_a1 = UserMadrasah(nama="Guru A1", no_hp="081200000010", password_hash="x", role="guru")
    session.add(guru_a1)
    await session.flush()

    rombel = RombelMadrasah(nama="Kelas A")
    mapel_a1 = MapelMadrasah(kode="A1", nama="Mapel A1")
    mapel_a2 = MapelMadrasah(kode="A2", nama="Mapel A2")
    santri = SantriMadrasah(nama="Ahmad")
    session.add_all([rombel, mapel_a1, mapel_a2, santri])
    await session.flush()
    santri.rombel_id = rombel.id
    await session.flush()

    # guru_a1 hanya ditugaskan mengajar mapel_a1 di rombel ini
    session.add(GuruMapelRombel(guru_id=guru_a1.id, mapel_id=mapel_a1.id, rombel_id=rombel.id))
    session.add(AbsensiMadrasah(tanggal=date.today(), status="hadir", santri_id=santri.id, guru_id=guru_a1.id, mapel_id=mapel_a1.id))
    session.add(AbsensiMadrasah(tanggal=date.today(), status="hadir", santri_id=santri.id, guru_id=guru_a1.id, mapel_id=mapel_a2.id))
    await session.flush()

    # boleh lihat rekap mapel yang dia ampu
    rekap = await services.rekap_absensi_mapel(session, rombel.id, mapel_a1.id, guru_a1)
    assert len(rekap) == 1

    # TIDAK boleh lihat rekap mapel lain di rombel yang sama
    with pytest.raises(services.MadrasahForbiddenError):
        await services.rekap_absensi_mapel(session, rombel.id, mapel_a2.id, guru_a1)


@pytest.mark.asyncio
async def test_admin_dan_kepala_sekolah_bebas_lihat_rekap_mapel_apapun(session):
    admin = UserMadrasah(nama="Admin", no_hp="081200000011", password_hash="x", role="admin")
    session.add(admin)
    await session.flush()

    rombel = RombelMadrasah(nama="Kelas A")
    mapel = MapelMadrasah(kode="A2", nama="Mapel A2")
    session.add_all([rombel, mapel])
    await session.flush()

    # admin tidak punya penugasan GuruMapelRombel sama sekali, tetap boleh lihat
    rekap = await services.rekap_absensi_mapel(session, rombel.id, mapel.id, admin)
    assert rekap == []


@pytest.mark.asyncio
async def test_pay_spp_manual_menolak_tagihan_yang_belum_diajukan(session):
    santri = SantriMadrasah(nama="Ahmad")
    session.add(santri)
    await session.flush()
    tagihan = TagihanSyahriyah(bulan_tahun="2026-01", nominal="50000", status_bayar=False, santri_id=santri.id)
    session.add(tagihan)
    await session.flush()

    with pytest.raises(services.MadrasahForbiddenError):
        await services.pay_spp_manual(session, tagihan.id)

    await session.refresh(tagihan)
    assert tagihan.status_bayar is False


@pytest.mark.asyncio
async def test_pay_spp_manual_berhasil_setelah_diajukan_wali_kelas(session):
    wali_kelas = UserMadrasah(nama="Wali Kelas", no_hp="081200000012", password_hash="x", role="wali_kelas")
    session.add(wali_kelas)
    await session.flush()
    rombel = RombelMadrasah(nama="Kelas A", wali_kelas_id=wali_kelas.id)
    santri = SantriMadrasah(nama="Ahmad")
    session.add_all([rombel, santri])
    await session.flush()
    santri.rombel_id = rombel.id
    tagihan = TagihanSyahriyah(bulan_tahun="2026-01", nominal="50000", status_bayar=False, santri_id=santri.id)
    session.add(tagihan)
    await session.flush()

    await services.ajukan_pembayaran(session, wali_kelas, tagihan.id)
    row = await services.pay_spp_manual(session, tagihan.id)
    assert row.status_bayar is True
