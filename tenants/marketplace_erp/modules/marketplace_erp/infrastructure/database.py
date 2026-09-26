"""Isolated async engine for the marketplace_erp tenant database.

Brand-new tenant, independent from tenants/toko -- own Postgres instance,
own DeclarativeBase, zero shared tables. Same pattern as
tenants/toko/modules/toko/infrastructure/database.py and
tenants/madrasah/modules/madrasah/infrastructure/database.py.
"""
from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


def _marketplace_erp_url() -> str:
    url = os.getenv("DATABASE_URL_MARKETPLACE_ERP") or ""
    if not url:
        raise RuntimeError("DATABASE_URL_MARKETPLACE_ERP must be set for the marketplace_erp module")

    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme in {"postgresql", "postgres"}:
        scheme = "postgresql+asyncpg"
    elif scheme == "postgresql+asyncpg":
        pass
    else:
        raise RuntimeError("DATABASE_URL_MARKETPLACE_ERP must use PostgreSQL")

    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in {"channel_binding", "sslmode", "ssl"}
    ]
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


DATABASE_URL_MARKETPLACE_ERP = os.getenv("DATABASE_URL_MARKETPLACE_ERP")

_connect_args: dict = {"ssl": True}

engine = None
SessionLocal = None

if DATABASE_URL_MARKETPLACE_ERP:
    engine = create_async_engine(
        _marketplace_erp_url(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        connect_args=_connect_args,
    )
    SessionLocal = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


class MarketplaceErpBase(DeclarativeBase):
    """Declarative base isolated from SIABUMDES / madrasah / toko bases."""


async def get_db_marketplace_erp() -> AsyncGenerator[AsyncSession, None]:
    if SessionLocal is None:
        raise RuntimeError("DATABASE_URL_MARKETPLACE_ERP is not configured")
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
