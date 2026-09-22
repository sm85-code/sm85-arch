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
from tenants.madrasah.modules.madrasah.infrastructure.models import PengaturanSekolah


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MadrasahBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def session_without_pengaturan_table():
    """Simulates a database deployed before this table existed -- every
    table EXCEPT madrasah_pengaturan gets created, so get_pengaturan() has
    to self-heal instead of hitting a real database elsewhere."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    other_tables = [t for t in MadrasahBase.metadata.sorted_tables if t.name != PengaturanSekolah.__tablename__]
    async with engine.begin() as conn:
        await conn.run_sync(MadrasahBase.metadata.create_all, tables=other_tables)
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
async def test_get_pengaturan_self_heals_when_table_is_missing(session_without_pengaturan_table):
    """GET /pengaturan is public (Login.jsx/Landing.jsx call it before
    anyone logs in), so on a database deployed before this table existed,
    it must create the table itself instead of 500ing for every visitor
    until an admin remembers to hit /seed-now."""
    row = await services.get_pengaturan(session_without_pengaturan_table)
    assert row.nama_sekolah == "Madrasah Diniyah"

    # Panggilan berikutnya harus idempotent (tidak buat baris kedua).
    row_again = await services.get_pengaturan(session_without_pengaturan_table)
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
