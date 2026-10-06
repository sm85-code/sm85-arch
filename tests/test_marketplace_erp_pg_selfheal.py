"""Postgres-only: marketplace_erp self-heal adds mpe_users.must_change_password
to a database created before the column existed (create_all never ALTERs).

Skips when TEST_DATABASE_URL is unset (unit job); CI's integration-pg job sets
it to a throwaway Postgres service. Runs inside a throwaway schema so it never
touches real tables.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL tidak tersedia",
)


def _asyncpg_url() -> str:
    url = os.getenv("TEST_DATABASE_URL") or ""
    for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):].split("?", 1)[0]
    return url


@pytest.mark.asyncio
async def test_self_heal_adds_must_change_password_column():
    from sqlalchemy.ext.asyncio import create_async_engine

    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import seeder
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase

    schema = f"mpe_selfheal_{uuid.uuid4().hex[:8]}"
    engine = create_async_engine(_asyncpg_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            # Legacy (pre-fix) shape of mpe_users, with an existing row.
            await conn.execute(text(
                "CREATE TABLE mpe_users (id VARCHAR(64) PRIMARY KEY, nama VARCHAR(255) NOT NULL, "
                "email VARCHAR(255) NOT NULL UNIQUE, password_hash VARCHAR(255) NOT NULL, "
                "role VARCHAR(32) NOT NULL, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ)"
            ))
            await conn.execute(text(
                "INSERT INTO mpe_users (id, nama, email, password_hash, role) "
                "VALUES ('u1', 'Owner', 'o@example.com', 'x', 'owner')"
            ))
            await conn.run_sync(MarketplaceErpBase.metadata.create_all)
            await seeder._self_heal_columns(conn)
            await seeder._self_heal_columns(conn)  # idempotent
            cols = {
                row[0] for row in await conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = 'mpe_users'"
                ), {"s": schema})
            }
            assert {"must_change_password", "session_version"} <= cols
            version = (await conn.execute(text("SELECT session_version FROM mpe_users WHERE id='u1'"))).scalar_one()
            assert version == 0
            flag = (await conn.execute(text("SELECT must_change_password FROM mpe_users WHERE id='u1'"))).scalar_one()
            assert flag is False
    except (OperationalError, InterfaceError, OSError, TimeoutError) as exc:
        pytest.skip(f"PostgreSQL integration unavailable: {exc}")
    except DBAPIError as exc:
        if "connect" in str(exc).lower():
            pytest.skip(f"PostgreSQL integration unavailable: {exc}")
        raise
    finally:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        except Exception:
            pass
        await engine.dispose()
