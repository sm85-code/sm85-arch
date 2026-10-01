"""Isolated async engine for the store (online shop) database.

Reads only DATABASE_URL_STORE and shares no engine with the other tenants --
same pattern as tenants/madrasah/modules/madrasah/infrastructure/database.py.
The admin and buyer sub-tenants share this one database.
"""
from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


def _store_url() -> str:
    url = os.getenv("DATABASE_URL_STORE") or ""
    if not url:
        raise RuntimeError("DATABASE_URL_STORE must be set for the store module")

    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme in {"postgresql", "postgres"}:
        scheme = "postgresql+asyncpg"
    elif scheme == "postgresql+asyncpg":
        pass
    else:
        raise RuntimeError("DATABASE_URL_STORE must use PostgreSQL")

    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in {"channel_binding", "sslmode", "ssl"}
    ]
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


DATABASE_URL_STORE = os.getenv("DATABASE_URL_STORE")

_connect_args: dict = {"ssl": True}

engine = None
SessionLocal = None

if DATABASE_URL_STORE:
    engine = create_async_engine(
        _store_url(),
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


class StoreBase(DeclarativeBase):
    """Declarative base isolated from the madrasah / marketplace_erp bases."""


async def get_db_store() -> AsyncGenerator[AsyncSession, None]:
    if SessionLocal is None:
        raise RuntimeError("DATABASE_URL_STORE is not configured")
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
