"""Idempotent starter rows for the isolated madrasah Neon database.

This product is being sold to multiple madrasah, so the seed data must be
generic (no fictional school/rombel/santri baked in) -- just enough
accounts to log in and start configuring the school from the UI.
"""
from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase, engine
from tenants.madrasah.modules.madrasah.infrastructure.models import UserMadrasah
from shared.security import hash_password

ADMIN_HP = "081200000001"
GURU_HP = "081200000002"
DEFAULT_PASSWORD = "password123"


async def _ensure_user(session: AsyncSession, *, no_hp: str, nama: str, role: str) -> UserMadrasah:
    user = (await session.execute(select(UserMadrasah).where(UserMadrasah.no_hp == no_hp))).scalar_one_or_none()
    if not user:
        user = UserMadrasah(nama=nama, no_hp=no_hp, password_hash=hash_password(DEFAULT_PASSWORD), role=role)
        session.add(user)
        await session.flush()
    return user


async def ensure_madrasah_schema() -> None:
    """Idempotent schema repair, meant to be called once on every app
    startup (see main.py lifespan) -- not tied to /seed-now, since a
    customer's Neon database might never see that endpoint called again
    after initial setup. Creates any table added to this module after a
    customer's first deploy (checkfirst, safe no-op if already there) and
    adds columns added to an *existing* table since then (session_version
    on madrasah_users), which metadata.create_all alone cannot do.

    Any failure here is caught by the caller and logged, never raised --
    a stale schema must not prevent the whole app (BUMDes, Toko, etc.) from
    starting.
    """
    if engine is None:
        return
    async with engine.begin() as conn:
        # create_all membuat madrasah_tahun_ajaran & madrasah_semester untuk
        # database yang belum punya keduanya (checkfirst) -- harus terjadi
        # SEBELUM ALTER TABLE ADD COLUMN semester_id di bawah (FK-nya
        # menunjuk ke madrasah_semester.id).
        await conn.run_sync(MadrasahBase.metadata.create_all)
        await conn.execute(
            text("ALTER TABLE IF EXISTS madrasah_users ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0")
        )
        for table in (
            "madrasah_absensi",
            "madrasah_progres_hafalan",
            "madrasah_tagihan_syahriyah",
            "madrasah_jadwal",
        ):
            await conn.execute(text(f"ALTER TABLE IF EXISTS {table} ADD COLUMN IF NOT EXISTS semester_id VARCHAR(64) NULL"))
        await conn.execute(
            text("ALTER TABLE IF EXISTS madrasah_santri ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'aktif'")
        )
        await conn.execute(text("ALTER TABLE IF EXISTS madrasah_santri ADD COLUMN IF NOT EXISTS tanggal_status DATE NULL"))


async def seed_madrasah(session: AsyncSession) -> dict[str, str]:
    """Idempotent: creates the tables if missing, and the two default
    accounts (admin, guru) if they don't already exist. Safe to call on
    every deploy -- never touches or deletes existing data."""
    if engine is None:
        raise RuntimeError("DATABASE_URL_MADRASAH is not configured")
    async with engine.begin() as conn:
        await conn.run_sync(MadrasahBase.metadata.create_all)

    admin = await _ensure_user(session, no_hp=ADMIN_HP, nama="Admin Madrasah", role="admin")
    guru = await _ensure_user(session, no_hp=GURU_HP, nama="Guru Madrasah", role="wali_kelas")

    return {"admin_id": admin.id, "guru_id": guru.id}


async def reset_madrasah(session: AsyncSession) -> dict[str, str]:
    """DESTRUCTIVE: drops every madrasah_* table (all santri, rombel,
    mapel, tagihan, buku kas, everything) and recreates just the two
    default accounts. Only call this deliberately -- e.g. wiping a demo
    database back to a clean slate before handing it to a new customer --
    never as part of routine deploys (use seed_madrasah/`/seed-now` for
    that instead, which is safe and idempotent)."""
    if engine is None:
        raise RuntimeError("DATABASE_URL_MADRASAH is not configured")
    async with engine.begin() as conn:
        await conn.run_sync(MadrasahBase.metadata.drop_all)
        await conn.run_sync(MadrasahBase.metadata.create_all)

    admin = UserMadrasah(nama="Admin Madrasah", no_hp=ADMIN_HP, password_hash=hash_password(DEFAULT_PASSWORD), role="admin")
    guru = UserMadrasah(nama="Guru Madrasah", no_hp=GURU_HP, password_hash=hash_password(DEFAULT_PASSWORD), role="wali_kelas")
    session.add_all([admin, guru])
    await session.flush()

    return {"admin_id": admin.id, "guru_id": guru.id}
