"""Kelola Akun: list_semua_akun (directory lintas role) + pengaman admin
terakhir tidak boleh dihapus/di-demote lewat patch_guru/delete_guru.
"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.madrasah.modules.madrasah.application import services
from tenants.madrasah.modules.madrasah.application.schemas import GuruIn, UserPatch
from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase
from tenants.madrasah.modules.madrasah.infrastructure.models import UserMadrasah
from shared.security import hash_password, verify_password


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MadrasahBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    await engine.dispose()


async def _make_user(session, *, nama, no_hp, role):
    user = UserMadrasah(nama=nama, no_hp=no_hp, password_hash=hash_password("password123"), role=role)
    session.add(user)
    await session.flush()
    return user


@pytest.mark.asyncio
async def test_list_semua_akun_returns_every_role(session):
    await _make_user(session, nama="Admin Utama", no_hp="081200000001", role="admin")
    await _make_user(session, nama="Kepala Madrasah", no_hp="081200000002", role="kepala_sekolah")
    await _make_user(session, nama="Admin Yayasan", no_hp="081200000003", role="yayasan_admin")
    await _make_user(session, nama="Wali Santri", no_hp="081200000004", role="wali_santri")

    rows = await services.list_semua_akun(session)
    assert {r.role for r in rows} == {"admin", "kepala_sekolah", "yayasan_admin", "wali_santri"}


@pytest.mark.asyncio
async def test_cannot_delete_last_admin(session):
    admin = await _make_user(session, nama="Admin Satu-satunya", no_hp="081200000001", role="admin")

    with pytest.raises(services.MadrasahForbiddenError):
        await services.delete_guru(session, admin.id)


@pytest.mark.asyncio
async def test_can_delete_admin_when_another_admin_remains(session):
    admin1 = await _make_user(session, nama="Admin Satu", no_hp="081200000001", role="admin")
    await _make_user(session, nama="Admin Dua", no_hp="081200000002", role="admin")

    # Tidak boleh raise -- masih ada admin lain yang tersisa.
    await services.delete_guru(session, admin1.id)


@pytest.mark.asyncio
async def test_cannot_demote_last_admin(session):
    admin = await _make_user(session, nama="Admin Satu-satunya", no_hp="081200000001", role="admin")

    with pytest.raises(services.MadrasahForbiddenError):
        await services.patch_guru(session, admin.id, UserPatch(role="kepala_sekolah"))


@pytest.mark.asyncio
async def test_can_demote_admin_when_another_admin_remains(session):
    admin1 = await _make_user(session, nama="Admin Satu", no_hp="081200000001", role="admin")
    await _make_user(session, nama="Admin Dua", no_hp="081200000002", role="admin")

    updated = await services.patch_guru(session, admin1.id, UserPatch(role="kurikulum"))
    assert updated.role == "kurikulum"


@pytest.mark.asyncio
async def test_can_edit_own_no_hp_without_touching_role(session):
    admin = await _make_user(session, nama="Admin", no_hp="081200000001", role="admin")

    updated = await services.patch_guru(session, admin.id, UserPatch(no_hp="081299999999"))
    assert updated.no_hp == "081299999999"
    assert updated.role == "admin"


@pytest.mark.asyncio
async def test_create_guru_without_password_generates_random_one(session):
    row, generated = await services.create_guru(session, GuruIn(nama="Guru Tanpa Password", no_hp="081377770099"))

    assert generated is not None
    assert generated != "password123"
    assert verify_password(generated, row.password_hash)


@pytest.mark.asyncio
async def test_create_guru_with_password_does_not_generate_one(session):
    row, generated = await services.create_guru(
        session, GuruIn(nama="Guru Dengan Password", no_hp="081377770098", password="rahasia123")
    )

    assert generated is None
    assert verify_password("rahasia123", row.password_hash)
