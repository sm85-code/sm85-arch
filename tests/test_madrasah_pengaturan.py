"""Exercises PengaturanSekolah (school branding settings) against a real
(SQLite, in-memory) async session -- this feature exists so the product can
be sold to other madrasah without hardcoding one school's name/logo in
source code.
"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.madrasah.modules.madrasah.application import services
from tenants.madrasah.modules.madrasah.application.schemas import PengaturanPatch
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
async def test_get_pengaturan_creates_default_row_when_missing(session):
    row = await services.get_pengaturan(session)
    assert row.nama_sekolah == "Madrasah Diniyah"
    assert row.tagline == "Sistem Informasi Madrasah"
    assert row.logo_url is None

    # Idempotent: memanggil lagi tidak membuat baris kedua.
    row_again = await services.get_pengaturan(session)
    assert row_again.id == row.id


@pytest.mark.asyncio
async def test_update_pengaturan_patches_only_given_fields(session):
    await services.get_pengaturan(session)

    updated = await services.update_pengaturan(
        session, PengaturanPatch(nama_sekolah="Madrasah Bani Husen", logo_url="https://example.com/logo.png")
    )
    assert updated.nama_sekolah == "Madrasah Bani Husen"
    assert updated.logo_url == "https://example.com/logo.png"
    # tagline tidak dikirim di patch -- harus tetap nilai default, bukan None.
    assert updated.tagline == "Sistem Informasi Madrasah"

    out = services.pengaturan_out(updated)
    assert out == {
        "nama_sekolah": "Madrasah Bani Husen",
        "tagline": "Sistem Informasi Madrasah",
        "logo_url": "https://example.com/logo.png",
        "alamat": "",
    }
