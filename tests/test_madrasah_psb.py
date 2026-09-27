"""Kegiatan (public landing content) dan Pendaftaran (PSB form) -- baru,
tidak menyentuh entitas madrasah lain.
"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.madrasah.modules.madrasah.application import services
from tenants.madrasah.modules.madrasah.application.schemas import (
    KegiatanIn,
    PendaftaranIn,
    PendaftaranPatch,
    PengaturanPatch,
)
from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase


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
async def test_kegiatan_crud(session):
    row = await services.create_kegiatan(session, KegiatanIn(judul="Tahfidz", deskripsi="Hafalan Al-Qur'an", urutan=1))
    assert row.judul == "Tahfidz"

    rows = await services.list_kegiatan(session)
    assert [r.judul for r in rows] == ["Tahfidz"]

    await services.delete_kegiatan(session, row.id)
    assert await services.list_kegiatan(session) == []


@pytest.mark.asyncio
async def test_delete_kegiatan_missing_raises(session):
    with pytest.raises(services.MadrasahNotFoundError):
        await services.delete_kegiatan(session, "tidak-ada")


@pytest.mark.asyncio
async def test_pendaftaran_create_defaults_status_baru(session):
    payload = PendaftaranIn(
        nama_calon="Ahmad",
        nama_orang_tua="Budi",
        no_hp="081234567890",
        alamat="Jl. Contoh No. 1",
    )
    row = await services.create_pendaftaran(session, payload)
    assert row.status == "baru"

    rows = await services.list_pendaftaran(session)
    assert len(rows) == 1
    assert rows[0].nama_calon == "Ahmad"


@pytest.mark.asyncio
async def test_patch_pendaftaran_updates_status(session):
    row = await services.create_pendaftaran(
        session,
        PendaftaranIn(nama_calon="Ahmad", nama_orang_tua="Budi", no_hp="081234567890"),
    )
    updated = await services.patch_pendaftaran(session, row.id, PendaftaranPatch(status="diterima"))
    assert updated.status == "diterima"


@pytest.mark.asyncio
async def test_patch_pendaftaran_missing_raises(session):
    with pytest.raises(services.MadrasahNotFoundError):
        await services.patch_pendaftaran(session, "tidak-ada", PendaftaranPatch(status="diterima"))


@pytest.mark.asyncio
async def test_pengaturan_patch_info_psb_and_kontak(session):
    row = await services.update_pengaturan(session, PengaturanPatch(info_psb="Syarat: fotokopi KK.", kontak_psb="0812xxxx"))
    assert row.info_psb == "Syarat: fotokopi KK."
    assert row.kontak_psb == "0812xxxx"

    out = services.pengaturan_out(row)
    assert out["info_psb"] == "Syarat: fotokopi KK."
    assert out["kontak_psb"] == "0812xxxx"
