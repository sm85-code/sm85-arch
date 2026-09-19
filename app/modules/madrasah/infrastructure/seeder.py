"""Idempotent starter rows for the isolated madrasah Neon database."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.madrasah.infrastructure.database import MadrasahBase, engine
from app.modules.madrasah.infrastructure.models import KelasMadrasah, SantriMadrasah, UserMadrasah
from shared.security import hash_password

GURU_HP = "082315394967"
WALI_HP = "081234567890"
DEFAULT_PASSWORD = "password123"
KELAS_NAMA = "Jilid 1 B"


async def seed_madrasah(session: AsyncSession) -> dict[str, str]:
    if engine is None:
        raise RuntimeError("DATABASE_URL_MADRASAH is not configured")
    async with engine.begin() as conn:
        await conn.run_sync(MadrasahBase.metadata.create_all)

    guru = (
        await session.execute(select(UserMadrasah).where(UserMadrasah.no_hp == GURU_HP))
    ).scalar_one_or_none()
    if not guru:
        guru = UserMadrasah(
            nama="SITI MUKAROMAH MASKUR",
            no_hp=GURU_HP,
            password_hash=hash_password(DEFAULT_PASSWORD),
            role="guru",
        )
        session.add(guru)
        await session.flush()

    wali = (
        await session.execute(select(UserMadrasah).where(UserMadrasah.no_hp == WALI_HP))
    ).scalar_one_or_none()
    if not wali:
        wali = UserMadrasah(
            nama="Wali Faqih",
            no_hp=WALI_HP,
            password_hash=hash_password(DEFAULT_PASSWORD),
            role="wali_santri",
        )
        session.add(wali)
        await session.flush()

    kelas = (
        await session.execute(select(KelasMadrasah).where(KelasMadrasah.nama_kelas == KELAS_NAMA))
    ).scalar_one_or_none()
    if not kelas:
        kelas = KelasMadrasah(nama_kelas=KELAS_NAMA)
        session.add(kelas)
        await session.flush()

    santri = (
        await session.execute(
            select(SantriMadrasah).where(SantriMadrasah.nama == "MUHAMMAD FAQIH AL MURTADLO")
        )
    ).scalar_one_or_none()
    if not santri:
        santri = SantriMadrasah(
            nama="MUHAMMAD FAQIH AL MURTADLO",
            kelas_id=kelas.id,
            orang_tua_id=wali.id,
        )
        session.add(santri)
        await session.flush()

    return {
        "guru_id": guru.id,
        "wali_id": wali.id,
        "kelas_id": kelas.id,
        "santri_id": santri.id,
    }
