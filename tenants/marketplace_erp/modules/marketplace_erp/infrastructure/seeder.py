"""Idempotent schema creation + starter owner account for the isolated
marketplace_erp database. Triggered by GET /api/marketplace-erp/seed-now
(gated -- see marketplace_erp_router.authorize_marketplace_erp_seed).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.security import hash_password
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase, engine
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    DEFAULT_GUDANG_KODE,
    Gudang,
    UserMarketplaceErp,
)

# EmailStr (pydantic) rejects RFC 2606 reserved TLDs (.test/.example/...)
# as "special-use or reserved" -- use a non-reserved placeholder domain so
# this default account can actually log in through the /auth/login schema.
OWNER_EMAIL = "owner@marketplace-erp.internal"
DEFAULT_PASSWORD = "password123"


async def _ensure_owner(session: AsyncSession) -> UserMarketplaceErp:
    owner = (
        await session.execute(select(UserMarketplaceErp).where(UserMarketplaceErp.email == OWNER_EMAIL))
    ).scalar_one_or_none()
    if owner:
        return owner
    owner = UserMarketplaceErp(
        nama="Owner",
        email=OWNER_EMAIL,
        password_hash=hash_password(DEFAULT_PASSWORD),
        role="owner",
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


async def seed_marketplace_erp(session: AsyncSession) -> dict[str, str]:
    if engine is None:
        raise RuntimeError("DATABASE_URL_MARKETPLACE_ERP is not configured")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)

    owner = await _ensure_owner(session)
    gudang = await _ensure_default_gudang(session)
    await session.commit()
    return {
        "owner_id": owner.id,
        "owner_email": owner.email,
        "gudang_default_id": gudang.id,
        "gudang_default_kode": gudang.kode,
    }
