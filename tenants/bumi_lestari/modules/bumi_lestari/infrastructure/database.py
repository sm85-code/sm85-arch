"""Isolated async engine for the bumi_lestari tenant database (UMKM Bumi Lestari).

Own Postgres instance, own DeclarativeBase, zero shared tables. Same pattern as
tenants/marketplace_erp/modules/marketplace_erp/infrastructure/database.py.
"""
from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


def _bumi_lestari_url() -> str:
    url = os.getenv("DATABASE_URL_BUMI_LESTARI") or ""
    if not url:
        raise RuntimeError("DATABASE_URL_BUMI_LESTARI must be set for the bumi_lestari module")

    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme in {"postgresql", "postgres"}:
        scheme = "postgresql+asyncpg"
    elif scheme != "postgresql+asyncpg":
        raise RuntimeError("DATABASE_URL_BUMI_LESTARI must use PostgreSQL")

    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in {"channel_binding", "sslmode", "ssl"}
    ]
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


DATABASE_URL_BUMI_LESTARI = os.getenv("DATABASE_URL_BUMI_LESTARI")

engine = None
SessionLocal = None

if DATABASE_URL_BUMI_LESTARI:
    engine = create_async_engine(
        _bumi_lestari_url(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        connect_args={"ssl": True},
    )
    SessionLocal = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


class BumiLestariBase(DeclarativeBase):
    """Declarative base isolated from every other tenant's base."""


async def get_db_bumi_lestari() -> AsyncGenerator[AsyncSession, None]:
    if SessionLocal is None:
        raise RuntimeError("DATABASE_URL_BUMI_LESTARI is not configured")
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def kolom_tambahan_column():
    """Nilai kolom tambahan (spesifikasi 10.5): JSONB di PostgreSQL (indeks GIN), JSON di SQLite; kunci = definisi kolom."""
    from sqlalchemy import JSON, text
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.orm import mapped_column

    return mapped_column(JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict, server_default=text("'{}'"))
