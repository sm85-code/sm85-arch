"""Regresi untuk bug "admin tidak bisa hapus guru default": delete_guru harus
berhasil walau akun itu masih dirujuk sebagai wali_kelas di rombel, pencatat
absensi/pengumuman/buku-kas, atau pengaju tagihan -- bukan gagal dengan
IntegrityError karena FK constraint di DB masih RESTRICT.
"""
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.madrasah.modules.madrasah.application import services
from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase
from tenants.madrasah.modules.madrasah.infrastructure.models import (
    AbsensiMadrasah,
    BukuKasMadrasah,
    GuruMapelRombel,
    MapelMadrasah,
    PengumumanMadrasah,
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
async def test_delete_guru_referenced_everywhere_still_succeeds(session):
    guru = UserMadrasah(nama="Guru Madrasah", no_hp="081200000002", password_hash="x", role="wali_kelas")
    session.add(guru)
    await session.flush()

    rombel = RombelMadrasah(nama="Kelas A", wali_kelas_id=guru.id)
    santri = SantriMadrasah(nama="Ahmad")
    mapel = MapelMadrasah(kode="MTK", nama="Matematika")
    session.add_all([rombel, santri, mapel])
    await session.flush()
    santri.rombel_id = rombel.id
    await session.flush()

    session.add(AbsensiMadrasah(tanggal=date.today(), status="hadir", santri_id=santri.id, guru_id=guru.id))
    session.add(PengumumanMadrasah(judul="Libur", isi="", tanggal=date.today(), dibuat_by=guru.id))
    session.add(BukuKasMadrasah(tanggal=date.today(), tipe="keluar", kategori="Honor", jumlah="100000", dicatat_oleh=guru.id))
    session.add(GuruMapelRombel(guru_id=guru.id, mapel_id=mapel.id, rombel_id=rombel.id))
    tagihan = TagihanSyahriyah(bulan_tahun="2026-01", nominal="50000", status_bayar=False, santri_id=santri.id, diajukan_oleh=guru.id)
    session.add(tagihan)
    await session.flush()

    await services.delete_guru(session, guru.id)
    await session.flush()

    assert await session.get(UserMadrasah, guru.id) is None

    await session.refresh(rombel)
    assert rombel.wali_kelas_id is None

    await session.refresh(tagihan)
    assert tagihan.diajukan_oleh is None


@pytest.mark.asyncio
async def test_delete_guru_not_found_raises(session):
    with pytest.raises(services.MadrasahNotFoundError):
        await services.delete_guru(session, "tidak-ada")
