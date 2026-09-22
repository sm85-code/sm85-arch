"""Idempotent starter rows for the isolated madrasah Neon database."""
from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase, engine
from tenants.madrasah.modules.madrasah.infrastructure.models import (
    GuruMapelRombel,
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

# Akun default tambahan supaya semua role bisa langsung dites tanpa perlu
# dibuat manual lewat AdminPortal (yang notabene butuh akun admin untuk
# mengaksesnya — ayam-telur kalau tidak di-seed dari sini).
ADMIN_HP = "081200000001"
KEPALA_SEKOLAH_HP = "081200000002"
KURIKULUM_HP = "081200000003"
BENDAHARA_HP = "081200000004"
GURU_MAPEL_HP = "081200000005"


async def _ensure_columns(conn) -> None:
    await conn.execute(text("ALTER TABLE IF EXISTS madrasah_santri ADD COLUMN IF NOT EXISTS rombel_id VARCHAR(64) NULL"))
    await conn.execute(text("ALTER TABLE IF EXISTS madrasah_tagihan_syahriyah ADD COLUMN IF NOT EXISTS dibayar_pada TIMESTAMPTZ NULL"))
    await conn.execute(text("ALTER TABLE IF EXISTS madrasah_progres_hafalan ADD COLUMN IF NOT EXISTS mapel_id VARCHAR(64) NULL"))
    await conn.execute(text("ALTER TABLE IF EXISTS madrasah_progres_hafalan ADD COLUMN IF NOT EXISTS materi_id VARCHAR(64) NULL"))
    await conn.execute(text("ALTER TABLE IF EXISTS madrasah_absensi ADD COLUMN IF NOT EXISTS mapel_id VARCHAR(64) NULL"))
    await conn.execute(text("ALTER TABLE IF EXISTS madrasah_tagihan_syahriyah ADD COLUMN IF NOT EXISTS diajukan_oleh VARCHAR(64) NULL"))
    await conn.execute(text("ALTER TABLE IF EXISTS madrasah_tagihan_syahriyah ADD COLUMN IF NOT EXISTS diajukan_pada TIMESTAMPTZ NULL"))


async def _ensure_user(session: AsyncSession, *, no_hp: str, nama: str, role: str) -> UserMadrasah:
    """Idempotent: buat akun kalau belum ada (cek by no_hp), kalau sudah ada
    dan role-nya beda, biarkan (jangan timpa role manual yang mungkin sudah
    diubah admin) -- konsisten dengan pola akun guru/wali yang sudah ada di
    fungsi ini."""
    user = (await session.execute(select(UserMadrasah).where(UserMadrasah.no_hp == no_hp))).scalar_one_or_none()
    if not user:
        user = UserMadrasah(nama=nama, no_hp=no_hp, password_hash=hash_password(DEFAULT_PASSWORD), role=role)
        session.add(user)
        await session.flush()
    return user


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

    admin = await _ensure_user(session, no_hp=ADMIN_HP, nama="Admin Madrasah", role="admin")
    kepsek = await _ensure_user(session, no_hp=KEPALA_SEKOLAH_HP, nama="Kepala Sekolah", role="kepala_sekolah")
    kurikulum_user = await _ensure_user(session, no_hp=KURIKULUM_HP, nama="Staf Kurikulum", role="kurikulum")
    bendahara_user = await _ensure_user(session, no_hp=BENDAHARA_HP, nama="Bendahara Madrasah", role="bendahara")
    guru_mapel_user = await _ensure_user(session, no_hp=GURU_MAPEL_HP, nama="Guru Mapel Contoh", role="guru")

    # Supaya akun guru mapel contoh langsung bisa dites tanpa harus login
    # sebagai kurikulum dulu untuk membuat penugasan secara manual.
    penugasan = (
        await session.execute(
            select(GuruMapelRombel).where(
                GuruMapelRombel.guru_id == guru_mapel_user.id,
                GuruMapelRombel.mapel_id == mapel.id,
                GuruMapelRombel.rombel_id == rombel.id,
            )
        )
    ).scalar_one_or_none()
    if not penugasan:
        session.add(GuruMapelRombel(guru_id=guru_mapel_user.id, mapel_id=mapel.id, rombel_id=rombel.id))
        await session.flush()

    return {
        "guru_id": guru.id,
        "wali_id": wali.id,
        "kelas_id": kelas.id,
        "rombel_id": rombel.id,
        "tingkat_id": tingkat.id,
        "santri_id": santri.id,
        "mapel_id": mapel.id,
        "admin_id": admin.id,
        "kepala_sekolah_id": kepsek.id,
        "kurikulum_id": kurikulum_user.id,
        "bendahara_id": bendahara_user.id,
        "guru_mapel_id": guru_mapel_user.id,
    }
