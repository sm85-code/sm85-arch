"""Idempotent schema creation + starter owner account for the isolated
marketplace_erp database. Triggered by GET /api/marketplace-erp/seed-now
(gated -- see marketplace_erp_router.authorize_marketplace_erp_seed) and,
for the column self-heal only, by main.py's lifespan via
ensure_marketplace_erp_schema().
"""
from __future__ import annotations

import logging

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.security import hash_password, verify_password
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import database as mpe_database
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    DEFAULT_GUDANG_KODE,
    Gudang,
    UserMarketplaceErp,
)

logger = logging.getLogger(__name__)

# EmailStr (pydantic) rejects RFC 2606 reserved TLDs (.test/.example/...)
# as "special-use or reserved" -- use a non-reserved placeholder domain so
# this default account can actually log in through the /auth/login schema.
OWNER_EMAIL = "owner@marketplace-erp.internal"
DEFAULT_PASSWORD = "password123"

# Columns added after the table first shipped. create_all() never ALTERs an
# existing table, so Postgres databases created before these columns existed
# are self-healed here. Postgres-only syntax -- skipped on SQLite (tests
# build the schema fresh from metadata anyway).
_SELF_HEAL_COLUMNS = (
    "ALTER TABLE IF EXISTS mpe_users "
    "ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE",
    # Tahap 3: manual pengiriman (courier/AWB) fields on the orders inbox.
    "ALTER TABLE IF EXISTS mpe_pesanan ADD COLUMN IF NOT EXISTS kurir VARCHAR(64) NULL",
    "ALTER TABLE IF EXISTS mpe_pesanan ADD COLUMN IF NOT EXISTS nomor_resi VARCHAR(128) NULL",
    "ALTER TABLE IF EXISTS mpe_pesanan ADD COLUMN IF NOT EXISTS tanggal_kirim TIMESTAMPTZ NULL",
    # Product weight/size and lead time.
    "ALTER TABLE IF EXISTS mpe_produk ADD COLUMN IF NOT EXISTS berat_gram INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE IF EXISTS mpe_produk ADD COLUMN IF NOT EXISTS panjang_cm NUMERIC(8,1) NOT NULL DEFAULT 0",
    "ALTER TABLE IF EXISTS mpe_produk ADD COLUMN IF NOT EXISTS lebar_cm NUMERIC(8,1) NOT NULL DEFAULT 0",
    "ALTER TABLE IF EXISTS mpe_produk ADD COLUMN IF NOT EXISTS tinggi_cm NUMERIC(8,1) NOT NULL DEFAULT 0",
    "ALTER TABLE IF EXISTS mpe_produk ADD COLUMN IF NOT EXISTS preorder BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE IF EXISTS mpe_produk ADD COLUMN IF NOT EXISTS hari_proses INTEGER NOT NULL DEFAULT 2",
)


async def _self_heal_columns(conn) -> None:
    if conn.dialect.name != "postgresql":
        return
    for stmt in _SELF_HEAL_COLUMNS:
        await conn.execute(text(stmt))


async def _flag_default_password_owner(session: AsyncSession) -> bool:
    """Mark the seeded owner as must_change_password while it still uses
    DEFAULT_PASSWORD. Returns True when a flag was newly set."""
    owner = (
        await session.execute(select(UserMarketplaceErp).where(UserMarketplaceErp.email == OWNER_EMAIL))
    ).scalar_one_or_none()
    if owner is None or owner.must_change_password:
        return False
    if verify_password(DEFAULT_PASSWORD, owner.password_hash):
        owner.must_change_password = True
        await session.flush()
        return True
    return False


async def _ensure_owner(session: AsyncSession) -> UserMarketplaceErp:
    owner = (
        await session.execute(select(UserMarketplaceErp).where(UserMarketplaceErp.email == OWNER_EMAIL))
    ).scalar_one_or_none()
    if owner:
        await _flag_default_password_owner(session)
        return owner
    owner = UserMarketplaceErp(
        nama="Owner",
        email=OWNER_EMAIL,
        password_hash=hash_password(DEFAULT_PASSWORD),
        role="owner",
        must_change_password=True,
    )
    session.add(owner)
    await session.flush()
    return owner


async def _ensure_default_gudang(session: AsyncSession) -> Gudang:
    gudang = (
        await session.execute(select(Gudang).where(Gudang.kode == DEFAULT_GUDANG_KODE))
    ).scalar_one_or_none()
    if gudang:
        return gudang
    gudang = Gudang(kode=DEFAULT_GUDANG_KODE, nama="Gudang Utama")
    session.add(gudang)
    await session.flush()
    return gudang


async def _create_schema(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
        await _self_heal_columns(conn)


async def ensure_marketplace_erp_schema() -> None:
    """Startup hook (main.py lifespan): create missing tables, add columns
    introduced after first deploy, and flag a still-default-password owner.

    No-op when DATABASE_URL_MARKETPLACE_ERP is not configured.
    """
    engine = mpe_database.engine
    if engine is None or mpe_database.SessionLocal is None:
        return
    await _create_schema(engine)
    async with mpe_database.SessionLocal() as session:
        if await _flag_default_password_owner(session):
            logger.warning(
                "marketplace_erp: seeded owner %s still uses the default password -- "
                "flagged must_change_password", OWNER_EMAIL,
            )
        await session.commit()


async def seed_marketplace_erp(session: AsyncSession) -> dict[str, str]:
    engine = mpe_database.engine
    if engine is None:
        raise RuntimeError("DATABASE_URL_MARKETPLACE_ERP is not configured")
    await _create_schema(engine)

    owner = await _ensure_owner(session)
    gudang = await _ensure_default_gudang(session)
    await session.commit()
    return {
        "owner_id": owner.id,
        "owner_email": owner.email,
        "gudang_default_id": gudang.id,
        "gudang_default_kode": gudang.kode,
    }
