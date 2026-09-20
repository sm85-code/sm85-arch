"""Idempotent starter rows for the isolated madrasah Neon database."""
from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.madrasah.infrastructure.database import MadrasahBase, engine
from app.modules.madrasah.infrastructure.models import (
    KelasMadrasah,
    MapelMadrasah,
    MateriTarget,
    RombelMadrasah,
    SantriMadrasah,
    TingkatMadrasah,
    UserMadrasah,
)
from shared.security import hash_password

GURU_HP = "082315394967"
WALI_HP = "081234567890"
DEFAULT_PASSWORD = "password123"
ROMBEL_NAMA = "Jilid 1 B"
TINGKAT_NAMA = "Jilid 1"


async def _ensure_columns(conn) -> None:
    await conn.execute(text("ALTER TABLE IF EXISTS madrasah_santri ADD COLUMN IF NOT EXISTS rombel_id VARCHAR(64) NULL"))
    await conn.execute(text("ALTER TABLE IF EXISTS madrasah_tagihan_syahriyah ADD COLUMN IF NOT EXISTS dibayar_pada TIMESTAMPTZ NULL"))
    await conn.execute(text("ALTER TABLE IF EXISTS madrasah_progres_hafalan ADD COLUMN IF NOT EXISTS mapel_id VARCHAR(64) NULL"))
    await conn.execute(text("ALTER TABLE IF EXISTS madrasah_progres_hafalan ADD COLUMN IF NOT EXISTS materi_id VARCHAR(64) NULL"))


async def seed_madrasah(session: AsyncSession) -> dict[str, str]:
    if engine is None:
        raise RuntimeError("DATABASE_URL_MADRASAH is not configured")
    async with engine.begin() as conn:
        await conn.run_sync(MadrasahBase.metadata.create_all)
        await _ensure_columns(conn)

    guru = (await session.execute(select(UserMadrasah).where(UserMadrasah.no_hp == GURU_HP))).scalar_one_or_none()
    if not guru:
        guru = UserMadrasah(
            nama="SITI MUKAROMAH MASKUR",
            no_hp=GURU_HP,
            password_hash=hash_password(DEFAULT_PASSWORD),
            role="wali_kelas",
        )
        session.add(guru)
        await session.flush()
    elif guru.role == "guru":
        guru.role = "wali_kelas"

    wali = (await session.execute(select(UserMadrasah).where(UserMadrasah.no_hp == WALI_HP))).scalar_one_or_none()
    if not wali:
        wali = UserMadrasah(
            nama="Wali Faqih",
            no_hp=WALI_HP,
            password_hash=hash_password(DEFAULT_PASSWORD),
            role="wali_santri",
        )
        session.add(wali)
        await session.flush()

    tingkat = (await session.execute(select(TingkatMadrasah).where(TingkatMadrasah.nama == TINGKAT_NAMA))).scalar_one_or_none()
    if not tingkat:
        tingkat = TingkatMadrasah(nama=TINGKAT_NAMA, urutan=1)
        session.add(tingkat)
        await session.flush()

    rombel = (await session.execute(select(RombelMadrasah).where(RombelMadrasah.nama == ROMBEL_NAMA))).scalar_one_or_none()
    if not rombel:
        rombel = RombelMadrasah(nama=ROMBEL_NAMA, tingkat_id=tingkat.id, wali_kelas_id=guru.id)
        session.add(rombel)
        await session.flush()
    else:
        rombel.wali_kelas_id = guru.id
        rombel.tingkat_id = tingkat.id

    kelas = (await session.execute(select(KelasMadrasah).where(KelasMadrasah.nama_kelas == ROMBEL_NAMA))).scalar_one_or_none()
    if not kelas:
        kelas = KelasMadrasah(id=rombel.id, nama_kelas=ROMBEL_NAMA)
        session.add(kelas)
        await session.flush()

    santri = (
        await session.execute(select(SantriMadrasah).where(SantriMadrasah.nama == "MUHAMMAD FAQIH AL MURTADLO"))
    ).scalar_one_or_none()
    if not santri:
        santri = SantriMadrasah(
            nama="MUHAMMAD FAQIH AL MURTADLO",
            kelas_id=kelas.id,
            rombel_id=rombel.id,
            orang_tua_id=wali.id,
        )
        session.add(santri)
        await session.flush()
    else:
        santri.rombel_id = rombel.id
        santri.kelas_id = kelas.id
        santri.orang_tua_id = wali.id

    mapel = (await session.execute(select(MapelMadrasah).where(MapelMadrasah.kode == "NGJI"))).scalar_one_or_none()
    if not mapel:
        mapel = MapelMadrasah(kode="NGJI", nama="Mengaji Jilid")
        session.add(mapel)
        await session.flush()
        session.add(MateriTarget(mapel_id=mapel.id, judul="Iqra halaman 1-5", urutan=1, aktif=True))

    return {
        "guru_id": guru.id,
        "wali_id": wali.id,
        "kelas_id": kelas.id,
        "rombel_id": rombel.id,
        "tingkat_id": tingkat.id,
        "santri_id": santri.id,
        "mapel_id": mapel.id,
    }
