"""Real PostgreSQL migration and concurrency checks in a disposable schema."""
import asyncio
import hashlib
import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import oauth_security, token_crypto
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.store.modules.store.infrastructure import database as store_database, seeder as store_seeder
from tenants.store.modules.store.infrastructure.database import StoreBase

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL tidak tersedia")


@pytest.mark.asyncio
async def test_existing_store_accounts_and_plaintext_tokens_migrate_and_nonce_consumption_is_atomic(monkeypatch):
    url = os.environ["TEST_DATABASE_URL"].replace("postgresql://", "postgresql+asyncpg://", 1)
    schema = "stage2_" + uuid.uuid4().hex[:12]
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(StoreBase.metadata.create_all)
            await conn.run_sync(MarketplaceErpBase.metadata.create_all)
            for table in ("store_admin_users", "store_buyer_users"):
                await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN session_version"))
                columns, values = (", role", ", 'admin'") if table == "store_admin_users" else ("", "")
                await conn.execute(text(f"INSERT INTO {table} (id, nama, email, password_hash, created_at{columns}) VALUES ('existing', 'Existing', 'a@example.com', 'hash', NOW(){values})"))
            await conn.execute(text("INSERT INTO mpe_akun_marketplace (id, platform, nama_toko, status, access_token, refresh_token, created_at, updated_at) VALUES ('shop', 'shopee', 'T', 'terhubung', 'legacy-access', 'legacy-refresh', NOW(), NOW())"))
            assert await token_crypto.migrate_token_storage(conn) == 1
            raw = (await conn.execute(text("SELECT access_token FROM mpe_akun_marketplace"))).scalar_one()
            assert raw.startswith(token_crypto.PREFIX) and token_crypto.decrypt_token(raw) == "legacy-access"
        monkeypatch.setattr(store_database, "engine", engine)
        monkeypatch.setattr(store_database, "SessionLocal", factory)
        await store_seeder.ensure_store_schema()
        await store_seeder.ensure_store_schema()
        async with engine.connect() as conn:
            for table in ("store_admin_users", "store_buyer_users"):
                assert (await conn.execute(text(f"SELECT session_version FROM {table} WHERE id='existing'"))).scalar_one() == 0
        user = SimpleNamespace(id="owner", session_version=0)
        async with factory() as session:
            nonce = await oauth_security.issue_nonce(session, "shop", user)
            await session.commit()

        async def consume():
            async with factory() as session:
                try:
                    await oauth_security.consume_nonce(session, "shop", nonce, user)
                    return "accepted"
                except HTTPException:
                    return "rejected"

        results = await asyncio.gather(consume(), consume())
        assert sorted(results) == ["accepted", "rejected"]
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT consumed FROM mpe_shopee_oauth_requests WHERE nonce_hash=:h"), {"h": hashlib.sha256(nonce.encode()).hexdigest()})).scalar_one()
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
