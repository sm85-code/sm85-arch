"""Exercises seed_madrasah (idempotent) and reset_madrasah (destructive)
against a real (SQLite, in-memory) async session.
"""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import tenants.madrasah.modules.madrasah.infrastructure.seeder as seeder_module
from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase
from tenants.madrasah.modules.madrasah.infrastructure.models import SantriMadrasah, UserMadrasah


@pytest_asyncio.fixture
async def engine_and_session(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(seeder_module, "engine", engine)
    async with engine.begin() as conn:
        await conn.run_sync(MadrasahBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield engine, s
    await engine.dispose()


@pytest.mark.asyncio
async def test_seed_madrasah_creates_exactly_admin_and_guru(engine_and_session):
    _, session = engine_and_session
    ids = await seeder_module.seed_madrasah(session)
    assert set(ids.keys()) == {"admin_id", "guru_id"}

    users = list((await session.execute(select(UserMadrasah))).scalars())
    assert len(users) == 2
    roles = {u.role for u in users}
    assert roles == {"admin", "wali_kelas"}


@pytest.mark.asyncio
async def test_seed_madrasah_is_idempotent(engine_and_session):
    _, session = engine_and_session
    first = await seeder_module.seed_madrasah(session)
    second = await seeder_module.seed_madrasah(session)
    assert first == second
    users = list((await session.execute(select(UserMadrasah))).scalars())
    assert len(users) == 2


@pytest.mark.asyncio
async def test_reset_madrasah_wipes_existing_data_before_reseeding(engine_and_session):
    _, session = engine_and_session
    # Simulasikan database pelanggan lama yang sudah punya data (bukan cuma
    # akun default) -- santri ini, dan akun user apa pun yang ada sebelum
    # reset, harus benar-benar hilang setelahnya.
    session.add(SantriMadrasah(nama="Santri Lama"))
    session.add(UserMadrasah(nama="Akun Lama", no_hp="089900000000", password_hash="x", role="kurikulum"))
    await session.flush()

    ids = await seeder_module.reset_madrasah(session)
    assert set(ids.keys()) == {"admin_id", "guru_id"}

    santri_rows = list((await session.execute(select(SantriMadrasah))).scalars())
    assert santri_rows == []

    users = list((await session.execute(select(UserMadrasah))).scalars())
    assert len(users) == 2
    assert {u.no_hp for u in users} == {seeder_module.ADMIN_HP, seeder_module.GURU_HP}
