"""Idempotent schema creation + first owner account for the store database.

Triggered by GET /api/store/admin/seed-now (gated, see store_admin_router).
The owner's password comes from STORE_SEED_OWNER_PASSWORD -- there is no
default password in code, so a forgotten env var can never leave a
well-known login behind.
"""
from __future__ import annotations

import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.security import hash_password
from tenants.store.modules.store.infrastructure.database import StoreBase, engine
from tenants.store.modules.store.infrastructure.models import ROLE_OWNER, AdminStore

# EmailStr rejects RFC 2606 reserved TLDs (.test/.example/...); ".internal"
# is accepted, so the placeholder owner can actually log in.
DEFAULT_OWNER_EMAIL = "owner@store.internal"


async def seed_store(session: AsyncSession) -> dict[str, str]:
    if engine is None:
        raise RuntimeError("DATABASE_URL_STORE is not configured")
    password = (os.getenv("STORE_SEED_OWNER_PASSWORD") or "").strip()
    if len(password) < 8:
        raise RuntimeError("STORE_SEED_OWNER_PASSWORD must be set (min 8 characters) to seed the store owner")

    async with engine.begin() as conn:
        await conn.run_sync(StoreBase.metadata.create_all)

    email = (os.getenv("STORE_SEED_OWNER_EMAIL") or DEFAULT_OWNER_EMAIL).strip().lower()
    owner = (await session.execute(select(AdminStore).where(AdminStore.email == email))).scalar_one_or_none()
    if owner is None:
        owner = AdminStore(nama="Pemilik Toko", email=email, password_hash=hash_password(password), role=ROLE_OWNER)
        session.add(owner)
        await session.flush()
    return {"owner_id": owner.id, "owner_email": owner.email}
