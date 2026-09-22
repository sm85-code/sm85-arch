"""Idempotent starter rows for the isolated toko database."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.security import hash_password
from tenants.toko.modules.toko.infrastructure.database import TokoBase, engine
from tenants.toko.modules.toko.infrastructure.models import UserToko

OWNER_EMAIL = "owner@toko.test"
ADMIN_EMAIL = "admin@toko.test"
DEFAULT_PASSWORD = "password123"


async def _ensure_user(session: AsyncSession, *, email: str, nama: str, role: str) -> UserToko:
    user = (await session.execute(select(UserToko).where(UserToko.email == email))).scalar_one_or_none()
    if not user:
        user = UserToko(nama=nama, email=email, password_hash=hash_password(DEFAULT_PASSWORD), role=role)
        session.add(user)
        await session.flush()
    return user


async def seed_toko(session: AsyncSession) -> dict[str, str]:
    if engine is None:
        raise RuntimeError("DATABASE_URL_TOKO is not configured")
    async with engine.begin() as conn:
        await conn.run_sync(TokoBase.metadata.create_all)

    owner = await _ensure_user(session, email=OWNER_EMAIL, nama="Pemilik Toko", role="owner")
    admin = await _ensure_user(session, email=ADMIN_EMAIL, nama="Admin Toko", role="admin_toko")

    return {"owner_id": owner.id, "admin_id": admin.id}
