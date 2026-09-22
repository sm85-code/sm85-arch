"""Second async engine for the madrasah Neon database.

Does not use shared.database / DATABASE_URL (BUMDes).
"""
from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


def _madrasah_url() -> str:
    url = os.getenv("DATABASE_URL_MADRASAH") or ""
    if not url:
        raise RuntimeError("DATABASE_URL_MADRASAH must be set for the madrasah module")

    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme in {"postgresql", "postgres"}:
        scheme = "postgresql+asyncpg"
    elif scheme == "postgresql+asyncpg":
        pass
    else:
        raise RuntimeError("DATABASE_URL_MADRASAH must use PostgreSQL")

    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in {"channel_binding", "sslmode", "ssl"}
    ]
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


DATABASE_URL_MADRASAH = os.getenv("DATABASE_URL_MADRASAH")

_connect_args: dict = {"ssl": True}

engine = None
SessionLocal = None

if DATABASE_URL_MADRASAH:
    engine = create_async_engine(
        _madrasah_url(),
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


class MadrasahBase(DeclarativeBase):
    """Declarative base isolated from SIABUMDES Base."""


async def get_db_madrasah() -> AsyncGenerator[AsyncSession, None]:
    if SessionLocal is None:
        raise RuntimeError("DATABASE_URL_MADRASAH is not configured")
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
