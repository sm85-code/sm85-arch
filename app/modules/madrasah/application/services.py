"""Madrasah use-cases against the isolated Neon session (multi-role)."""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.madrasah.application.schemas import (
    AbsenBulkRequest,
    AbsenMapelBulkRequest,
    GuruIn,
    JadwalIn,
    LoginRequest,
    MapelIn,
    MateriIn,
    MateriPatch,
    PenugasanIn,
    PlacementIn,
    ProgresCreateRequest,
    ProgresPatch,
    RombelIn,
    SantriIn,
    TingkatIn,
)
from app.modules.madrasah.infrastructure.models import (
    AbsensiMadrasah,
    GuruMapelRombel,
    JadwalMadrasah,
    KelasMadrasah,
    MapelMadrasah,
    MateriTarget,
    PengumumanMadrasah,
    ProgresHafalan,
    RombelMadrasah,
    SantriMadrasah,
    TagihanSyahriyah,
    TingkatMadrasah,
    UserMadrasah,
)
from shared.security import hash_password, verify_password

STATUS_BELUM = "Belum Bayar"
STATUS_LUNAS = "Lunas"
DEFAULT_SPP_NOMINAL = Decimal(os.getenv("SPP_NOMINAL", "50000"))


class MadrasahAuthError(Exception):
    pass


class MadrasahNotFoundError(Exception):
    pass


class MadrasahForbiddenError(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _bulan_tahun(value: date | None = None) -> str:
    return (value or date.today()).strftime("%Y-%m")


def user_out(user: UserMadrasah) -> dict:
    return {"id": user.id, "nama": user.nama, "no_hp": user.no_hp, "role": user.role}


def tagihan_status_label(row: TagihanSyahriyah) -> str:
    return STATUS_LUNAS if row.status_bayar else STATUS_BELUM


def tagihan_out(row: TagihanSyahriyah, nama: str | None = None) -> dict:
    paid_at = row.dibayar_pada.isoformat() if row.dibayar_pada else None
    return {
        "id": row.id,
        "santri_id": row.santri_id,
        "nama": nama or (row.santri.nama if getattr(row, "santri", None) else "-"),
        "nama_santri": nama or (row.santri.nama if getattr(row, "santri", None) else "-"),
        "bulan_tahun": row.bulan_tahun,
        "nominal": float(row.nominal),
        "status": tagihan_status_label(row),
        "status_bayar": row.status_bayar,
        "lunas": row.status_bayar,
        "dibayar_pada": paid_at,
    }


async def login_by_phone(session: AsyncSession, payload: LoginRequest) -> UserMadrasah:
    user = (await session.execute(select(UserMadrasah).where(UserMadrasah.no_hp == payload.no_hp.strip()))).scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        raise MadrasahAuthError("Nomor HP atau password salah")
    return user


async def list_kelas(session: AsyncSession):
    rombel = list((await session.execute(select(RombelMadrasah).order_by(RombelMadrasah.nama))).scalars())
    if rombel:
        return [type("Alias", (), {"id": r.id, "nama_kelas": r.nama})() for r in rombel]
    return list((await session.execute(select(KelasMadrasah).order_by(KelasMadrasah.nama_kelas))).scalars())


async def list_santri(session: AsyncSession, kelas_id: str | None = None) -> list[SantriMadrasah]:
    stmt = select(SantriMadrasah).order_by(SantriMadrasah.nama)
    if kelas_id:
        stmt = stmt.where((SantriMadrasah.kelas_id == kelas_id) | (SantriMadrasah.rombel_id == kelas_id))
    return list((await session.execute(stmt)).scalars())


async def bulk_insert_absensi(session: AsyncSession, payload: AbsenBulkRequest, guru: UserMadrasah | None = None) -> list[AbsensiMadrasah]:
    if guru:
        await assert_own_rombel_santri(session, guru, [item.santri_id for item in payload.items])
    rows: list[AbsensiMadrasah] = []
    for item in payload.items:
        row = AbsensiMadrasah(tanggal=payload.tanggal, status=item.status, santri_id=item.santri_id, guru_id=payload.guru_id)
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows


async def assert_own_rombel_santri(session: AsyncSession, guru: UserMadrasah, santri_ids: list[str]) -> None:
    """Wali kelas cuma boleh input/edit absensi & progres untuk santri di
    rombel yang dia asuh sendiri (RombelMadrasah.wali_kelas_id == guru.id).
    Admin/kepala sekolah bypass (perlu akses lintas kelas). Role lain (guru
    mapel biasa) tidak dibatasi lewat fungsi ini -- dia punya jalurnya sendiri
    (GuruMapelRombel, lihat bulk_insert_absensi_mapel)."""
    if guru.role in ("admin", "kepala_sekolah"):
        return
    if guru.role != "wali_kelas":
        return
    if not santri_ids:
        return
    rows = list(
        (
            await session.execute(
                select(SantriMadrasah.id, SantriMadrasah.rombel_id).where(SantriMadrasah.id.in_(santri_ids))
            )
        ).all()
    )
    rombel_ids = {r.rombel_id for r in rows if r.rombel_id}
    if not rombel_ids:
        raise MadrasahForbiddenError("Santri tidak ditemukan di rombel Anda")
    owned = list(
        (
            await session.execute(
                select(RombelMadrasah.id).where(RombelMadrasah.id.in_(rombel_ids), RombelMadrasah.wali_kelas_id == guru.id)
            )
        ).scalars()
    )
    if set(owned) != rombel_ids:
        raise MadrasahForbiddenError("Anda hanya dapat mengelola santri di rombel Anda sendiri")


async def list_rombel_for_caller(session: AsyncSession, caller: UserMadrasah) -> list[RombelMadrasah]:
    rows = await list_rombel(session)
    if caller.role != "wali_kelas":
        return rows
    return [r for r in rows if r.wali_kelas_id == caller.id]


async def list_santri_for_caller(session: AsyncSession, caller: UserMadrasah, kelas_id: str | None) -> list[SantriMadrasah]:
    """Untuk wali kelas: abaikan/timpa kelas_id yang dikirim client, paksa
    hanya rombel miliknya sendiri -- menutup celah client mengganti kelas_id
    di request untuk melihat santri kelas lain."""
    if caller.role != "wali_kelas":
        return await list_santri(session, kelas_id)
    own_rombel = list(
        (await session.execute(select(RombelMadrasah.id).where(RombelMadrasah.wali_kelas_id == caller.id))).scalars()
    )
    if not own_rombel:
        return []
    stmt = select(SantriMadrasah).where(SantriMadrasah.rombel_id.in_(own_rombel)).order_by(SantriMadrasah.nama)
    return list((await session.execute(stmt)).scalars())


async def create_progres(session: AsyncSession, payload: ProgresCreateRequest, guru: UserMadrasah | None = None) -> ProgresHafalan:
    if not await session.get(SantriMadrasah, payload.santri_id):
        raise MadrasahNotFoundError("Santri tidak ditemukan")
    if guru:
        await assert_own_rombel_santri(session, guru, [payload.santri_id])
    row = ProgresHafalan(
        tanggal=payload.tanggal,
        santri_id=payload.santri_id,
        tipe=payload.tipe,
        capaian=payload.capaian,
        catatan_guru=payload.catatan_guru or "",
        mapel_id=payload.mapel_id,
        materi_id=payload.materi_id,
    )
    session.add(row)
    await session.flush()
    return row


async def patch_progres(session: AsyncSession, guru: UserMadrasah, progres_id: str, payload: ProgresPatch) -> ProgresHafalan:
    row = await session.get(ProgresHafalan, progres_id)
    if not row:
        raise MadrasahNotFoundError("Entri progres tidak ditemukan")
    santri = await session.get(SantriMadrasah, row.santri_id)
    if not santri or not await _can_view_santri(session, guru, santri):
        raise MadrasahForbiddenError("Anda tidak berwenang mengubah entri ini")
    if payload.capaian is not None:
        row.capaian = payload.capaian
    if payload.catatan_guru is not None:
        row.catatan_guru = payload.catatan_guru
    await session.flush()
    return row


async def list_tagihan(session: AsyncSession, santri_id: str) -> list[TagihanSyahriyah]:
    return list((await session.execute(select(TagihanSyahriyah).where(TagihanSyahriyah.santri_id == santri_id).order_by(TagihanSyahriyah.bulan_tahun.desc()))).scalars())


async def list_pengumuman(session: AsyncSession, limit: int = 50) -> list[PengumumanMadrasah]:
    return list((await session.execute(select(PengumumanMadrasah).order_by(PengumumanMadrasah.tanggal.desc()).limit(limit))).scalars())


async def generate_spp_massal(session: AsyncSession) -> list[TagihanSyahriyah]:
    await session.execute(text("ALTER TABLE IF EXISTS madrasah_tagihan_syahriyah ADD COLUMN IF NOT EXISTS dibayar_pada TIMESTAMPTZ NULL"))
    await session.execute(text("ALTER TABLE IF EXISTS madrasah_santri ADD COLUMN IF NOT EXISTS rombel_id VARCHAR(64) NULL"))
    period = _bulan_tahun()
    santri_rows = list((await session.execute(select(SantriMadrasah))).scalars())
    existing = {r.santri_id for r in (await session.execute(select(TagihanSyahriyah).where(TagihanSyahriyah.bulan_tahun == period))).scalars()}
    for santri in santri_rows:
        if santri.id in existing:
            continue
        session.add(TagihanSyahriyah(bulan_tahun=period, nominal=DEFAULT_SPP_NOMINAL, status_bayar=False, santri_id=santri.id))
    await session.flush()
    return list((await session.execute(select(TagihanSyahriyah).options(selectinload(TagihanSyahriyah.santri)).where(TagihanSyahriyah.bulan_tahun == period))).scalars())


async def pay_spp_manual(session: AsyncSession, target_id: str) -> TagihanSyahriyah:
    row = await session.get(TagihanSyahriyah, target_id)
    if row is None:
        row = (await session.execute(select(TagihanSyahriyah).where(TagihanSyahriyah.santri_id == target_id, TagihanSyahriyah.status_bayar.is_(False)).order_by(TagihanSyahriyah.bulan_tahun.desc()))).scalar_one_or_none()
    if row is None:
        raise MadrasahNotFoundError("Tagihan SPP tidak ditemukan")
    row.status_bayar = True
    row.dibayar_pada = _utcnow()
    await session.flush()
    await session.refresh(row, attribute_names=["santri"])
    return row


async def list_tingkat(session: AsyncSession) -> list[TingkatMadrasah]:
    return list((await session.execute(select(TingkatMadrasah).order_by(TingkatMadrasah.urutan))).scalars())


async def create_tingkat(session: AsyncSession, payload: TingkatIn) -> TingkatMadrasah:
    row = TingkatMadrasah(nama=payload.nama, urutan=payload.urutan)
    session.add(row)
    await session.flush()
    return row


async def list_rombel(session: AsyncSession) -> list[RombelMadrasah]:
    return list((await session.execute(select(RombelMadrasah).options(selectinload(RombelMadrasah.wali_kelas), selectinload(RombelMadrasah.tingkat)).order_by(RombelMadrasah.nama))).scalars())


async def create_rombel(session: AsyncSession, payload: RombelIn) -> RombelMadrasah:
    row = RombelMadrasah(nama=payload.nama, tingkat_id=payload.tingkat_id, wali_kelas_id=payload.wali_kelas_id)
    session.add(row)
    await session.flush()
    if not (await session.execute(select(KelasMadrasah).where(KelasMadrasah.nama_kelas == payload.nama))).scalar_one_or_none():
        session.add(KelasMadrasah(id=row.id, nama_kelas=payload.nama))
        await session.flush()
    return row


async def list_guru(session: AsyncSession) -> list[UserMadrasah]:
    return list((await session.execute(select(UserMadrasah).where(UserMadrasah.role.in_(["wali_kelas", "guru", "kepala_sekolah", "kurikulum", "bendahara"])).order_by(UserMadrasah.nama))).scalars())


async def create_guru(session: AsyncSession, payload: GuruIn) -> UserMadrasah:
    row = UserMadrasah(nama=payload.nama, no_hp=payload.no_hp.strip(), password_hash=hash_password(payload.password), role=payload.role or "wali_kelas")
    session.add(row)
    await session.flush()
    return row


async def place_santri(session: AsyncSession, payload: PlacementIn) -> SantriMadrasah:
    santri = await session.get(SantriMadrasah, payload.santri_id)
    rombel = await session.get(RombelMadrasah, payload.rombel_id)
    if not santri or not rombel:
        raise MadrasahNotFoundError("Santri atau rombel tidak ditemukan")
    santri.rombel_id = rombel.id
    santri.kelas_id = rombel.id
    await session.flush()
    return santri


async def create_santri(session: AsyncSession, payload: SantriIn) -> SantriMadrasah:
    row = SantriMadrasah(nama=payload.nama, rombel_id=payload.rombel_id, kelas_id=payload.kelas_id or payload.rombel_id, orang_tua_id=payload.orang_tua_id)
    session.add(row)
    await session.flush()
    return row


async def list_mapel(session: AsyncSession) -> list[MapelMadrasah]:
    return list((await session.execute(select(MapelMadrasah).options(selectinload(MapelMadrasah.materi)).order_by(MapelMadrasah.nama))).scalars())


async def create_mapel(session: AsyncSession, payload: MapelIn) -> MapelMadrasah:
    row = MapelMadrasah(kode=payload.kode, nama=payload.nama)
    session.add(row)
    await session.flush()
    return row


async def create_materi(session: AsyncSession, payload: MateriIn) -> MateriTarget:
    if not await session.get(MapelMadrasah, payload.mapel_id):
        raise MadrasahNotFoundError("Mapel tidak ditemukan")
    row = MateriTarget(mapel_id=payload.mapel_id, judul=payload.judul, urutan=payload.urutan, aktif=payload.aktif)
    session.add(row)
    await session.flush()
    return row


async def patch_materi(session: AsyncSession, materi_id: str, payload: MateriPatch) -> MateriTarget:
    row = await session.get(MateriTarget, materi_id)
    if not row:
        raise MadrasahNotFoundError("Materi tidak ditemukan")
    if payload.judul is not None:
        row.judul = payload.judul
    if payload.urutan is not None:
        row.urutan = payload.urutan
    if payload.aktif is not None:
        row.aktif = payload.aktif
    await session.flush()
    return row


async def list_jadwal(session: AsyncSession, rombel_id: str | None = None) -> list[JadwalMadrasah]:
    stmt = select(JadwalMadrasah).options(selectinload(JadwalMadrasah.mapel), selectinload(JadwalMadrasah.rombel))
    if rombel_id:
        stmt = stmt.where(JadwalMadrasah.rombel_id == rombel_id)
    return list((await session.execute(stmt)).scalars())


async def create_jadwal(session: AsyncSession, payload: JadwalIn) -> JadwalMadrasah:
    row = JadwalMadrasah(rombel_id=payload.rombel_id, mapel_id=payload.mapel_id, hari=payload.hari, jam_mulai=payload.jam_mulai, jam_selesai=payload.jam_selesai)
    session.add(row)
    await session.flush()
    return row


async def progres_series(session: AsyncSession, santri_id: str) -> list[dict]:
    rows = list((await session.execute(select(ProgresHafalan).where(ProgresHafalan.santri_id == santri_id).order_by(ProgresHafalan.tanggal.asc()))).scalars())
    return [{"id": r.id, "tanggal": r.tanggal.isoformat(), "tipe": r.tipe, "capaian": r.capaian, "catatan_guru": r.catatan_guru, "mapel_id": r.mapel_id, "materi_id": r.materi_id} for r in rows]


# --- Guru Mapel: penugasan guru<->mapel<->rombel (fondasi baru, additive) ---

async def assign_guru_mapel(session: AsyncSession, payload: PenugasanIn) -> GuruMapelRombel:
    existing = (
        await session.execute(
            select(GuruMapelRombel).where(
                GuruMapelRombel.guru_id == payload.guru_id,
                GuruMapelRombel.mapel_id == payload.mapel_id,
                GuruMapelRombel.rombel_id == payload.rombel_id,
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    row = GuruMapelRombel(guru_id=payload.guru_id, mapel_id=payload.mapel_id, rombel_id=payload.rombel_id)
    session.add(row)
    await session.flush()
    return row


async def remove_penugasan(session: AsyncSession, penugasan_id: str) -> None:
    row = await session.get(GuruMapelRombel, penugasan_id)
    if not row:
        raise MadrasahNotFoundError("Penugasan tidak ditemukan")
    await session.delete(row)


async def list_penugasan(session: AsyncSession, guru_id: str | None = None) -> list[GuruMapelRombel]:
    stmt = select(GuruMapelRombel).options(
        selectinload(GuruMapelRombel.guru), selectinload(GuruMapelRombel.mapel), selectinload(GuruMapelRombel.rombel)
    )
    if guru_id:
        stmt = stmt.where(GuruMapelRombel.guru_id == guru_id)
    return list((await session.execute(stmt)).scalars())


async def bulk_insert_absensi_mapel(session: AsyncSession, guru_id: str, payload: AbsenMapelBulkRequest) -> list[AbsensiMadrasah]:
    # Defense in depth: pastikan guru ini memang ditugaskan untuk kombinasi
    # mapel+rombel ini, jangan percaya begitu saja rombel_id/mapel_id dari
    # body request (mengulang temuan IDOR yang sama seperti sebelumnya).
    penugasan = (
        await session.execute(
            select(GuruMapelRombel).where(
                GuruMapelRombel.guru_id == guru_id,
                GuruMapelRombel.mapel_id == payload.mapel_id,
                GuruMapelRombel.rombel_id == payload.rombel_id,
            )
        )
    ).scalar_one_or_none()
    if not penugasan:
        raise MadrasahNotFoundError("Anda tidak ditugaskan untuk mapel/rombel ini")

    rows: list[AbsensiMadrasah] = []
    for item in payload.items:
        row = AbsensiMadrasah(
            tanggal=payload.tanggal,
            status=item.status,
            santri_id=item.santri_id,
            guru_id=guru_id,
            mapel_id=payload.mapel_id,
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows


async def rekap_absensi_mapel(session: AsyncSession, rombel_id: str, mapel_id: str) -> list[dict]:
    stmt = (
        select(AbsensiMadrasah)
        .join(SantriMadrasah, AbsensiMadrasah.santri_id == SantriMadrasah.id)
        .where(SantriMadrasah.rombel_id == rombel_id, AbsensiMadrasah.mapel_id == mapel_id)
        .order_by(AbsensiMadrasah.tanggal.desc())
    )
    rows = list((await session.execute(stmt)).scalars())
    return [{"id": r.id, "tanggal": r.tanggal.isoformat(), "santri_id": r.santri_id, "status": r.status} for r in rows]


# --- Rapor: baca riwayat progres gabungan seorang santri, dengan cek ---
# kepemilikan supaya guru/wali kelas cuma bisa lihat santri yang benar-benar
# ada hubungannya (rombel yang dia ajar/asuh), bukan santri siapa pun.

async def _can_view_santri(session: AsyncSession, guru: UserMadrasah, santri: SantriMadrasah) -> bool:
    if guru.role in ("admin", "kepala_sekolah"):
        return True
    if not santri.rombel_id:
        return False
    if guru.role == "wali_kelas":
        rombel = await session.get(RombelMadrasah, santri.rombel_id)
        if rombel and rombel.wali_kelas_id == guru.id:
            return True
    penugasan = (
        await session.execute(
            select(GuruMapelRombel).where(GuruMapelRombel.guru_id == guru.id, GuruMapelRombel.rombel_id == santri.rombel_id)
        )
    ).scalar_one_or_none()
    return bool(penugasan)


async def rapor_santri(session: AsyncSession, guru: UserMadrasah, santri_id: str) -> dict:
    santri = await session.get(SantriMadrasah, santri_id)
    if not santri:
        raise MadrasahNotFoundError("Santri tidak ditemukan")
    if not await _can_view_santri(session, guru, santri):
        raise MadrasahForbiddenError("Anda tidak berwenang melihat rapor santri ini")
    return {"santri_id": santri.id, "santri_nama": santri.nama, "progres": await progres_series(session, santri_id)}
