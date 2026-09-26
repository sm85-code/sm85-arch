"""Regression tests for Fase 1.2 (Riwayat Kelas & Status Santri):

- place_santri/create_santri/patch_santri menulis ke RiwayatPenempatanSantri,
  menutup baris terbuka sebelumnya sebelum membuka yang baru.
- list_santri default hanya menampilkan santri status="aktif".
- set_status_santri menandai lulus/keluar/pindah tanpa menghapus baris, dan
  menutup riwayat kelas yang masih terbuka.
- kenaikan_kelas_massal memindahkan sebagian santri dan meluluskan sisanya
  dalam satu batch, tanpa menyentuh santri yang tidak disebut.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.madrasah.modules.madrasah.application import services
from tenants.madrasah.modules.madrasah.application.schemas import (
    KenaikanKelasItem,
    KenaikanKelasRequest,
    PlacementIn,
    SantriIn,
    SantriStatusIn,
)
from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase
from tenants.madrasah.modules.madrasah.infrastructure.models import RombelMadrasah


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MadrasahBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    await engine.dispose()


async def _buat_rombel(session, nama: str) -> str:
    row = RombelMadrasah(nama=nama)
    session.add(row)
    await session.flush()
    return row.id


@pytest.mark.asyncio
async def test_create_santri_with_rombel_opens_riwayat_row(session):
    rombel_id = await _buat_rombel(session, "Jilid 1 A")
    santri = await services.create_santri(session, SantriIn(nama="Santri A", rombel_id=rombel_id))

    riwayat = await services.riwayat_kelas_santri(session, santri.id)
    assert len(riwayat) == 1
    assert riwayat[0]["rombel_id"] == rombel_id
    assert riwayat[0]["tanggal_keluar"] is None


@pytest.mark.asyncio
async def test_place_santri_closes_previous_open_row_and_opens_new_one(session):
    rombel_a = await _buat_rombel(session, "Jilid 1 A")
    rombel_b = await _buat_rombel(session, "Jilid 1 B")
    santri = await services.create_santri(session, SantriIn(nama="Santri B", rombel_id=rombel_a))

    await services.place_santri(session, PlacementIn(santri_id=santri.id, rombel_id=rombel_b))

    riwayat = await services.riwayat_kelas_santri(session, santri.id)
    assert len(riwayat) == 2
    open_rows = [r for r in riwayat if r["tanggal_keluar"] is None]
    assert len(open_rows) == 1
    assert open_rows[0]["rombel_id"] == rombel_b


@pytest.mark.asyncio
async def test_list_santri_defaults_to_active_only(session):
    aktif = await services.create_santri(session, SantriIn(nama="Aktif"))
    lulus = await services.create_santri(session, SantriIn(nama="Lulus"))
    await services.set_status_santri(session, lulus.id, SantriStatusIn(status="lulus"))

    rows = await services.list_santri(session)
    ids = {r.id for r in rows}
    assert aktif.id in ids
    assert lulus.id not in ids

    semua = await services.list_santri(session, status="semua")
    assert {r.id for r in semua} == {aktif.id, lulus.id}


@pytest.mark.asyncio
async def test_set_status_lulus_closes_open_riwayat_without_deleting_santri(session):
    rombel_id = await _buat_rombel(session, "Jilid 6")
    santri = await services.create_santri(session, SantriIn(nama="Calon Lulus", rombel_id=rombel_id))

    row = await services.set_status_santri(session, santri.id, SantriStatusIn(status="lulus"))
    assert row.status == "lulus"
    assert row.tanggal_status is not None

    riwayat = await services.riwayat_kelas_santri(session, santri.id)
    assert riwayat[0]["tanggal_keluar"] is not None

    # Row still exists and is readable directly (not deleted).
    fetched = await session.get(type(row), santri.id)
    assert fetched is not None


@pytest.mark.asyncio
async def test_kenaikan_kelas_massal_moves_some_and_graduates_others(session):
    rombel_asal = await _buat_rombel(session, "Jilid 1")
    rombel_tujuan = await _buat_rombel(session, "Jilid 2")
    naik = await services.create_santri(session, SantriIn(nama="Naik Kelas", rombel_id=rombel_asal))
    lulus = await services.create_santri(session, SantriIn(nama="Lulus Batch", rombel_id=rombel_asal))
    tidak_disebut = await services.create_santri(session, SantriIn(nama="Tidak Disebut", rombel_id=rombel_asal))

    hasil = await services.kenaikan_kelas_massal(
        session,
        KenaikanKelasRequest(
            items=[
                KenaikanKelasItem(santri_id=naik.id, rombel_tujuan_id=rombel_tujuan),
                KenaikanKelasItem(santri_id=lulus.id, rombel_tujuan_id=None),
            ]
        ),
    )
    assert hasil["dipindah"] == [naik.id]
    assert hasil["diluluskan"] == [lulus.id]

    await session.refresh(naik)
    await session.refresh(lulus)
    await session.refresh(tidak_disebut)

    assert naik.rombel_id == rombel_tujuan
    assert naik.status == "aktif"
    assert lulus.status == "lulus"
    assert tidak_disebut.rombel_id == rombel_asal
    assert tidak_disebut.status == "aktif"


@pytest.mark.asyncio
async def test_kenaikan_kelas_massal_rejects_unknown_santri(session):
    with pytest.raises(services.MadrasahNotFoundError):
        await services.kenaikan_kelas_massal(
            session, KenaikanKelasRequest(items=[KenaikanKelasItem(santri_id="tidak-ada", rombel_tujuan_id=None)])
        )


@pytest.mark.asyncio
async def test_kenaikan_kelas_massal_rejects_unknown_target_rombel(session):
    santri = await services.create_santri(session, SantriIn(nama="X"))
    with pytest.raises(services.MadrasahNotFoundError):
        await services.kenaikan_kelas_massal(
            session,
            KenaikanKelasRequest(items=[KenaikanKelasItem(santri_id=santri.id, rombel_tujuan_id="tidak-ada")]),
        )
