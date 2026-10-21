"""Use an ephemeral token-encryption key for isolated test databases only."""
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def isolated_marketplace_token_key(monkeypatch):
    monkeypatch.setenv("SHOPEE_TOKEN_ENCRYPTION_KEYS", Fernet.generate_key().decode())


@pytest_asyncio.fixture
async def sesi_peran():
    """An in-memory database holding only the built-in roles, for tests that call endpoints as a user without data access.
    The role check reads the role tables; every other table stays empty, so a handler that ran would find nothing."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from tenants.marketplace_erp.modules.marketplace_erp.application import akses
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        await akses.pastikan_bawaan(s)
        await s.commit()
        yield s
    await engine.dispose()
