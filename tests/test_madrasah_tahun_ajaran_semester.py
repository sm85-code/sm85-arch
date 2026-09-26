"""Regression tests for Fase 1.1 (Tahun Ajaran & Semester):

- Membuat tahun ajaran dan semester di bawahnya.
- Hanya satu semester boleh "aktif" pada satu waktu; mengaktifkan semester
  baru menutup semester aktif yang lama.
- Absensi/progres/tagihan/jadwal baru otomatis di-tag ke semester aktif,
  dan berhenti di-tag begitu semester itu ditutup (kembali None sampai ada
  semester lain yang diaktifkan) -- data lama tidak berubah.
"""
from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.madrasah.modules.madrasah.application import services
from tenants.madrasah.modules.madrasah.application.schemas import (
    AbsenBulkRequest,
    AbsenItem,
    ProgresCreateRequest,
    SemesterIn,
    TahunAjaranIn,
)
from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase
from tenants.madrasah.modules.madrasah.infrastructure.models import SantriMadrasah


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MadrasahBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    await engine.dispose()


async def _buat_semester(session, nama="Ganjil") -> str:
    ta = await services.create_tahun_ajaran(
        session, TahunAjaranIn(kode="2025/2026", tanggal_mulai=dt.date(2025, 7, 1), tanggal_selesai=dt.date(2026, 6, 30))
    )
    sem = await services.create_semester(
        session,
        SemesterIn(tahun_ajaran_id=ta.id, nama=nama, tanggal_mulai=dt.date(2025, 7, 1), tanggal_selesai=dt.date(2025, 12, 31)),
    )
    return sem.id


@pytest.mark.asyncio
async def test_new_semester_starts_as_draft(session):
    semester_id = await _buat_semester(session)
    row = await session.get(services.SemesterMadrasah, semester_id)
    assert row.status == "draft"
    assert await services.get_semester_aktif(session) is None


@pytest.mark.asyncio
async def test_aktifkan_semester_makes_it_the_active_one(session):
    semester_id = await _buat_semester(session)
    await services.aktifkan_semester(session, semester_id)

    aktif = await services.get_semester_aktif(session)
    assert aktif is not None
    assert aktif.id == semester_id
    assert aktif.status == "aktif"


@pytest.mark.asyncio
async def test_activating_a_new_semester_deactivates_the_old_one(session):
    first_id = await _buat_semester(session, nama="Ganjil")
    await services.aktifkan_semester(session, first_id)

    ta2 = await services.create_tahun_ajaran(
        session, TahunAjaranIn(kode="2026/2027", tanggal_mulai=dt.date(2026, 7, 1), tanggal_selesai=dt.date(2027, 6, 30))
    )
    second = await services.create_semester(
        session,
        SemesterIn(tahun_ajaran_id=ta2.id, nama="Genap", tanggal_mulai=dt.date(2026, 1, 1), tanggal_selesai=dt.date(2026, 6, 30)),
    )
    await services.aktifkan_semester(session, second.id)

    first = await session.get(services.SemesterMadrasah, first_id)
    assert first.status == "ditutup"
    aktif = await services.get_semester_aktif(session)
    assert aktif.id == second.id


@pytest.mark.asyncio
async def test_tutup_semester_clears_active_semester(session):
    semester_id = await _buat_semester(session)
    await services.aktifkan_semester(session, semester_id)
    await services.tutup_semester(session, semester_id)

    assert await services.get_semester_aktif(session) is None
    row = await session.get(services.SemesterMadrasah, semester_id)
    assert row.status == "ditutup"


@pytest.mark.asyncio
async def test_new_absensi_and_progres_are_tagged_with_active_semester(session):
    santri = SantriMadrasah(nama="Santri A")
    session.add(santri)
    await session.flush()

    semester_id = await _buat_semester(session)
    await services.aktifkan_semester(session, semester_id)

    absensi_rows = await services.bulk_insert_absensi(
        session, AbsenBulkRequest(tanggal=dt.date.today(), items=[AbsenItem(santri_id=santri.id, status="hadir")])
    )
    assert absensi_rows[0].semester_id == semester_id

    progres = await services.create_progres(
        session, ProgresCreateRequest(tanggal=dt.date.today(), santri_id=santri.id, tipe="hafalan", capaian="Juz 1")
    )
    assert progres.semester_id == semester_id


@pytest.mark.asyncio
async def test_records_after_closing_semester_are_untagged_not_backdated(session):
    """Setelah semester ditutup dan belum ada semester lain yang aktif,
    baris baru harus tetap semester_id=None (tidak salah ditandai ke
    semester yang sudah ditutup itu)."""
    santri = SantriMadrasah(nama="Santri B")
    session.add(santri)
    await session.flush()

    semester_id = await _buat_semester(session)
    await services.aktifkan_semester(session, semester_id)
    await services.tutup_semester(session, semester_id)

    absensi_rows = await services.bulk_insert_absensi(
        session, AbsenBulkRequest(tanggal=dt.date.today(), items=[AbsenItem(santri_id=santri.id, status="hadir")])
    )
    assert absensi_rows[0].semester_id is None


@pytest.mark.asyncio
async def test_generate_spp_massal_tags_active_semester(session):
    santri = SantriMadrasah(nama="Santri C")
    session.add(santri)
    await session.flush()

    semester_id = await _buat_semester(session)
    await services.aktifkan_semester(session, semester_id)

    tagihan = await services.generate_spp_massal(session)
    assert len(tagihan) == 1
    assert tagihan[0].semester_id == semester_id


@pytest.mark.asyncio
async def test_create_semester_rejects_unknown_tahun_ajaran(session):
    with pytest.raises(services.MadrasahNotFoundError):
        await services.create_semester(
            session,
            SemesterIn(tahun_ajaran_id="tidak-ada", nama="Ganjil", tanggal_mulai=dt.date(2025, 7, 1), tanggal_selesai=dt.date(2025, 12, 31)),
        )
