"""Self-service "Profil Saya": ganti nama & password sendiri, beda dari
patch_guru (admin mengedit akun ORANG LAIN tanpa perlu password lama).
"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.madrasah.modules.madrasah.application import services
from tenants.madrasah.modules.madrasah.application.schemas import ProfilPatch
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


async def _make_user(session, **kwargs):
    user = UserMadrasah(
        nama=kwargs.get("nama", "Guru Madrasah"),
        no_hp="081200000099",
        password_hash=hash_password(kwargs.get("password", "password123")),
        role="wali_kelas",
    )
    session.add(user)
    await session.flush()
    return user


@pytest.mark.asyncio
async def test_update_nama_only(session):
    user = await _make_user(session)
    old_sv = user.session_version

    updated = await services.update_profil_saya(session, user, ProfilPatch(nama="Nama Baru"))
    assert updated.nama == "Nama Baru"
    # Ganti nama saja tidak boleh mencabut sesi/token lain yang sedang aktif.
    assert updated.session_version == old_sv


@pytest.mark.asyncio
async def test_change_password_requires_correct_current_password(session):
    user = await _make_user(session, password="password123")

    with pytest.raises(services.MadrasahAuthError):
        await services.update_profil_saya(
            session, user, ProfilPatch(current_password="salah", new_password="passwordBaru123")
        )
    # Password lama tidak berubah kalau verifikasi gagal.
    assert verify_password("password123", user.password_hash)


@pytest.mark.asyncio
async def test_change_password_success_bumps_session_version(session):
    user = await _make_user(session, password="password123")
    old_sv = user.session_version

    updated = await services.update_profil_saya(
        session, user, ProfilPatch(current_password="password123", new_password="passwordBaru123")
    )
    assert verify_password("passwordBaru123", updated.password_hash)
    assert updated.session_version == old_sv + 1


@pytest.mark.asyncio
async def test_new_password_without_current_password_raises(session):
    user = await _make_user(session, password="password123")

    with pytest.raises(services.MadrasahAuthError):
        await services.update_profil_saya(session, user, ProfilPatch(new_password="passwordBaru123"))
