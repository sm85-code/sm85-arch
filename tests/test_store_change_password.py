"""store admin: changing one's own password."""
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shared.security import hash_password, verify_password
from tenants.store.modules.store.application import services
from tenants.store.modules.store.application.schemas import ChangePasswordIn
from tenants.store.modules.store.infrastructure.models import ROLE_OWNER, AdminStore

from tenants.store.modules.store.infrastructure.database import StoreBase


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(StoreBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _owner(session):
    user = AdminStore(nama="Owner", email="o@store.internal", password_hash=hash_password("lama-12345"), role=ROLE_OWNER)
    session.add(user)
    await session.flush()
    return user


@pytest.mark.asyncio
async def test_change_password_updates_hash(session):
    user = await _owner(session)
    await services.change_admin_password(session, user, "lama-12345", "baru-67890")
    assert verify_password("baru-67890", user.password_hash)
    assert not verify_password("lama-12345", user.password_hash)


@pytest.mark.asyncio
async def test_change_password_rejects_wrong_current(session):
    user = await _owner(session)
    with pytest.raises(HTTPException) as exc:
        await services.change_admin_password(session, user, "salah-salah", "baru-67890")
    assert exc.value.status_code == 400
    assert verify_password("lama-12345", user.password_hash)


@pytest.mark.asyncio
async def test_change_password_rejects_same_password(session):
    user = await _owner(session)
    with pytest.raises(HTTPException) as exc:
        await services.change_admin_password(session, user, "lama-12345", "lama-12345")
    assert exc.value.status_code == 400


def test_new_password_min_length():
    with pytest.raises(ValueError):
        ChangePasswordIn(current_password="x", new_password="pendek")
