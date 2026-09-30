"""Madrasah use-cases against the isolated Neon session (multi-role)."""
from __future__ import annotations

import os
import re
import secrets
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, func, inspect as sa_inspect, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tenants.madrasah.modules.madrasah.application.schemas import (
    AbsenBulkRequest,
    AbsenMapelBulkRequest,
    BukuKasIn,
    GuruIn,
    JadwalIn,
    KegiatanIn,
    LoginRequest,
    MapelIn,
    MapelPatch,
    MateriIn,
    MateriPatch,
    PendaftaranIn,
    PendaftaranPatch,
    PenugasanIn,
    ProfilPatch,
    PengaturanPatch,
    PlacementIn,
    ProgresCreateRequest,
    ProgresPatch,
    RombelIn,
    RombelPatch,
    SantriIn,
    SantriPatch,
    KenaikanKelasRequest,
    MadrasahUnitIn,
    MadrasahUnitPatch,
    PengumumanIn,
    PesanIn,
    SantriStatusIn,
    SemesterIn,
    TahunAjaranIn,
    TingkatIn,
    TingkatPatch,
    UserPatch,
    YayasanPatch,
)
from tenants.madrasah.modules.madrasah.infrastructure.models import (
    AbsensiMadrasah,
    AkunMadrasah,
    AuditLogMadrasah,
    BukuKasMadrasah,
    GuruMapelRombel,
    HonorMengajar,
    JadwalMadrasah,
    JurnalMadrasah,
    KegiatanMadrasah,
    KelasMadrasah,
    MadrasahUnit,
    MapelMadrasah,
    MateriTarget,
    PendaftaranSantri,
    PengaturanSekolah,
    PengumumanMadrasah,
    PesanMadrasah,
    ProgresHafalan,
    RiwayatPenempatanSantri,
    RombelMadrasah,
    SantriMadrasah,
    SemesterMadrasah,
    TagihanSyahriyah,
    TahunAjaranMadrasah,
    TingkatMadrasah,
    TugasMadrasah,
    UserMadrasah,
    Yayasan,
)
from shared.security import hash_password, verify_password

STATUS_BELUM = "Belum Bayar"
STATUS_MENUNGGU = "Menunggu Verifikasi"
STATUS_LUNAS = "Lunas"
DEFAULT_SPP_NOMINAL = Decimal(os.getenv("SPP_NOMINAL", "50000"))
DEFAULT_HONOR_PER_SESI = Decimal(os.getenv("HONOR_PER_SESI", "10000"))

# Chart of Accounts minimal & tetap -- lihat AkunMadrasah. Diseed idempoten
# oleh seed_akun_default() (dipanggil dari ensure_madrasah_schema()).
AKUN_KAS = "KAS-001"
AKUN_PENDAPATAN_SPP = "PSP-001"
AKUN_PENDAPATAN_LAIN = "PDL-001"
AKUN_BEBAN_ATK = "BATK-001"
AKUN_BEBAN_HONOR = "BHNR-001"
AKUN_BEBAN_LAIN = "BLN-001"

AKUN_DEFAULT: list[tuple[str, str, str]] = [
    (AKUN_KAS, "Kas", "aset"),
    (AKUN_PENDAPATAN_SPP, "Pendapatan SPP/Syahriyah", "pendapatan"),
    (AKUN_PENDAPATAN_LAIN, "Pendapatan Lain-lain", "pendapatan"),
    (AKUN_BEBAN_ATK, "Beban ATK", "beban"),
    (AKUN_BEBAN_HONOR, "Beban Honor Mengajar", "beban"),
    (AKUN_BEBAN_LAIN, "Beban Lain-lain", "beban"),
]


class MadrasahAuthError(Exception):
    pass


class MadrasahNotFoundError(Exception):
    pass


class MadrasahForbiddenError(Exception):
    pass


CROSS_UNIT_ROLES = frozenset({"admin", "yayasan_admin"})
JENIS_TUGAS = frozenset({"guru_mapel", "kepala_sekolah", "lembaga_admin", "kurikulum", "bendahara", "wali_kelas"})
TUGAS_KE_ROLE = {
    "guru_mapel": "guru",
    "kepala_sekolah": "kepala_sekolah",
    "lembaga_admin": "lembaga_admin",
    "kurikulum": "kurikulum",
    "bendahara": "bendahara",
    "wali_kelas": "wali_kelas",
}
ROLE_KE_JENIS = {v: k for k, v in TUGAS_KE_ROLE.items()}
ROLE_KE_JENIS["guru"] = "guru_mapel"
PENGELOLA_ROMBEL = frozenset({"admin", "yayasan_admin", "kepala_sekolah", "lembaga_admin"})


def pengelola_lintas_rombel(user: UserMadrasah | None) -> bool:
    return bool(effective_roles(user) & PENGELOLA_ROMBEL)


def hanya_wali_kelas(user: UserMadrasah | None) -> bool:
    return "wali_kelas" in effective_roles(user) and not pengelola_lintas_rombel(user)


def _tugas_rows(user: UserMadrasah | None) -> list:
    if user is None:
        return []
    cached = getattr(user, "_tugas_list", None)
    if cached is not None:
        return list(cached)
    return []


def effective_roles(user: UserMadrasah | None) -> set[str]:
    if user is None:
        return set()
    roles = {(user.role or "").strip().lower()}
    for row in _tugas_rows(user):
        roles.add(TUGAS_KE_ROLE.get(row.jenis, row.jenis))
    return {r for r in roles if r}


def unit_ids_tugas(user: UserMadrasah | None) -> set[str]:
    ids: set[str] = set()
    if user is None:
        return ids
    if user.madrasah_unit_id:
        ids.add(user.madrasah_unit_id)
    for row in _tugas_rows(user):
        if row.madrasah_unit_id:
            ids.add(row.madrasah_unit_id)
    return ids


def resolve_unit_scope(caller: UserMadrasah | None, requested_unit_id: str | None = None) -> str | None:
    """None = lihat semua unit. String = filter ke unit itu.

    admin dan yayasan_admin melihat semua, kecuali mereka sendiri meminta
    unit_id. Role lain terkunci ke madrasah_unit_id di akun. Wali santri
    tidak di-scope di sini (akses lewat anak asuh).
    """
    if caller is None:
        return requested_unit_id
    if caller.role == "wali_santri":
        return None
    if caller.role in CROSS_UNIT_ROLES or "admin" in effective_roles(caller) or "yayasan_admin" in effective_roles(caller):
        return requested_unit_id or None
    allowed = unit_ids_tugas(caller)
    if not allowed and not caller.madrasah_unit_id:
        raise MadrasahForbiddenError("Akun ini belum terikat unit madrasah")
    if requested_unit_id:
        if requested_unit_id not in allowed:
            raise MadrasahForbiddenError("Tidak dapat mengakses unit lain")
        return requested_unit_id
    if not caller.madrasah_unit_id:
        raise MadrasahForbiddenError("Akun ini belum terikat unit madrasah")
    return caller.madrasah_unit_id


async def assign_unit_id(
    session: AsyncSession,
    caller: UserMadrasah | None,
    requested_unit_id: str | None,
) -> str | None:
    """Unit yang ditempel saat create. Staf biasa tidak bisa memilih unit lain."""
    if caller is None or caller.role in CROSS_UNIT_ROLES:
        return requested_unit_id or await _default_unit_id(session)
    if caller.role == "wali_santri":
        return requested_unit_id or await _default_unit_id(session)
    if requested_unit_id and caller.madrasah_unit_id and requested_unit_id != caller.madrasah_unit_id:
        raise MadrasahForbiddenError("Tidak dapat membuat data di unit lain")
    return caller.madrasah_unit_id or requested_unit_id or await _default_unit_id(session)


def _pastikan_akses_unit_keuangan(caller: UserMadrasah | None, unit_id: str | None) -> None:
    """Admin utama dan admin yayasan boleh lintas unit. Yang lain hanya unit sendiri."""
    if caller is None or caller.role in CROSS_UNIT_ROLES:
        return
    if not caller.madrasah_unit_id or unit_id != caller.madrasah_unit_id:
        raise MadrasahForbiddenError("Tidak dapat mengakses keuangan unit lain")


async def _unit_untuk_akun_baru(
    session: AsyncSession,
    caller: UserMadrasah | None,
    role: str,
    requested_unit_id: str | None,
) -> str | None:
    """Admin utama dan admin yayasan tidak punya unit. Staf wajib punya unit."""
    if role in ("admin", "yayasan_admin"):
        return None
    return await assign_unit_id(session, caller, requested_unit_id)


async def _ikat_kepala_ke_unit(session: AsyncSession, unit: MadrasahUnit, kepala_user_id: str | None) -> None:
    if not kepala_user_id:
        return
    user = await session.get(UserMadrasah, kepala_user_id)
    if not user or user.role != "kepala_sekolah":
        raise MadrasahNotFoundError("Akun kepala madrasah tidak ditemukan")
    user.madrasah_unit_id = unit.id
    unit.kepala_unit = user.nama
    await session.flush()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _bulan_tahun(value: date | None = None) -> str:
    return (value or date.today()).strftime("%Y-%m")


# --- Tahun Ajaran & Semester: fondasi periode akademik. Entity transaksional
# (absensi, progres, tagihan, jadwal) di-tag otomatis dengan semester aktif
# saat dibuat lewat _semester_aktif_id() -- baris lama (sebelum fitur ini
# ada) tetap semester_id=NULL, tidak di-backfill, supaya rilis fitur ini
# tidak mengubah data historis siapa pun. ---

def tahun_ajaran_out(row: TahunAjaranMadrasah) -> dict:
    return {
        "id": row.id,
        "kode": row.kode,
        "tanggal_mulai": row.tanggal_mulai.isoformat(),
        "tanggal_selesai": row.tanggal_selesai.isoformat(),
        "madrasah_unit_id": row.madrasah_unit_id,
    }


def semester_out(row: SemesterMadrasah) -> dict:
    tahun = None
    if "tahun_ajaran" not in sa_inspect(row).unloaded:
        tahun = row.tahun_ajaran.kode if row.tahun_ajaran else None
    return {
        "id": row.id,
        "tahun_ajaran_id": row.tahun_ajaran_id,
        "tahun_ajaran": tahun,
        "nama": row.nama,
        "tanggal_mulai": row.tanggal_mulai.isoformat(),
        "tanggal_selesai": row.tanggal_selesai.isoformat(),
        "status": row.status,
    }


async def create_tahun_ajaran(session: AsyncSession, payload: TahunAjaranIn, caller: UserMadrasah | None = None) -> TahunAjaranMadrasah:
    row = TahunAjaranMadrasah(
        kode=payload.kode,
        tanggal_mulai=payload.tanggal_mulai,
        tanggal_selesai=payload.tanggal_selesai,
        madrasah_unit_id=await assign_unit_id(session, caller, payload.madrasah_unit_id),
    )
    session.add(row)
    await session.flush()
    return row


async def list_tahun_ajaran(session: AsyncSession, unit_id: str | None = None) -> list[TahunAjaranMadrasah]:
    stmt = select(TahunAjaranMadrasah).order_by(TahunAjaranMadrasah.kode.desc())
    if unit_id:
        stmt = stmt.where(TahunAjaranMadrasah.madrasah_unit_id == unit_id)
    return list((await session.execute(stmt)).scalars())


async def create_semester(session: AsyncSession, payload: SemesterIn) -> SemesterMadrasah:
    if payload.tanggal_selesai < payload.tanggal_mulai:
        raise MadrasahForbiddenError("Tanggal selesai tidak boleh sebelum tanggal mulai")
    if not await session.get(TahunAjaranMadrasah, payload.tahun_ajaran_id):
        raise MadrasahNotFoundError("Tahun ajaran tidak ditemukan")
    sudah = (
        await session.execute(
            select(SemesterMadrasah.id).where(
                SemesterMadrasah.tahun_ajaran_id == payload.tahun_ajaran_id,
                SemesterMadrasah.nama == payload.nama,
            )
        )
    ).scalar_one_or_none()
    if sudah:
        raise MadrasahForbiddenError(f"Semester {payload.nama} untuk tahun ajaran ini sudah ada")
    row = SemesterMadrasah(
        tahun_ajaran_id=payload.tahun_ajaran_id,
        nama=payload.nama,
        tanggal_mulai=payload.tanggal_mulai,
        tanggal_selesai=payload.tanggal_selesai,
        status="draft",
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise MadrasahForbiddenError(f"Semester {payload.nama} untuk tahun ajaran ini sudah ada") from exc
    await session.refresh(row, attribute_names=["tahun_ajaran"])
    return row


async def list_semester(session: AsyncSession, unit_id: str | None = None) -> list[SemesterMadrasah]:
    stmt = (
        select(SemesterMadrasah)
        .options(selectinload(SemesterMadrasah.tahun_ajaran))
        .join(TahunAjaranMadrasah, SemesterMadrasah.tahun_ajaran_id == TahunAjaranMadrasah.id)
        .order_by(SemesterMadrasah.tanggal_mulai.desc())
    )
    if unit_id:
        stmt = stmt.where(TahunAjaranMadrasah.madrasah_unit_id == unit_id)
    return list((await session.execute(stmt)).scalars())


async def get_semester_aktif(session: AsyncSession, unit_id: str | None = None) -> SemesterMadrasah | None:
    stmt = (
        select(SemesterMadrasah)
        .options(selectinload(SemesterMadrasah.tahun_ajaran))
        .where(SemesterMadrasah.status == "aktif")
    )
    if unit_id:
        stmt = stmt.join(TahunAjaranMadrasah).where(TahunAjaranMadrasah.madrasah_unit_id == unit_id)
    stmt = stmt.limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _semester_aktif_id(session: AsyncSession) -> str | None:
    row = (await session.execute(select(SemesterMadrasah.id).where(SemesterMadrasah.status == "aktif").limit(1))).scalar_one_or_none()
    return row


async def aktifkan_semester(session: AsyncSession, semester_id: str) -> SemesterMadrasah:
    """Hanya satu semester boleh aktif sekaligus -- semester lain yang masih
    "aktif" (seharusnya tidak ada lebih dari satu, tapi dijaga di sini)
    diturunkan ke "ditutup" dulu sebelum yang baru diaktifkan."""
    row = await session.get(SemesterMadrasah, semester_id)
    if not row:
        raise MadrasahNotFoundError("Semester tidak ditemukan")
    await session.refresh(row, attribute_names=["tahun_ajaran"])
    unit_id = row.tahun_ajaran.madrasah_unit_id if row.tahun_ajaran else None
    aktif_lain = select(SemesterMadrasah.id).join(TahunAjaranMadrasah).where(SemesterMadrasah.status == "aktif")
    if unit_id:
        aktif_lain = aktif_lain.where(TahunAjaranMadrasah.madrasah_unit_id == unit_id)
    else:
        aktif_lain = aktif_lain.where(TahunAjaranMadrasah.madrasah_unit_id.is_(None))
    await session.execute(update(SemesterMadrasah).where(SemesterMadrasah.id.in_(aktif_lain)).values(status="ditutup"))
    row.status = "aktif"
    await session.flush()
    await session.refresh(row, attribute_names=["tahun_ajaran"])
    return row


async def tutup_semester(session: AsyncSession, semester_id: str) -> SemesterMadrasah:
    """Mengunci semester ini: tidak ada lagi input baru yang akan di-tag ke
    sini secara otomatis (lihat _semester_aktif_id -- setelah ditutup,
    semester ini tidak lagi dikembalikan sebagai "aktif"). Data yang sudah
    tercatat di dalamnya TIDAK dihapus atau diubah."""
    row = await session.get(SemesterMadrasah, semester_id)
    if not row:
        raise MadrasahNotFoundError("Semester tidak ditemukan")
    row.status = "ditutup"
    await session.flush()
    await session.refresh(row, attribute_names=["tahun_ajaran"])
    return row


def user_out(user: UserMadrasah, *, password_sementara: str | None = None) -> dict:
    tugas = [
        {"id": t.id, "jenis": t.jenis, "madrasah_unit_id": t.madrasah_unit_id}
        for t in _tugas_rows(user)
    ]
    out = {
        "id": user.id,
        "nama": user.nama,
        "no_hp": user.no_hp,
        "role": user.role,
        "roles": sorted(effective_roles(user)),
        "tugas": tugas,
        "madrasah_unit_id": user.madrasah_unit_id,
    }
    if password_sementara:
        # Cuma disertakan sekali, langsung setelah create_guru/create_wali_santri
        # menghasilkan password acak (payload.password kosong) -- tidak pernah
        # disimpan/di-return lagi setelahnya karena cuma hash yang tersimpan.
        out["password_sementara"] = password_sementara
    return out


def boleh_menugaskan(caller: UserMadrasah | None, jenis: str, unit_id: str | None) -> None:
    if caller is None:
        raise MadrasahForbiddenError("Tidak dapat menugaskan")
    if jenis not in JENIS_TUGAS:
        raise MadrasahForbiddenError("Jenis tugas tidak dikenal")
    roles = effective_roles(caller)
    if "admin" in roles:
        return
    if "yayasan_admin" in roles:
        return
    if "kepala_sekolah" in roles or "lembaga_admin" in roles:
        if jenis == "kepala_sekolah":
            raise MadrasahForbiddenError("Penugasan Kepala Madrasah hanya oleh Admin Yayasan atau Admin Utama")
        if jenis not in {"guru_mapel", "bendahara", "kurikulum", "wali_kelas", "lembaga_admin"}:
            raise MadrasahForbiddenError("Tugas ini tidak dapat diberikan dari unit")
        if "lembaga_admin" in roles and "kepala_sekolah" not in roles and jenis == "lembaga_admin":
            raise MadrasahForbiddenError("Admin Lembaga tidak dapat menunjuk Admin Lembaga lain")
        if not caller.madrasah_unit_id or unit_id != caller.madrasah_unit_id:
            raise MadrasahForbiddenError("Penugasan lintas unit hanya oleh Admin Yayasan atau Admin Utama")
        return
    raise MadrasahForbiddenError("Tidak berwenang menugaskan")


async def list_tugas(session: AsyncSession, unit_id: str | None = None, user_id: str | None = None) -> list[TugasMadrasah]:
    stmt = select(TugasMadrasah).options(selectinload(TugasMadrasah.user)).order_by(TugasMadrasah.jenis)
    if unit_id:
        stmt = stmt.where(TugasMadrasah.madrasah_unit_id == unit_id)
    if user_id:
        stmt = stmt.where(TugasMadrasah.user_id == user_id)
    return list((await session.execute(stmt)).scalars())


async def load_tugas(session: AsyncSession, user: UserMadrasah) -> UserMadrasah:
    rows = list((await session.execute(select(TugasMadrasah).where(TugasMadrasah.user_id == user.id))).scalars())
    user._tugas_list = rows
    if not user.madrasah_unit_id:
        for row in rows:
            if row.madrasah_unit_id:
                user.madrasah_unit_id = row.madrasah_unit_id
                break
    return user


async def backfill_tugas_dari_role(session: AsyncSession, user: UserMadrasah) -> None:
    if user.role in ("admin", "yayasan_admin", "wali_santri"):
        return
    if not user.madrasah_unit_id:
        return
    jenis = ROLE_KE_JENIS.get(user.role)
    if not jenis:
        return
    ada = (
        await session.execute(
            select(TugasMadrasah.id).where(
                TugasMadrasah.user_id == user.id,
                TugasMadrasah.madrasah_unit_id == user.madrasah_unit_id,
                TugasMadrasah.jenis == jenis,
            )
        )
    ).scalar_one_or_none()
    if ada:
        return
    session.add(TugasMadrasah(user_id=user.id, madrasah_unit_id=user.madrasah_unit_id, jenis=jenis))
    await session.flush()


async def create_tugas(session: AsyncSession, payload, caller: UserMadrasah) -> TugasMadrasah:
    unit_id = payload.madrasah_unit_id or caller.madrasah_unit_id
    if not unit_id:
        raise MadrasahForbiddenError("Unit wajib dipilih")
    boleh_menugaskan(caller, payload.jenis, unit_id)
    target = await session.get(UserMadrasah, payload.user_id)
    if not target:
        raise MadrasahNotFoundError("Akun tidak ditemukan")
    if target.role in ("admin", "yayasan_admin"):
        raise MadrasahForbiddenError("Admin Utama dan Admin Yayasan tidak memakai tugas unit")
    existing = (
        await session.execute(
            select(TugasMadrasah).where(
                TugasMadrasah.user_id == payload.user_id,
                TugasMadrasah.madrasah_unit_id == unit_id,
                TugasMadrasah.jenis == payload.jenis,
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    if payload.jenis == "kepala_sekolah":
        kepala_lain_user = (
            await session.execute(
                select(TugasMadrasah.id).where(
                    TugasMadrasah.user_id == payload.user_id,
                    TugasMadrasah.jenis == "kepala_sekolah",
                    TugasMadrasah.madrasah_unit_id != unit_id,
                )
            )
        ).scalar_one_or_none()
        if kepala_lain_user:
            raise MadrasahForbiddenError("Satu orang hanya boleh menjadi Kepala Madrasah di satu unit")
        kepala_unit_lain = (
            await session.execute(
                select(TugasMadrasah.id).where(
                    TugasMadrasah.madrasah_unit_id == unit_id,
                    TugasMadrasah.jenis == "kepala_sekolah",
                    TugasMadrasah.user_id != payload.user_id,
                )
            )
        ).scalar_one_or_none()
        if kepala_unit_lain:
            raise MadrasahForbiddenError("Unit ini sudah punya Kepala Madrasah")
    row = TugasMadrasah(user_id=payload.user_id, madrasah_unit_id=unit_id, jenis=payload.jenis)
    session.add(row)
    if payload.jenis in ("kepala_sekolah", "lembaga_admin"):
        unit = await session.get(MadrasahUnit, unit_id)
        if unit and payload.jenis == "kepala_sekolah":
            unit.kepala_unit = target.nama
        target.madrasah_unit_id = target.madrasah_unit_id or unit_id
    await session.flush()
    return row


async def delete_tugas(session: AsyncSession, tugas_id: str, caller: UserMadrasah) -> None:
    row = await session.get(TugasMadrasah, tugas_id)
    if not row:
        raise MadrasahNotFoundError("Tugas tidak ditemukan")
    boleh_menugaskan(caller, row.jenis, row.madrasah_unit_id)
    await session.delete(row)


def tugas_out(row: TugasMadrasah) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "user": row.user.nama if getattr(row, "user", None) else None,
        "jenis": row.jenis,
        "madrasah_unit_id": row.madrasah_unit_id,
    }


def _generate_password() -> str:
    return secrets.token_urlsafe(9)


async def update_profil_saya(session: AsyncSession, user: UserMadrasah, payload: ProfilPatch) -> UserMadrasah:
    """Self-service dari halaman "Profil Saya" -- BEDA dari patch_guru (yang
    dipakai admin mengedit akun orang lain): di sini user cuma boleh
    mengubah namanya sendiri dan menyertakan current_password yang benar
    kalau mau ganti password, tidak bisa mengubah role/no_hp/akun lain."""
    if payload.nama is not None:
        user.nama = payload.nama.strip()
    if payload.new_password:
        if not payload.current_password or not verify_password(payload.current_password, user.password_hash):
            raise MadrasahAuthError("Password saat ini salah")
        user.password_hash = hash_password(payload.new_password)
        user.session_version += 1
    await session.flush()
    return user


def tagihan_status_label(row: TagihanSyahriyah) -> str:
    if row.status_bayar:
        return STATUS_LUNAS
    if row.diajukan_oleh:
        return STATUS_MENUNGGU
    return STATUS_BELUM


def tagihan_out(row: TagihanSyahriyah, nama: str | None = None) -> dict:
    paid_at = row.dibayar_pada.isoformat() if row.dibayar_pada else None
    diajukan_at = row.diajukan_pada.isoformat() if row.diajukan_pada else None
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
        "diajukan_oleh": row.diajukan_oleh,
        "diajukan_pada": diajukan_at,
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


async def list_santri(
    session: AsyncSession,
    kelas_id: str | None = None,
    status: str | None = "aktif",
    unit_id: str | None = None,
) -> list[SantriMadrasah]:
    """status="aktif" (default) menyembunyikan santri lulus/keluar/pindah
    dari listing biasa (dropdown absensi, dsb) -- barisnya tetap ada di DB,
    cuma tidak ikut ditampilkan. status=None/"semua" menonaktifkan filter
    ini untuk layar admin yang memang butuh melihat semuanya (mis. daftar
    alumni). unit_id membatasi ke satu MadrasahUnit; None = semua unit."""
    stmt = select(SantriMadrasah).options(selectinload(SantriMadrasah.orang_tua)).order_by(SantriMadrasah.nama)
    if kelas_id:
        stmt = stmt.where((SantriMadrasah.kelas_id == kelas_id) | (SantriMadrasah.rombel_id == kelas_id))
    if status and status != "semua":
        stmt = stmt.where(SantriMadrasah.status == status)
    if unit_id:
        stmt = stmt.where(SantriMadrasah.madrasah_unit_id == unit_id)
    return list((await session.execute(stmt)).scalars())


async def bulk_insert_absensi(session: AsyncSession, payload: AbsenBulkRequest, guru: UserMadrasah | None = None) -> list[AbsensiMadrasah]:
    if guru:
        await assert_own_rombel_santri(session, guru, [item.santri_id for item in payload.items])
    semester_id = await _semester_aktif_id(session)
    rows: list[AbsensiMadrasah] = []
    for item in payload.items:
        row = AbsensiMadrasah(
            tanggal=payload.tanggal, status=item.status, santri_id=item.santri_id, guru_id=payload.guru_id, semester_id=semester_id
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows


async def assert_own_rombel_santri(session: AsyncSession, guru: UserMadrasah, santri_ids: list[str]) -> None:
    """Wali kelas cuma boleh input/edit absensi & progres untuk santri di
    rombel yang dia asuh sendiri (RombelMadrasah.wali_kelas_id == guru.id).
    Admin/kepala/admin lembaga bypass (perlu akses lintas kelas). Wali kelas
    lewat tugas tambahan juga terkunci ke rombel asuhannya."""
    if pengelola_lintas_rombel(guru):
        return
    if "wali_kelas" not in effective_roles(guru):
        raise MadrasahForbiddenError("Anda hanya dapat mengelola santri di rombel Anda sendiri")
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


async def assert_own_rombel(session: AsyncSession, guru: UserMadrasah, rombel_id: str) -> None:
    """Untuk endpoint yang menerima rombel_id langsung (bukan santri_id):
    pastikan rombel_id itu memang milik wali kelas yang bersangkutan."""
    if pengelola_lintas_rombel(guru):
        return
    if "wali_kelas" not in effective_roles(guru):
        raise MadrasahForbiddenError("Rombel ini bukan rombel Anda")
    rombel = await session.get(RombelMadrasah, rombel_id)
    if not rombel or rombel.wali_kelas_id != guru.id:
        raise MadrasahForbiddenError("Rombel ini bukan rombel Anda")


async def assert_guru_mengajar_santri(session: AsyncSession, guru: UserMadrasah, mapel_id: str, santri_id: str) -> None:
    """Untuk POST /guru-mapel/progres: pastikan guru benar-benar ditugaskan
    (GuruMapelRombel) mengajar mapel_id ini di rombel tempat santri_id berada
    -- sebelumnya cuma bulk_insert_absensi_mapel yang divalidasi begini,
    endpoint progres guru mapel masih bisa dipakai untuk santri di rombel
    manapun."""
    if guru.role in ("admin", "kepala_sekolah"):
        return
    santri = await session.get(SantriMadrasah, santri_id)
    if not santri or not santri.rombel_id:
        raise MadrasahForbiddenError("Santri tidak ditemukan di rombel manapun")
    penugasan = (
        await session.execute(
            select(GuruMapelRombel).where(
                GuruMapelRombel.guru_id == guru.id,
                GuruMapelRombel.mapel_id == mapel_id,
                GuruMapelRombel.rombel_id == santri.rombel_id,
            )
        )
    ).scalar_one_or_none()
    if not penugasan:
        raise MadrasahForbiddenError("Anda tidak ditugaskan mengajar mapel ini di rombel santri tersebut")


async def list_rombel_for_caller(
    session: AsyncSession, caller: UserMadrasah, requested_unit_id: str | None = None
) -> list[RombelMadrasah]:
    scope = resolve_unit_scope(caller, requested_unit_id)
    rows = await list_rombel(session, unit_id=scope)
    if not hanya_wali_kelas(caller):
        return rows
    return [r for r in rows if r.wali_kelas_id == caller.id]


async def list_santri_for_caller(session: AsyncSession, caller: UserMadrasah, kelas_id: str | None) -> list[SantriMadrasah]:
    """Untuk wali kelas: abaikan/timpa kelas_id yang dikirim client, paksa
    hanya rombel miliknya sendiri. Untuk wali santri: abaikan kelas_id sama
    sekali, paksa hanya anak yang orang_tua_id-nya cocok dengan akun ini --
    menutup celah wali santri melihat/memilih santri siapa pun di dropdown."""
    if caller.role == "wali_santri":
        stmt = select(SantriMadrasah).where(SantriMadrasah.orang_tua_id == caller.id).order_by(SantriMadrasah.nama)
        return list((await session.execute(stmt)).scalars())
    if not hanya_wali_kelas(caller):
        scope = resolve_unit_scope(caller)
        return await list_santri(session, kelas_id, unit_id=scope)
    own_rombel = list(
        (await session.execute(select(RombelMadrasah.id).where(RombelMadrasah.wali_kelas_id == caller.id))).scalars()
    )
    if not own_rombel:
        return []
    stmt = select(SantriMadrasah).where(SantriMadrasah.rombel_id.in_(own_rombel)).order_by(SantriMadrasah.nama)
    return list((await session.execute(stmt)).scalars())


async def assert_own_child(session: AsyncSession, caller: UserMadrasah, santri_id: str) -> None:
    if caller.role in ("admin", "kepala_sekolah"):
        return
    if caller.role != "wali_santri":
        return
    santri = await session.get(SantriMadrasah, santri_id)
    if not santri or santri.orang_tua_id != caller.id:
        raise MadrasahForbiddenError("Anda hanya dapat melihat data anak Anda sendiri")


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
        semester_id=await _semester_aktif_id(session),
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


async def list_tagihan_rombel(session: AsyncSession, rombel_id: str) -> list[TagihanSyahriyah]:
    stmt = (
        select(TagihanSyahriyah)
        .options(selectinload(TagihanSyahriyah.santri))
        .join(SantriMadrasah, TagihanSyahriyah.santri_id == SantriMadrasah.id)
        .where(SantriMadrasah.rombel_id == rombel_id)
        .order_by(TagihanSyahriyah.bulan_tahun.desc())
    )
    return list((await session.execute(stmt)).scalars())


async def list_tagihan_menunggu(session: AsyncSession, unit_id: str | None = None) -> list[TagihanSyahriyah]:
    stmt = (
        select(TagihanSyahriyah)
        .options(selectinload(TagihanSyahriyah.santri))
        .join(SantriMadrasah, TagihanSyahriyah.santri_id == SantriMadrasah.id)
        .where(TagihanSyahriyah.diajukan_oleh.is_not(None), TagihanSyahriyah.status_bayar.is_(False))
        .order_by(TagihanSyahriyah.diajukan_pada.asc())
    )
    if unit_id:
        stmt = stmt.where(SantriMadrasah.madrasah_unit_id == unit_id)
    return list((await session.execute(stmt)).scalars())


async def list_tagihan_semua(session: AsyncSession, bulan_tahun: str | None = None, unit_id: str | None = None) -> list[TagihanSyahriyah]:
    """Semua tagihan syahriyah (lunas maupun belum), dipakai bendahara untuk
    meninjau dan membersihkan tagihan yang salah generate -- beda dari
    list_tagihan_menunggu yang hanya menampilkan pengajuan pembayaran dari
    wali kelas."""
    stmt = (
        select(TagihanSyahriyah)
        .options(selectinload(TagihanSyahriyah.santri))
        .join(SantriMadrasah, TagihanSyahriyah.santri_id == SantriMadrasah.id)
        .order_by(TagihanSyahriyah.bulan_tahun.desc())
    )
    if bulan_tahun:
        stmt = stmt.where(TagihanSyahriyah.bulan_tahun == bulan_tahun)
    if unit_id:
        stmt = stmt.where(SantriMadrasah.madrasah_unit_id == unit_id)
    return list((await session.execute(stmt)).scalars())


async def delete_tagihan(session: AsyncSession, tagihan_id: str, caller: UserMadrasah | None = None) -> None:
    """Tagihan yang sudah lunas tidak boleh dihapus lewat sini -- angkanya
    sudah tercermin di buku kas/jurnal dan laporan laba rugi, jadi
    menghapusnya diam-diam akan membuat laporan keuangan tidak konsisten
    dengan riwayat pembayaran. Batalkan tagihan yang salah selagi masih
    belum dibayar."""
    row = await session.get(TagihanSyahriyah, tagihan_id)
    if not row:
        raise MadrasahNotFoundError("Tagihan tidak ditemukan")
    if row.status_bayar:
        raise MadrasahForbiddenError("Tidak bisa menghapus tagihan yang sudah lunas")
    santri = await session.get(SantriMadrasah, row.santri_id)
    _pastikan_akses_unit_keuangan(caller, santri.madrasah_unit_id if santri else None)
    await session.delete(row)


async def ajukan_pembayaran(session: AsyncSession, guru: UserMadrasah, tagihan_id: str) -> TagihanSyahriyah:
    row = await session.get(TagihanSyahriyah, tagihan_id)
    if not row:
        raise MadrasahNotFoundError("Tagihan tidak ditemukan")
    await assert_own_rombel_santri(session, guru, [row.santri_id])
    if row.status_bayar:
        raise MadrasahForbiddenError("Tagihan ini sudah lunas")
    row.diajukan_oleh = guru.id
    row.diajukan_pada = _utcnow()
    await session.flush()
    await session.refresh(row, attribute_names=["santri"])
    return row


async def get_pengaturan(session: AsyncSession) -> PengaturanSekolah:
    """Baris singleton -- dibuat otomatis kalau belum ada, supaya modul ini
    bisa dipasang di database yang sudah berjalan tanpa migrasi data manual.

    GET /pengaturan itu publik (dipanggil dari Login.jsx/Landing.jsx sebelum
    siapa pun login), jadi kalau tabelnya belum sempat dibuat lewat
    /seed-now setelah deploy, endpoint ini akan 500 buat SEMUA pengunjung,
    bukan cuma admin. Self-heal di sini (buat tabelnya sendiri kalau belum
    ada) supaya kelas bug ini tidak bisa terulang untuk tabel baru lain.
    """
    try:
        row = (await session.execute(select(PengaturanSekolah).limit(1))).scalar_one_or_none()
    except (ProgrammingError, OperationalError):
        await session.rollback()
        conn = await session.connection()
        await conn.run_sync(lambda sync_conn: PengaturanSekolah.__table__.create(sync_conn, checkfirst=True))
        row = None
    if not row:
        row = PengaturanSekolah()
        session.add(row)
        await session.flush()
    return row


async def update_pengaturan(session: AsyncSession, payload: PengaturanPatch) -> PengaturanSekolah:
    row = await get_pengaturan(session)
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(row, field, value)
    await session.flush()
    return row


def pengaturan_out(row: PengaturanSekolah) -> dict:
    return {
        "nama_sekolah": row.nama_sekolah,
        "tagline": row.tagline,
        "logo_url": row.logo_url,
        "alamat": row.alamat,
        "info_psb": row.info_psb,
        "kontak_psb": row.kontak_psb,
    }


def kegiatan_out(row: KegiatanMadrasah, unit_nama: str | None = None) -> dict:
    return {
        "id": row.id,
        "judul": row.judul,
        "deskripsi": row.deskripsi,
        "urutan": row.urutan,
        "madrasah_unit_id": row.madrasah_unit_id,
        "unit_nama": unit_nama,
    }


async def list_kegiatan(session: AsyncSession, unit_id: str | None = None) -> list[KegiatanMadrasah]:
    stmt = select(KegiatanMadrasah).order_by(KegiatanMadrasah.urutan, KegiatanMadrasah.dibuat_pada)
    if unit_id:
        stmt = stmt.where(KegiatanMadrasah.madrasah_unit_id == unit_id)
    return list((await session.execute(stmt)).scalars())


async def create_kegiatan(session: AsyncSession, payload: KegiatanIn, caller: UserMadrasah | None = None) -> KegiatanMadrasah:
    row = KegiatanMadrasah(
        judul=payload.judul,
        deskripsi=payload.deskripsi,
        urutan=payload.urutan,
        madrasah_unit_id=await assign_unit_id(session, caller, payload.madrasah_unit_id),
    )
    session.add(row)
    await session.flush()
    return row


async def delete_kegiatan(session: AsyncSession, kegiatan_id: str) -> None:
    row = await session.get(KegiatanMadrasah, kegiatan_id)
    if not row:
        raise MadrasahNotFoundError("Kegiatan tidak ditemukan")
    await session.delete(row)


def pendaftaran_out(row: PendaftaranSantri) -> dict:
    return {
        "id": row.id,
        "nama_calon": row.nama_calon,
        "tempat_lahir": row.tempat_lahir,
        "tanggal_lahir": row.tanggal_lahir.isoformat() if row.tanggal_lahir else None,
        "nama_orang_tua": row.nama_orang_tua,
        "no_hp": row.no_hp,
        "alamat": row.alamat,
        "asal_sekolah": row.asal_sekolah,
        "catatan": row.catatan,
        "status": row.status,
        "dibuat_pada": row.dibuat_pada.isoformat(),
        "madrasah_unit_id": row.madrasah_unit_id,
    }


async def create_pendaftaran(session: AsyncSession, payload: PendaftaranIn) -> PendaftaranSantri:
    row = PendaftaranSantri(**payload.model_dump())
    session.add(row)
    await session.flush()
    return row


async def list_pendaftaran(session: AsyncSession, unit_id: str | None = None) -> list[PendaftaranSantri]:
    stmt = select(PendaftaranSantri).order_by(PendaftaranSantri.dibuat_pada.desc())
    if unit_id:
        stmt = stmt.where(PendaftaranSantri.madrasah_unit_id == unit_id)
    return list((await session.execute(stmt)).scalars())


async def patch_pendaftaran(session: AsyncSession, pendaftaran_id: str, payload: PendaftaranPatch) -> PendaftaranSantri:
    row = await session.get(PendaftaranSantri, pendaftaran_id)
    if not row:
        raise MadrasahNotFoundError("Pendaftaran tidak ditemukan")
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(row, field, value)
    await session.flush()
    return row


async def nama_unit_map(session: AsyncSession, ids: list[str | None]) -> dict[str, str]:
    clean = [i for i in ids if i]
    if not clean:
        return {}
    rows = (await session.execute(select(MadrasahUnit.id, MadrasahUnit.nama).where(MadrasahUnit.id.in_(clean)))).all()
    return {i: n for i, n in rows}


async def list_pengumuman(
    session: AsyncSession, limit: int = 50, unit_id: str | None = None, hanya_publik: bool = False
) -> list[PengumumanMadrasah]:
    stmt = select(PengumumanMadrasah).order_by(PengumumanMadrasah.tanggal.desc()).limit(limit)
    if unit_id:
        stmt = stmt.where(PengumumanMadrasah.madrasah_unit_id == unit_id)
    if hanya_publik:
        stmt = stmt.where(PengumumanMadrasah.publik.is_(True))
    return list((await session.execute(stmt)).scalars())


def pengumuman_out(row: PengumumanMadrasah, unit_nama: str | None = None) -> dict:
    return {
        "id": row.id,
        "judul": row.judul,
        "isi": row.isi,
        "tanggal": row.tanggal.isoformat(),
        "dibuat_by": row.dibuat_by,
        "madrasah_unit_id": row.madrasah_unit_id,
        "unit_nama": unit_nama,
        "publik": bool(getattr(row, "publik", True)),
    }


async def generate_spp_massal(session: AsyncSession, unit_id: str | None = None, nominal: Decimal | None = None) -> list[TagihanSyahriyah]:
    # "ALTER TABLE ... ADD COLUMN IF NOT EXISTS" is Postgres-only syntax
    # (this is production self-heal for Neon) -- SQLite (used by the unit
    # test suite) doesn't understand it, and unlike the other self-heal
    # helpers in this file we can't rollback-and-retry here without
    # discarding whatever else the caller already flushed in this same
    # session/transaction. Just skip it outside Postgres.
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        await session.execute(text("ALTER TABLE IF EXISTS madrasah_tagihan_syahriyah ADD COLUMN IF NOT EXISTS dibayar_pada TIMESTAMPTZ NULL"))
        await session.execute(text("ALTER TABLE IF EXISTS madrasah_santri ADD COLUMN IF NOT EXISTS rombel_id VARCHAR(64) NULL"))
    period = _bulan_tahun()
    semester_id = await _semester_aktif_id(session)
    # Nominal per generate boleh diisi bendahara unit (tiap unit bisa beda
    # tarif SPP-nya) -- kalau tidak diisi, jatuh ke default global
    # (env SPP_NOMINAL) supaya perilaku lama tetap sama.
    nominal_final = nominal if nominal is not None else DEFAULT_SPP_NOMINAL
    santri_stmt = select(SantriMadrasah)
    if unit_id:
        santri_stmt = santri_stmt.where(SantriMadrasah.madrasah_unit_id == unit_id)
    santri_rows = list((await session.execute(santri_stmt)).scalars())
    existing = {r.santri_id for r in (await session.execute(select(TagihanSyahriyah).where(TagihanSyahriyah.bulan_tahun == period))).scalars()}
    for santri in santri_rows:
        if santri.id in existing:
            continue
        session.add(
            TagihanSyahriyah(
                bulan_tahun=period, nominal=nominal_final, status_bayar=False, santri_id=santri.id, semester_id=semester_id
            )
        )
    await session.flush()
    stmt = select(TagihanSyahriyah).options(selectinload(TagihanSyahriyah.santri)).where(TagihanSyahriyah.bulan_tahun == period)
    if unit_id:
        stmt = stmt.join(SantriMadrasah, TagihanSyahriyah.santri_id == SantriMadrasah.id).where(
            SantriMadrasah.madrasah_unit_id == unit_id
        )
    return list((await session.execute(stmt)).scalars())


async def pay_spp_manual(session: AsyncSession, target_id: str, caller: UserMadrasah | None = None) -> TagihanSyahriyah:
    row = await session.get(TagihanSyahriyah, target_id)
    if row is None:
        row = (await session.execute(select(TagihanSyahriyah).where(TagihanSyahriyah.santri_id == target_id, TagihanSyahriyah.status_bayar.is_(False)).order_by(TagihanSyahriyah.bulan_tahun.desc()))).scalar_one_or_none()
    if row is None:
        raise MadrasahNotFoundError("Tagihan SPP tidak ditemukan")
    if row.diajukan_oleh is None:
        raise MadrasahForbiddenError("Tagihan ini belum diajukan wali kelas -- tidak bisa langsung ditandai lunas")
    santri = await session.get(SantriMadrasah, row.santri_id)
    unit_id = santri.madrasah_unit_id if santri else None
    _pastikan_akses_unit_keuangan(caller, unit_id)
    row.status_bayar = True
    row.dibayar_pada = _utcnow()
    await session.flush()
    await session.refresh(row, attribute_names=["santri"])
    session.add(BukuKasMadrasah(
        tanggal=_utcnow().date(),
        tipe="masuk",
        kategori="SPP",
        jumlah=row.nominal,
        keterangan=f"SPP {row.santri.nama} -- {row.bulan_tahun}",
        madrasah_unit_id=unit_id,
    ))
    await catat_jurnal(
        session,
        tanggal=_utcnow().date(),
        akun_debit=AKUN_KAS,
        akun_kredit=AKUN_PENDAPATAN_SPP,
        jumlah=row.nominal,
        keterangan=f"SPP {row.santri.nama} -- {row.bulan_tahun}",
        sumber_tipe="spp",
        sumber_id=row.id,
        madrasah_unit_id=unit_id,
    )
    await session.flush()
    return row


async def list_buku_kas(session: AsyncSession, bulan: str | None = None, unit_id: str | None = None) -> list[BukuKasMadrasah]:
    # Filter bulan (format "YYYY-MM") di Python, bukan lewat fungsi tanggal
    # SQL yang beda nama antar dialek (strftime di SQLite vs to_char di
    # Postgres) -- volume baris buku kas per sekolah kecil, jadi ini murah.
    stmt = select(BukuKasMadrasah).order_by(BukuKasMadrasah.tanggal.desc(), BukuKasMadrasah.created_at.desc())
    if unit_id:
        stmt = stmt.where(BukuKasMadrasah.madrasah_unit_id == unit_id)
    rows = list((await session.execute(stmt)).scalars())
    if bulan:
        rows = [r for r in rows if r.tanggal.strftime("%Y-%m") == bulan]
    return rows


def _akun_lawan_kas(kategori: str, tipe: str) -> str:
    """Memetakan kategori bebas (field teks BukuKasIn.kategori) ke akun COA
    tetap, supaya setiap baris buku kas otomatis punya pasangan jurnal yang
    masuk akal tanpa memaksa pencatat kas memilih kode akun sendiri.
    Kategori yang tidak dikenali jatuh ke akun "lain-lain" sesuai tipenya --
    tidak pernah gagal, cuma kurang rinci di laporan laba-rugi."""
    key = (kategori or "").strip().lower()
    if tipe == "masuk":
        if "spp" in key or "syahriyah" in key:
            return AKUN_PENDAPATAN_SPP
        return AKUN_PENDAPATAN_LAIN
    if "atk" in key:
        return AKUN_BEBAN_ATK
    if "honor" in key:
        return AKUN_BEBAN_HONOR
    return AKUN_BEBAN_LAIN


async def create_buku_kas_entry(
    session: AsyncSession, payload: BukuKasIn, dicatat_oleh: str, unit_id: str | None = None
) -> BukuKasMadrasah:
    row = BukuKasMadrasah(
        tanggal=payload.tanggal,
        tipe=payload.tipe,
        kategori=payload.kategori,
        jumlah=payload.jumlah,
        keterangan=payload.keterangan,
        dicatat_oleh=dicatat_oleh,
        madrasah_unit_id=unit_id,
    )
    session.add(row)
    await session.flush()
    akun_lawan = _akun_lawan_kas(payload.kategori, payload.tipe)
    await catat_jurnal(
        session,
        tanggal=payload.tanggal,
        akun_debit=AKUN_KAS if payload.tipe == "masuk" else akun_lawan,
        akun_kredit=akun_lawan if payload.tipe == "masuk" else AKUN_KAS,
        jumlah=payload.jumlah,
        keterangan=payload.keterangan or payload.kategori,
        sumber_tipe="buku_kas",
        sumber_id=row.id,
        dibuat_oleh=dicatat_oleh,
        madrasah_unit_id=unit_id,
    )
    return row


async def patch_buku_kas_entry(
    session: AsyncSession, entry_id: str, payload: BukuKasIn, caller: UserMadrasah | None = None
) -> BukuKasMadrasah:
    row = await session.get(BukuKasMadrasah, entry_id)
    if not row:
        raise MadrasahNotFoundError("Baris buku kas tidak ditemukan")
    _pastikan_akses_unit_keuangan(caller, row.madrasah_unit_id)
    row.tanggal = payload.tanggal
    row.tipe = payload.tipe
    row.kategori = payload.kategori
    row.jumlah = payload.jumlah
    row.keterangan = payload.keterangan
    await session.flush()
    return row


async def delete_buku_kas_entry(session: AsyncSession, entry_id: str, caller: UserMadrasah | None = None) -> None:
    row = await session.get(BukuKasMadrasah, entry_id)
    if not row:
        raise MadrasahNotFoundError("Baris buku kas tidak ditemukan")
    _pastikan_akses_unit_keuangan(caller, row.madrasah_unit_id)
    await session.delete(row)


def buku_kas_out(row: BukuKasMadrasah) -> dict:
    return {
        "id": row.id,
        "tanggal": row.tanggal.isoformat(),
        "tipe": row.tipe,
        "kategori": row.kategori,
        "jumlah": str(row.jumlah),
        "keterangan": row.keterangan,
    }


async def laporan_keuangan(session: AsyncSession, bulan: str | None = None, unit_id: str | None = None) -> dict:
    rows = await list_buku_kas(session, bulan, unit_id=unit_id)
    total_masuk = sum((r.jumlah for r in rows if r.tipe == "masuk"), Decimal("0"))
    total_keluar = sum((r.jumlah for r in rows if r.tipe == "keluar"), Decimal("0"))
    return {
        "total_masuk": str(total_masuk),
        "total_keluar": str(total_keluar),
        "saldo": str(total_masuk - total_keluar),
        "entries": [buku_kas_out(r) for r in rows],
    }


# --- Chart of Accounts & jurnal double-entry (Fase 2.1) ---

async def seed_akun_default(session: AsyncSession) -> None:
    """Idempoten: hanya menambah kode akun yang belum ada, tidak pernah
    menimpa `nama`/`tipe` akun yang sudah ada (kalau admin pernah
    mengubahnya secara manual lewat DB). Dipanggil dari
    seeder.ensure_madrasah_schema() setiap startup."""
    existing = set((await session.execute(select(AkunMadrasah.kode))).scalars())
    for kode, nama, tipe in AKUN_DEFAULT:
        if kode not in existing:
            session.add(AkunMadrasah(kode=kode, nama=nama, tipe=tipe))
    await session.flush()


async def list_akun(session: AsyncSession) -> list[AkunMadrasah]:
    return list((await session.execute(select(AkunMadrasah).order_by(AkunMadrasah.kode))).scalars())


def akun_out(row: AkunMadrasah) -> dict:
    return {"kode": row.kode, "nama": row.nama, "tipe": row.tipe}


async def catat_jurnal(
    session: AsyncSession,
    *,
    tanggal: date,
    akun_debit: str,
    akun_kredit: str,
    jumlah: Decimal,
    keterangan: str = "",
    sumber_tipe: str = "",
    sumber_id: str | None = None,
    dibuat_oleh: str | None = None,
    madrasah_unit_id: str | None = None,
) -> JurnalMadrasah:
    row = JurnalMadrasah(
        tanggal=tanggal,
        akun_debit=akun_debit,
        akun_kredit=akun_kredit,
        jumlah=jumlah,
        keterangan=keterangan,
        sumber_tipe=sumber_tipe,
        sumber_id=sumber_id,
        dibuat_oleh=dibuat_oleh,
        madrasah_unit_id=madrasah_unit_id,
    )
    session.add(row)
    await session.flush()
    return row


def jurnal_out(row: JurnalMadrasah) -> dict:
    return {
        "id": row.id,
        "tanggal": row.tanggal.isoformat(),
        "akun_debit": row.akun_debit,
        "akun_kredit": row.akun_kredit,
        "jumlah": str(row.jumlah),
        "keterangan": row.keterangan,
        "sumber_tipe": row.sumber_tipe,
        "sumber_id": row.sumber_id,
    }


async def list_jurnal(session: AsyncSession, bulan: str | None = None, unit_id: str | None = None) -> list[JurnalMadrasah]:
    stmt = select(JurnalMadrasah).order_by(JurnalMadrasah.tanggal.desc(), JurnalMadrasah.created_at.desc())
    if unit_id:
        stmt = stmt.where(JurnalMadrasah.madrasah_unit_id == unit_id)
    rows = list((await session.execute(stmt)).scalars())
    if bulan:
        rows = [r for r in rows if r.tanggal.strftime("%Y-%m") == bulan]
    return rows


async def laba_rugi(session: AsyncSession, bulan: str | None = None, unit_id: str | None = None) -> dict:
    """Laba-rugi sederhana: setiap baris jurnal menambah SALDO akun
    kreditnya dan mengurangi saldo akun debitnya (konvensi normal
    akuntansi), lalu akun tipe pendapatan/beban diringkas per akun."""
    rows = await list_jurnal(session, bulan, unit_id=unit_id)
    akun_map = {a.kode: a for a in await list_akun(session)}
    saldo: dict[str, Decimal] = {}
    for r in rows:
        saldo[r.akun_kredit] = saldo.get(r.akun_kredit, Decimal("0")) + r.jumlah
        saldo[r.akun_debit] = saldo.get(r.akun_debit, Decimal("0")) - r.jumlah

    pendapatan = []
    beban = []
    total_pendapatan = Decimal("0")
    total_beban = Decimal("0")
    for kode, jumlah in saldo.items():
        akun = akun_map.get(kode)
        if not akun:
            continue
        if akun.tipe == "pendapatan" and jumlah > 0:
            pendapatan.append({"kode": kode, "nama": akun.nama, "jumlah": str(jumlah)})
            total_pendapatan += jumlah
        elif akun.tipe == "beban" and jumlah < 0:
            beban.append({"kode": kode, "nama": akun.nama, "jumlah": str(-jumlah)})
            total_beban += -jumlah
    return {
        "pendapatan": pendapatan,
        "beban": beban,
        "total_pendapatan": str(total_pendapatan),
        "total_beban": str(total_beban),
        "laba_bersih": str(total_pendapatan - total_beban),
    }


async def list_tingkat(session: AsyncSession, unit_id: str | None = None) -> list[TingkatMadrasah]:
    stmt = select(TingkatMadrasah).order_by(TingkatMadrasah.urutan)
    if unit_id:
        stmt = stmt.where(TingkatMadrasah.madrasah_unit_id == unit_id)
    return list((await session.execute(stmt)).scalars())


async def create_tingkat(
    session: AsyncSession, payload: TingkatIn, caller: UserMadrasah | None = None
) -> TingkatMadrasah:
    row = TingkatMadrasah(
        nama=payload.nama,
        urutan=payload.urutan,
        madrasah_unit_id=await assign_unit_id(session, caller, payload.madrasah_unit_id),
    )
    session.add(row)
    await session.flush()
    return row


async def list_rombel(session: AsyncSession, unit_id: str | None = None) -> list[RombelMadrasah]:
    stmt = select(RombelMadrasah).options(selectinload(RombelMadrasah.wali_kelas), selectinload(RombelMadrasah.tingkat)).order_by(RombelMadrasah.nama)
    if unit_id:
        stmt = stmt.where(RombelMadrasah.madrasah_unit_id == unit_id)
    return list((await session.execute(stmt)).scalars())


async def create_rombel(
    session: AsyncSession, payload: RombelIn, caller: UserMadrasah | None = None
) -> RombelMadrasah:
    row = RombelMadrasah(
        nama=payload.nama,
        tingkat_id=payload.tingkat_id,
        wali_kelas_id=payload.wali_kelas_id,
        madrasah_unit_id=await assign_unit_id(session, caller, payload.madrasah_unit_id),
    )
    session.add(row)
    await session.flush()
    if not (await session.execute(select(KelasMadrasah).where(KelasMadrasah.nama_kelas == payload.nama))).scalar_one_or_none():
        session.add(KelasMadrasah(id=row.id, nama_kelas=payload.nama))
        await session.flush()
    return row


async def patch_rombel(session: AsyncSession, rombel_id: str, payload: RombelPatch) -> RombelMadrasah:
    row = await session.get(RombelMadrasah, rombel_id)
    if not row:
        raise MadrasahNotFoundError("Rombel tidak ditemukan")
    if payload.nama is not None:
        row.nama = payload.nama
    if payload.tingkat_id is not None:
        row.tingkat_id = payload.tingkat_id
    if payload.wali_kelas_id is not None:
        row.wali_kelas_id = payload.wali_kelas_id
    await session.flush()
    return row


async def patch_tingkat(session: AsyncSession, tingkat_id: str, payload: TingkatPatch) -> TingkatMadrasah:
    row = await session.get(TingkatMadrasah, tingkat_id)
    if not row:
        raise MadrasahNotFoundError("Tingkat tidak ditemukan")
    if payload.nama is not None:
        row.nama = payload.nama
    if payload.urutan is not None:
        row.urutan = payload.urutan
    await session.flush()
    return row


async def delete_tingkat(session: AsyncSession, tingkat_id: str) -> None:
    row = await session.get(TingkatMadrasah, tingkat_id)
    if not row:
        raise MadrasahNotFoundError("Tingkat tidak ditemukan")
    await session.delete(row)


async def delete_rombel(session: AsyncSession, rombel_id: str) -> None:
    row = await session.get(RombelMadrasah, rombel_id)
    if not row:
        raise MadrasahNotFoundError("Rombel tidak ditemukan")
    await session.delete(row)


async def list_guru(session: AsyncSession, unit_id: str | None = None) -> list[UserMadrasah]:
    stmt = select(UserMadrasah).where(UserMadrasah.role.in_(STAF_UNIT_ROLES)).order_by(UserMadrasah.nama)
    if unit_id:
        stmt = stmt.where(UserMadrasah.madrasah_unit_id == unit_id)
    return list((await session.execute(stmt)).scalars())


async def list_wali_santri(session: AsyncSession, unit_id: str | None = None) -> list[UserMadrasah]:
    stmt = select(UserMadrasah).where(UserMadrasah.role == "wali_santri").order_by(UserMadrasah.nama)
    if unit_id:
        stmt = stmt.where(UserMadrasah.madrasah_unit_id == unit_id)
    return list((await session.execute(stmt)).scalars())


async def list_semua_akun(session: AsyncSession, unit_id: str | None = None) -> list[UserMadrasah]:
    """Untuk halaman "Kelola Akun" (admin aplikasi only): SEMUA akun tanpa
    filter role -- beda dari list_guru/list_wali_santri yang masing-masing
    cuma menampilkan sebagian role. Ini satu-satunya tempat admin/
    kepala_sekolah/yayasan_admin sendiri bisa dilihat & dikelola lewat UI;
    create_guru/patch_guru/delete_guru dipakai apa adanya (generik, tidak
    dibatasi role tertentu) untuk operasinya."""
    stmt = select(UserMadrasah).order_by(UserMadrasah.role, UserMadrasah.nama)
    if unit_id:
        stmt = stmt.where(UserMadrasah.madrasah_unit_id == unit_id)
    return list((await session.execute(stmt)).scalars())


STAF_UNIT_ROLES = ("guru", "wali_kelas", "kepala_sekolah", "lembaga_admin", "kurikulum", "bendahara")
PENGELOLA_UNIT = frozenset({"kepala_sekolah", "lembaga_admin"})
PEGAWAI_UNIT = frozenset({"kurikulum", "bendahara", "wali_kelas", "guru", "wali_santri"})
# Kepala boleh menunjuk admin lembaga sebagai cadangan. Admin lembaga
# hanya mengurus pegawai, tidak boleh membuat kepala atau admin lembaga lain.
AKUN_BOLEH_DIKELOLA = {
    "kepala_sekolah": PEGAWAI_UNIT | frozenset({"lembaga_admin"}),
    "lembaga_admin": PEGAWAI_UNIT,
}


def _pastikan_pengelola_unit(caller: UserMadrasah | None, role: str, unit_id: str | None) -> None:
    if caller is None or caller.role not in PENGELOLA_UNIT:
        return
    if role not in AKUN_BOLEH_DIKELOLA[caller.role]:
        raise MadrasahForbiddenError("Peran ini hanya boleh mengelola akun pegawai di unitnya")
    if not caller.madrasah_unit_id or unit_id != caller.madrasah_unit_id:
        raise MadrasahForbiddenError("Akun Kepala/Admin Lembaga belum terikat unit, atau unit tidak sama")


async def create_guru(
    session: AsyncSession, payload: GuruIn, caller: UserMadrasah | None = None
) -> tuple[UserMadrasah, str | None]:
    """Return (row, password_sementara) -- password_sementara diisi hanya
    kalau payload.password kosong (server yang membuatkan password acak),
    None kalau admin sudah menentukan passwordnya sendiri."""
    generated = None if payload.password else _generate_password()
    role = payload.role or "wali_kelas"
    if caller is not None and caller.role in PENGELOLA_UNIT:
        unit_id = caller.madrasah_unit_id
    else:
        unit_id = await _unit_untuk_akun_baru(session, caller, role, payload.madrasah_unit_id)
    _pastikan_pengelola_unit(caller, role, unit_id)
    row = UserMadrasah(
        nama=payload.nama,
        no_hp=payload.no_hp.strip(),
        password_hash=hash_password(payload.password or generated),
        role=role,
        madrasah_unit_id=unit_id,
    )
    session.add(row)
    await session.flush()
    return row, generated


async def _count_admin(session: AsyncSession, exclude_id: str | None = None) -> int:
    stmt = select(func.count()).select_from(UserMadrasah).where(UserMadrasah.role == "admin")
    if exclude_id:
        stmt = stmt.where(UserMadrasah.id != exclude_id)
    return (await session.execute(stmt)).scalar_one()


async def patch_guru(session: AsyncSession, user_id: str, payload: UserPatch, caller: UserMadrasah | None = None) -> UserMadrasah:
    row = await session.get(UserMadrasah, user_id)
    if not row:
        raise MadrasahNotFoundError("Akun tidak ditemukan")
    _pastikan_pengelola_unit(caller, row.role, row.madrasah_unit_id)
    if payload.role is not None:
        _pastikan_pengelola_unit(caller, payload.role, caller.madrasah_unit_id if caller and caller.role in PENGELOLA_UNIT else row.madrasah_unit_id)
    if payload.nama is not None:
        row.nama = payload.nama
    if payload.no_hp is not None:
        row.no_hp = payload.no_hp.strip()
    if payload.role is not None:
        if row.role == "admin" and payload.role != "admin" and await _count_admin(session, exclude_id=user_id) == 0:
            # Satu-satunya admin aplikasi yang tersisa -- turunkan rolenya
            # akan mengunci SEMUA orang keluar dari Kelola Akun (endpoint
            # itu eksklusif APP_ADMIN_ROLES = role "admin"), tidak ada jalan
            # baliknya lewat UI sama sekali.
            raise MadrasahForbiddenError("Tidak bisa mengubah role admin terakhir")
        row.role = payload.role
    if payload.password:
        row.password_hash = hash_password(payload.password)
    target_role = payload.role or row.role
    if caller is not None and caller.role in PENGELOLA_UNIT:
        row.madrasah_unit_id = caller.madrasah_unit_id
    elif "madrasah_unit_id" in payload.model_fields_set or target_role in ("admin", "yayasan_admin"):
        # Admin utama dan admin yayasan tidak terikat satu unit.
        row.madrasah_unit_id = None if target_role in ("admin", "yayasan_admin") else payload.madrasah_unit_id
    if payload.role is not None or payload.password:
        # Password atau role berubah -> setiap JWT yang sudah beredar untuk
        # akun ini (termasuk yang bocor) langsung ditolak di request
        # berikutnya, lihat get_current_user_madrasah.
        row.session_version += 1
    await session.flush()
    return row


async def delete_guru(session: AsyncSession, user_id: str, caller: UserMadrasah | None = None) -> None:
    """Hapus akun guru/wali_kelas/wali_santri/dst.

    Dilakukan lewat UPDATE/DELETE eksplisit (bukan cuma mengandalkan
    ON DELETE SET NULL/CASCADE di DB) karena tabel-tabel lama di database
    produksi bisa saja sudah dibuat sebelum aturan ondelete itu ada di
    models.py -- constraint FK aslinya di Postgres masih RESTRICT, jadi
    session.delete(row) langsung akan gagal dengan IntegrityError kalau
    akun ini masih dirujuk di mana pun (rombel yang diasuh, absensi yang
    dicatat, dsb). Meng-update dulu FK-nya ke NULL/hapus barisnya di sini
    membuat penghapusan akun aman apa pun kondisi constraint di DB.
    """
    row = await session.get(UserMadrasah, user_id)
    if not row:
        raise MadrasahNotFoundError("Akun tidak ditemukan")
    _pastikan_pengelola_unit(caller, row.role, row.madrasah_unit_id)
    if row.role == "admin" and await _count_admin(session, exclude_id=user_id) == 0:
        raise MadrasahForbiddenError("Tidak bisa menghapus admin terakhir")

    await session.execute(update(RombelMadrasah).where(RombelMadrasah.wali_kelas_id == user_id).values(wali_kelas_id=None))
    await session.execute(update(SantriMadrasah).where(SantriMadrasah.orang_tua_id == user_id).values(orang_tua_id=None))
    await session.execute(update(AbsensiMadrasah).where(AbsensiMadrasah.guru_id == user_id).values(guru_id=None))
    await session.execute(update(PengumumanMadrasah).where(PengumumanMadrasah.dibuat_by == user_id).values(dibuat_by=None))
    await session.execute(update(BukuKasMadrasah).where(BukuKasMadrasah.dicatat_oleh == user_id).values(dicatat_oleh=None))
    await session.execute(update(TagihanSyahriyah).where(TagihanSyahriyah.diajukan_oleh == user_id).values(diajukan_oleh=None))
    await session.execute(delete(GuruMapelRombel).where(GuruMapelRombel.guru_id == user_id))
    await session.execute(delete(PesanMadrasah).where(PesanMadrasah.dari_user_id == user_id))
    await session.flush()

    await session.delete(row)


async def create_wali_santri(
    session: AsyncSession, payload: GuruIn, caller: UserMadrasah | None = None
) -> tuple[UserMadrasah, str | None]:
    generated = None if payload.password else _generate_password()
    unit_id = payload.madrasah_unit_id or await _default_unit_id(session)
    if caller is not None and caller.role in PENGELOLA_UNIT:
        unit_id = caller.madrasah_unit_id
    _pastikan_pengelola_unit(caller, "wali_santri", unit_id)
    row = UserMadrasah(
        nama=payload.nama,
        no_hp=payload.no_hp.strip(),
        password_hash=hash_password(payload.password or generated),
        role="wali_santri",
        madrasah_unit_id=unit_id,
    )
    session.add(row)
    await session.flush()
    return row, generated


async def _tutup_riwayat_terbuka(session: AsyncSession, santri_id: str, tanggal: date | None = None) -> None:
    await session.execute(
        update(RiwayatPenempatanSantri)
        .where(RiwayatPenempatanSantri.santri_id == santri_id, RiwayatPenempatanSantri.tanggal_keluar.is_(None))
        .values(tanggal_keluar=tanggal or date.today())
    )


async def _catat_riwayat_penempatan(session: AsyncSession, santri_id: str, rombel_id: str) -> None:
    """Menutup baris riwayat yang masih terbuka (kalau ada) untuk santri
    ini, lalu membuka baris baru untuk rombel_id -- dipanggil setiap kali
    rombel santri berubah (penempatan awal, pindah kelas, kenaikan kelas),
    supaya RiwayatPenempatanSantri selalu punya jejak lengkap, bukan cuma
    posisi terakhir seperti SantriMadrasah.rombel_id."""
    await _tutup_riwayat_terbuka(session, santri_id)
    session.add(
        RiwayatPenempatanSantri(
            santri_id=santri_id, rombel_id=rombel_id, semester_id=await _semester_aktif_id(session)
        )
    )
    await session.flush()


async def riwayat_kelas_santri(session: AsyncSession, santri_id: str) -> list[dict]:
    stmt = (
        select(RiwayatPenempatanSantri)
        .options(selectinload(RiwayatPenempatanSantri.rombel))
        .where(RiwayatPenempatanSantri.santri_id == santri_id)
        .order_by(RiwayatPenempatanSantri.tanggal_masuk.desc())
    )
    rows = list((await session.execute(stmt)).scalars())
    return [
        {
            "id": r.id,
            "rombel_id": r.rombel_id,
            "rombel": r.rombel.nama if r.rombel else None,
            "tanggal_masuk": r.tanggal_masuk.isoformat(),
            "tanggal_keluar": r.tanggal_keluar.isoformat() if r.tanggal_keluar else None,
        }
        for r in rows
    ]


async def place_santri(session: AsyncSession, payload: PlacementIn) -> SantriMadrasah:
    santri = await session.get(SantriMadrasah, payload.santri_id)
    rombel = await session.get(RombelMadrasah, payload.rombel_id)
    if not santri or not rombel:
        raise MadrasahNotFoundError("Santri atau rombel tidak ditemukan")
    santri.rombel_id = rombel.id
    santri.kelas_id = rombel.id
    await _catat_riwayat_penempatan(session, santri.id, rombel.id)
    return santri


async def _cari_atau_buat_wali(
    session: AsyncSession,
    nama_wali: str,
    unit_id: str | None,
    caller: UserMadrasah | None,
    no_hp_wali: str | None = None,
    tanggal_lahir_santri: date | None = None,
) -> tuple[UserMadrasah, str | None]:
    nama = nama_wali.strip()
    stmt = select(UserMadrasah).where(
        UserMadrasah.role == "wali_santri",
        func.lower(UserMadrasah.nama) == nama.lower(),
    )
    if unit_id:
        stmt = stmt.where(UserMadrasah.madrasah_unit_id == unit_id)
    existing = (await session.execute(stmt)).scalars().first()
    if existing:
        return existing, None
    # Password default = tanggal lahir santri (ddmmyy) supaya admin bisa
    # menjawab saat wali lupa/menanyakan akunnya -- password acak sebelumnya
    # tidak pernah tersimpan/ditampilkan lagi setelah notifikasi awal hilang.
    generated = tanggal_lahir_santri.strftime("%d%m%y") if tanggal_lahir_santri else _generate_password()
    slug = re.sub(r"[^a-z0-9]+", "", nama.lower())[:12] or "wali"
    username = (no_hp_wali or "").strip() or f"{slug}.{secrets.token_hex(2)}"
    row = UserMadrasah(
        nama=nama,
        no_hp=username,
        password_hash=hash_password(generated),
        role="wali_santri",
        madrasah_unit_id=unit_id,
    )
    session.add(row)
    await session.flush()
    return row, generated


async def create_santri(
    session: AsyncSession, payload: SantriIn, caller: UserMadrasah | None = None
) -> tuple[SantriMadrasah, str | None, str | None]:
    unit_id = await assign_unit_id(session, caller, payload.madrasah_unit_id)
    wali_password = None
    wali_username = None
    orang_tua_id = payload.orang_tua_id
    if not orang_tua_id and payload.nama_wali:
        wali, wali_password = await _cari_atau_buat_wali(
            session, payload.nama_wali, unit_id, caller, payload.no_hp_wali, payload.tanggal_lahir
        )
        orang_tua_id = wali.id
        wali_username = wali.no_hp
    row = SantriMadrasah(
        nama=payload.nama,
        rombel_id=payload.rombel_id,
        kelas_id=payload.kelas_id or payload.rombel_id,
        orang_tua_id=orang_tua_id,
        madrasah_unit_id=unit_id,
        nik=payload.nik,
        tempat_lahir=payload.tempat_lahir,
        tanggal_lahir=payload.tanggal_lahir,
        jenis_kelamin=payload.jenis_kelamin,
        agama=payload.agama,
        status_dalam_keluarga=payload.status_dalam_keluarga,
        alamat_lengkap=payload.alamat_lengkap,
        nomor_kk=payload.nomor_kk,
        nama_ayah=payload.nama_ayah,
        nama_ibu=payload.nama_ibu,
        rt_rw=payload.rt_rw,
        kode_pos=payload.kode_pos,
        desa_kelurahan=payload.desa_kelurahan,
        kecamatan=payload.kecamatan,
        kabupaten_kota=payload.kabupaten_kota,
        provinsi=payload.provinsi,
    )
    session.add(row)
    await session.flush()
    if payload.rombel_id:
        await _catat_riwayat_penempatan(session, row.id, payload.rombel_id)
    return row, wali_username, wali_password


async def patch_santri(
    session: AsyncSession, santri_id: str, payload: SantriPatch
) -> tuple[SantriMadrasah, str | None, str | None]:
    row = await session.get(SantriMadrasah, santri_id)
    if not row:
        raise MadrasahNotFoundError("Santri tidak ditemukan")
    if payload.nama is not None:
        row.nama = payload.nama
    if payload.rombel_id is not None:
        row.rombel_id = payload.rombel_id
        row.kelas_id = payload.rombel_id
        await _catat_riwayat_penempatan(session, santri_id, payload.rombel_id)
    if payload.orang_tua_id is not None:
        row.orang_tua_id = payload.orang_tua_id or None
    wali_username: str | None = None
    wali_password: str | None = None
    if payload.nama_wali:
        # Cari/buat wali di unit yang sama dengan santri ini -- dipakai untuk
        # mengikat wali dari nama ayah/ibu hasil OCR KK setelah santri sudah
        # tersimpan (lihat AdminSantriPage: dropdown Wali Santri).
        wali, wali_password = await _cari_atau_buat_wali(
            session, payload.nama_wali, row.madrasah_unit_id, None, payload.no_hp_wali, row.tanggal_lahir
        )
        row.orang_tua_id = wali.id
        wali_username = wali.no_hp
    if payload.nik is not None:
        row.nik = payload.nik
    if payload.tempat_lahir is not None:
        row.tempat_lahir = payload.tempat_lahir
    if payload.tanggal_lahir is not None:
        row.tanggal_lahir = payload.tanggal_lahir
    if payload.jenis_kelamin is not None:
        row.jenis_kelamin = payload.jenis_kelamin
    if payload.agama is not None:
        row.agama = payload.agama
    if payload.status_dalam_keluarga is not None:
        row.status_dalam_keluarga = payload.status_dalam_keluarga
    if payload.alamat_lengkap is not None:
        row.alamat_lengkap = payload.alamat_lengkap
    if payload.nomor_kk is not None:
        row.nomor_kk = payload.nomor_kk
    if payload.nama_ayah is not None:
        row.nama_ayah = payload.nama_ayah
    if payload.nama_ibu is not None:
        row.nama_ibu = payload.nama_ibu
    if payload.rt_rw is not None:
        row.rt_rw = payload.rt_rw
    if payload.kode_pos is not None:
        row.kode_pos = payload.kode_pos
    if payload.desa_kelurahan is not None:
        row.desa_kelurahan = payload.desa_kelurahan
    if payload.kecamatan is not None:
        row.kecamatan = payload.kecamatan
    if payload.kabupaten_kota is not None:
        row.kabupaten_kota = payload.kabupaten_kota
    if payload.provinsi is not None:
        row.provinsi = payload.provinsi
    await session.flush()
    return row, wali_username, wali_password


async def set_status_santri(session: AsyncSession, santri_id: str, payload: SantriStatusIn) -> SantriMadrasah:
    """Menandai santri lulus/keluar/pindah (atau mengaktifkan kembali santri
    yang sebelumnya keluar). Baris santri TIDAK dihapus -- data historis
    (absensi, progres, tagihan, riwayat kelas) tetap tersimpan; santri
    hanya berhenti muncul di listing default (lihat list_santri)."""
    row = await session.get(SantriMadrasah, santri_id)
    if not row:
        raise MadrasahNotFoundError("Santri tidak ditemukan")
    row.status = payload.status
    row.tanggal_status = payload.tanggal or date.today()
    if payload.status != "aktif":
        await _tutup_riwayat_terbuka(session, santri_id, row.tanggal_status)
    await session.flush()
    return row


async def kenaikan_kelas_massal(session: AsyncSession, payload: KenaikanKelasRequest, caller: UserMadrasah | None = None) -> dict:
    """Proses satu batch kenaikan kelas: tiap item pindah rombel (dicatat ke
    riwayat lewat _catat_riwayat_penempatan) atau, kalau rombel_tujuan_id
    kosong, ditandai lulus (tanggal hari ini, riwayat ditutup). Santri yang
    tidak disebutkan dalam payload tidak tersentuh sama sekali."""
    if caller is not None and hanya_wali_kelas(caller):
        await assert_own_rombel_santri(session, caller, [item.santri_id for item in payload.items])
    dipindah: list[str] = []
    diluluskan: list[str] = []
    for item in payload.items:
        santri = await session.get(SantriMadrasah, item.santri_id)
        if not santri:
            raise MadrasahNotFoundError(f"Santri {item.santri_id} tidak ditemukan")
        if item.rombel_tujuan_id:
            rombel = await session.get(RombelMadrasah, item.rombel_tujuan_id)
            if not rombel:
                raise MadrasahNotFoundError(f"Rombel tujuan {item.rombel_tujuan_id} tidak ditemukan")
            santri.rombel_id = rombel.id
            santri.kelas_id = rombel.id
            await _catat_riwayat_penempatan(session, santri.id, rombel.id)
            dipindah.append(santri.id)
        else:
            santri.status = "lulus"
            santri.tanggal_status = date.today()
            await _tutup_riwayat_terbuka(session, santri.id, santri.tanggal_status)
            diluluskan.append(santri.id)
    await session.flush()
    return {"dipindah": dipindah, "diluluskan": diluluskan}


async def delete_santri(session: AsyncSession, santri_id: str, caller: UserMadrasah | None = None) -> None:
    """Hapus permanen data santri beserta riwayat penempatan/absensi/progres
    hafalan/pesan yang menunjuk ke santri ini.

    Dilakukan lewat DELETE eksplisit (bukan cuma mengandalkan ON DELETE
    CASCADE di DB) karena tabel-tabel lama di database produksi bisa saja
    sudah dibuat sebelum aturan ondelete itu ada di models.py -- constraint
    FK aslinya di Postgres masih RESTRICT, jadi session.delete(row) langsung
    bisa gagal dengan IntegrityError (500) kalau santri ini masih dirujuk di
    mana pun. Sama seperti delete_guru().

    Tagihan syahriyah yang belum lunas ikut dihapus. Selain Admin Utama,
    santri yang SUDAH lunas tidak boleh dihapus -- angkanya sudah tercermin
    di buku kas. Admin Utama boleh paksa hapus beserta tagihannya.
    """
    row = await session.get(SantriMadrasah, santri_id)
    if not row:
        raise MadrasahNotFoundError("Santri tidak ditemukan")

    lunas_count = (
        await session.execute(
            select(func.count())
            .select_from(TagihanSyahriyah)
            .where(TagihanSyahriyah.santri_id == santri_id, TagihanSyahriyah.status_bayar.is_(True))
        )
    ).scalar_one()
    admin_utama = caller is not None and (caller.role == "admin" or "admin" in effective_roles(caller))
    if lunas_count and not admin_utama:
        raise MadrasahForbiddenError(
            'Santri ini punya riwayat tagihan yang sudah lunas -- tidak bisa dihapus permanen. '
            'Pakai status "Keluarkan" supaya riwayat keuangannya tetap utuh.'
        )

    await session.execute(delete(TagihanSyahriyah).where(TagihanSyahriyah.santri_id == santri_id))
    await session.execute(delete(RiwayatPenempatanSantri).where(RiwayatPenempatanSantri.santri_id == santri_id))
    await session.execute(delete(AbsensiMadrasah).where(AbsensiMadrasah.santri_id == santri_id))
    await session.execute(delete(ProgresHafalan).where(ProgresHafalan.santri_id == santri_id))
    await session.execute(delete(PesanMadrasah).where(PesanMadrasah.santri_id == santri_id))
    await session.flush()

    await session.delete(row)


async def patch_santri_wali_kelas(session: AsyncSession, guru: UserMadrasah, santri_id: str, payload: SantriPatch) -> SantriMadrasah:
    """Versi terbatas patch_santri untuk wali kelas: hanya boleh untuk santri
    di rombelnya sendiri, dan tidak boleh memindahkan santri ke rombel lain
    lewat sini (itu tetap wewenang admin/kurikulum lewat penempatan)."""
    await assert_own_rombel_santri(session, guru, [santri_id])
    row = await session.get(SantriMadrasah, santri_id)
    if not row:
        raise MadrasahNotFoundError("Santri tidak ditemukan")
    if payload.nama is not None:
        row.nama = payload.nama
    if payload.orang_tua_id is not None:
        row.orang_tua_id = payload.orang_tua_id
    await session.flush()
    return row


async def kirim_pesan(session: AsyncSession, pengirim: UserMadrasah, payload: PesanIn) -> PesanMadrasah:
    santri = await session.get(SantriMadrasah, payload.santri_id)
    if not santri:
        raise MadrasahNotFoundError("Santri tidak ditemukan")
    if pengirim.role == "wali_kelas":
        await assert_own_rombel_santri(session, pengirim, [payload.santri_id])
    elif pengirim.role == "wali_santri":
        await assert_own_child(session, pengirim, payload.santri_id)
    elif pengirim.role not in ("admin", "kepala_sekolah"):
        raise MadrasahForbiddenError("Anda tidak berwenang mengirim pesan untuk santri ini")
    row = PesanMadrasah(santri_id=payload.santri_id, dari_user_id=pengirim.id, isi=payload.isi)
    session.add(row)
    await session.flush()
    return row


async def list_pesan(session: AsyncSession, santri_id: str) -> list[dict]:
    stmt = (
        select(PesanMadrasah)
        .options(selectinload(PesanMadrasah.dari_user))
        .where(PesanMadrasah.santri_id == santri_id)
        .order_by(PesanMadrasah.dibuat_pada.asc())
    )
    rows = list((await session.execute(stmt)).scalars())
    return [
        {
            "id": r.id,
            "isi": r.isi,
            "dari_nama": r.dari_user.nama if r.dari_user else "-",
            "dari_role": r.dari_user.role if r.dari_user else None,
            "dibuat_pada": r.dibuat_pada.isoformat(),
        }
        for r in rows
    ]


async def create_pengumuman(session: AsyncSession, payload: PengumumanIn, dibuat_by: str, caller: UserMadrasah | None = None) -> PengumumanMadrasah:
    row = PengumumanMadrasah(
        judul=payload.judul,
        isi=payload.isi,
        tanggal=date.today(),
        dibuat_by=dibuat_by,
        madrasah_unit_id=await assign_unit_id(session, caller, payload.madrasah_unit_id),
    )
    session.add(row)
    await session.flush()
    return row


def _pastikan_akses_pengumuman(caller: UserMadrasah | None, row: PengumumanMadrasah) -> None:
    if caller is None:
        raise MadrasahForbiddenError("Tidak berwenang")
    if caller.role in CROSS_UNIT_ROLES or "admin" in effective_roles(caller):
        return
    allowed = unit_ids_tugas(caller)
    if not row.madrasah_unit_id or row.madrasah_unit_id not in allowed:
        raise MadrasahForbiddenError("Pengumuman ini bukan milik unit Anda")


async def patch_pengumuman(session: AsyncSession, pengumuman_id: str, payload, caller: UserMadrasah | None = None) -> PengumumanMadrasah:
    row = await session.get(PengumumanMadrasah, pengumuman_id)
    if not row:
        raise MadrasahNotFoundError("Pengumuman tidak ditemukan")
    _pastikan_akses_pengumuman(caller, row)
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(row, field, value)
    await session.flush()
    return row


async def delete_pengumuman(session: AsyncSession, pengumuman_id: str, caller: UserMadrasah | None = None) -> None:
    row = await session.get(PengumumanMadrasah, pengumuman_id)
    if not row:
        raise MadrasahNotFoundError("Pengumuman tidak ditemukan")
    _pastikan_akses_pengumuman(caller, row)
    await session.delete(row)
# dari satu madrasah/unit dalam SATU database yang sama (beda dari
# multi-tenancy existing produk ini, yang memisahkan pelanggan lewat
# DATABASE_URL_MADRASAH terpisah per pelanggan). Pelanggan yang cuma punya
# 1 madrasah otomatis punya SATU unit ("Unit Utama") -- lihat
# ensure_default_unit_and_backfill(), dipanggil dari
# seeder.ensure_madrasah_schema() setiap startup. ---

async def get_or_create_yayasan(session: AsyncSession) -> Yayasan:
    row = (await session.execute(select(Yayasan).limit(1))).scalar_one_or_none()
    if not row:
        row = Yayasan()
        session.add(row)
        await session.flush()
    return row


async def update_yayasan(session: AsyncSession, payload: YayasanPatch) -> Yayasan:
    row = await get_or_create_yayasan(session)
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(row, field, value)
    await session.flush()
    return row


def yayasan_out(row: Yayasan) -> dict:
    return {"id": row.id, "nama": row.nama, "alamat": row.alamat}


async def _default_unit_id(session: AsyncSession) -> str | None:
    """Unit yang dipakai kalau caller tidak menyebutkan madrasah_unit_id
    secara eksplisit -- unit pertama yang dibuat (created_at paling awal).
    None kalau belum ada unit sama sekali (deployment yang belum sempat
    menjalankan ensure_default_unit_and_backfill, mis. test unit murni)."""
    return (
        await session.execute(select(MadrasahUnit.id).order_by(MadrasahUnit.created_at.asc()).limit(1))
    ).scalar_one_or_none()


async def create_unit(session: AsyncSession, payload: MadrasahUnitIn) -> MadrasahUnit:
    yayasan = await get_or_create_yayasan(session)
    row = MadrasahUnit(
        yayasan_id=yayasan.id, nama=payload.nama, alamat=payload.alamat, kepala_unit=payload.kepala_unit
    )
    session.add(row)
    await session.flush()
    await _ikat_kepala_ke_unit(session, row, payload.kepala_user_id)
    return row


async def list_unit(session: AsyncSession) -> list[MadrasahUnit]:
    return list((await session.execute(select(MadrasahUnit).order_by(MadrasahUnit.created_at.asc()))).scalars())


async def patch_unit(session: AsyncSession, unit_id: str, payload: MadrasahUnitPatch) -> MadrasahUnit:
    row = await session.get(MadrasahUnit, unit_id)
    if not row:
        raise MadrasahNotFoundError("Unit madrasah tidak ditemukan")
    data = payload.model_dump(exclude_unset=True)
    kepala_user_id = data.pop("kepala_user_id", None)
    for field, value in data.items():
        setattr(row, field, value)
    await _ikat_kepala_ke_unit(session, row, kepala_user_id)
    await session.flush()
    return row


async def delete_unit(session: AsyncSession, unit_id: str, caller: UserMadrasah | None = None) -> None:
    if caller is None or (caller.role != "admin" and "admin" not in effective_roles(caller)):
        raise MadrasahForbiddenError("Hanya Admin Utama yang dapat menghapus unit")
    unit = await session.get(MadrasahUnit, unit_id)
    if not unit:
        raise MadrasahNotFoundError("Unit madrasah tidak ditemukan")

    santri_ids = list(
        (await session.execute(select(SantriMadrasah.id).where(SantriMadrasah.madrasah_unit_id == unit_id))).scalars()
    )
    if santri_ids:
        await session.execute(delete(TagihanSyahriyah).where(TagihanSyahriyah.santri_id.in_(santri_ids)))
        await session.execute(delete(RiwayatPenempatanSantri).where(RiwayatPenempatanSantri.santri_id.in_(santri_ids)))
        await session.execute(delete(AbsensiMadrasah).where(AbsensiMadrasah.santri_id.in_(santri_ids)))
        await session.execute(delete(ProgresHafalan).where(ProgresHafalan.santri_id.in_(santri_ids)))
        await session.execute(delete(PesanMadrasah).where(PesanMadrasah.santri_id.in_(santri_ids)))
        await session.execute(delete(SantriMadrasah).where(SantriMadrasah.id.in_(santri_ids)))

    rombel_ids = list(
        (await session.execute(select(RombelMadrasah.id).where(RombelMadrasah.madrasah_unit_id == unit_id))).scalars()
    )
    mapel_ids = list(
        (await session.execute(select(MapelMadrasah.id).where(MapelMadrasah.madrasah_unit_id == unit_id))).scalars()
    )
    if rombel_ids:
        await session.execute(delete(JadwalMadrasah).where(JadwalMadrasah.rombel_id.in_(rombel_ids)))
        await session.execute(delete(GuruMapelRombel).where(GuruMapelRombel.rombel_id.in_(rombel_ids)))
    if mapel_ids:
        await session.execute(delete(MateriTarget).where(MateriTarget.mapel_id.in_(mapel_ids)))
        await session.execute(delete(JadwalMadrasah).where(JadwalMadrasah.mapel_id.in_(mapel_ids)))
        await session.execute(delete(GuruMapelRombel).where(GuruMapelRombel.mapel_id.in_(mapel_ids)))
        await session.execute(delete(HonorMengajar).where(HonorMengajar.mapel_id.in_(mapel_ids)))

    await session.execute(delete(PengumumanMadrasah).where(PengumumanMadrasah.madrasah_unit_id == unit_id))
    await session.execute(delete(KegiatanMadrasah).where(KegiatanMadrasah.madrasah_unit_id == unit_id))
    await session.execute(delete(PendaftaranSantri).where(PendaftaranSantri.madrasah_unit_id == unit_id))
    await session.execute(delete(HonorMengajar).where(HonorMengajar.madrasah_unit_id == unit_id))
    await session.execute(delete(JurnalMadrasah).where(JurnalMadrasah.madrasah_unit_id == unit_id))
    await session.execute(delete(BukuKasMadrasah).where(BukuKasMadrasah.madrasah_unit_id == unit_id))
    await session.execute(delete(MapelMadrasah).where(MapelMadrasah.madrasah_unit_id == unit_id))
    await session.execute(delete(RombelMadrasah).where(RombelMadrasah.madrasah_unit_id == unit_id))
    await session.execute(delete(TingkatMadrasah).where(TingkatMadrasah.madrasah_unit_id == unit_id))

    tahun_ids = list(
        (await session.execute(select(TahunAjaranMadrasah.id).where(TahunAjaranMadrasah.madrasah_unit_id == unit_id))).scalars()
    )
    if tahun_ids:
        await session.execute(delete(SemesterMadrasah).where(SemesterMadrasah.tahun_ajaran_id.in_(tahun_ids)))
        await session.execute(delete(TahunAjaranMadrasah).where(TahunAjaranMadrasah.id.in_(tahun_ids)))

    await session.execute(delete(TugasMadrasah).where(TugasMadrasah.madrasah_unit_id == unit_id))
    await session.execute(
        update(UserMadrasah).where(UserMadrasah.madrasah_unit_id == unit_id).values(madrasah_unit_id=None)
    )
    await session.delete(unit)
    await session.flush()


async def assert_unit_boleh_dihapus(session: AsyncSession, unit_id: str) -> None:
    """Tolak hapus unit yang masih punya santri/guru/rombel/tingkat/mapel."""
    checks = (
        (SantriMadrasah, "santri"),
        (UserMadrasah, "akun"),
        (RombelMadrasah, "rombel"),
        (TingkatMadrasah, "tingkat"),
        (MapelMadrasah, "mapel"),
    )
    for model, label in checks:
        n = (
            await session.execute(select(func.count()).select_from(model).where(model.madrasah_unit_id == unit_id))
        ).scalar_one()
        if n:
            raise MadrasahForbiddenError(f"Unit masih punya {label}; nonaktifkan saja, jangan hapus")


def unit_out(row: MadrasahUnit) -> dict:
    return {
        "id": row.id,
        "nama": row.nama,
        "alamat": row.alamat,
        "kepala_unit": row.kepala_unit,
        "aktif": row.aktif,
    }


async def ensure_default_unit_and_backfill(session: AsyncSession) -> None:
    """Idempoten, dipanggil setiap startup lewat seeder.ensure_madrasah_schema():
    pastikan minimal satu MadrasahUnit ada ("Unit Utama" kalau belum ada
    unit sama sekali), lalu tandai semua baris lama yang madrasah_unit_id-nya
    masih NULL (Tingkat/Rombel/Santri/Mapel/User) sebagai milik unit itu.
    Tidak pernah menyentuh baris yang sudah punya unit -- aman dijalankan
    berkali-kali dan aman untuk deployment yang memang sudah multi-unit."""
    unit_id = await _default_unit_id(session)
    if unit_id is None:
        yayasan = await get_or_create_yayasan(session)
        unit = MadrasahUnit(yayasan_id=yayasan.id, nama="Unit Utama")
        session.add(unit)
        await session.flush()
        unit_id = unit.id

    for model in (TingkatMadrasah, RombelMadrasah, SantriMadrasah, MapelMadrasah, BukuKasMadrasah, JurnalMadrasah, HonorMengajar, PengumumanMadrasah, KegiatanMadrasah, PendaftaranSantri, TahunAjaranMadrasah):
        await session.execute(
            update(model).where(model.madrasah_unit_id.is_(None)).values(madrasah_unit_id=unit_id)
        )
    await session.execute(
        update(UserMadrasah)
        .where(
            UserMadrasah.madrasah_unit_id.is_(None),
            UserMadrasah.role.notin_(("admin", "yayasan_admin")),
        )
        .values(madrasah_unit_id=unit_id)
    )
    await session.flush()


async def rekap_yayasan(session: AsyncSession) -> list[dict]:
    """Rekap lintas-unit untuk role yayasan_admin -- satu baris per unit,
    tidak menyingkap detail operasional (nama santri per orang, dsb),
    cuma agregat yang relevan buat pengurus yayasan."""
    units = await list_unit(session)
    hasil = []
    for unit in units:
        total_santri = (
            await session.execute(
                select(func.count())
                .select_from(SantriMadrasah)
                .where(SantriMadrasah.madrasah_unit_id == unit.id, SantriMadrasah.status == "aktif")
            )
        ).scalar_one()
        total_rombel = (
            await session.execute(select(func.count()).select_from(RombelMadrasah).where(RombelMadrasah.madrasah_unit_id == unit.id))
        ).scalar_one()
        total_guru = (
            await session.execute(
                select(func.count())
                .select_from(UserMadrasah)
                .where(
                    UserMadrasah.madrasah_unit_id == unit.id,
                    UserMadrasah.role.in_(STAF_UNIT_ROLES),
                )
            )
        ).scalar_one()
        santri_unit_ids = list(
            (await session.execute(select(SantriMadrasah.id).where(SantriMadrasah.madrasah_unit_id == unit.id))).scalars()
        )
        tagihan_lunas = 0
        tagihan_belum = 0
        if santri_unit_ids:
            tagihan_lunas = (
                await session.execute(
                    select(func.count())
                    .select_from(TagihanSyahriyah)
                    .where(TagihanSyahriyah.santri_id.in_(santri_unit_ids), TagihanSyahriyah.status_bayar.is_(True))
                )
            ).scalar_one()
            tagihan_belum = (
                await session.execute(
                    select(func.count())
                    .select_from(TagihanSyahriyah)
                    .where(TagihanSyahriyah.santri_id.in_(santri_unit_ids), TagihanSyahriyah.status_bayar.is_(False))
                )
            ).scalar_one()
        per_rombel_rows = list(
            (
                await session.execute(
                    select(RombelMadrasah.nama, func.count(SantriMadrasah.id))
                    .select_from(RombelMadrasah)
                    .outerjoin(SantriMadrasah, SantriMadrasah.rombel_id == RombelMadrasah.id)
                    .where(RombelMadrasah.madrasah_unit_id == unit.id)
                    .group_by(RombelMadrasah.id, RombelMadrasah.nama)
                    .order_by(RombelMadrasah.nama)
                )
            ).all()
        )
        hasil.append(
            {
                "unit_id": unit.id,
                "unit_nama": unit.nama,
                "aktif": unit.aktif,
                "total_santri": total_santri,
                "total_rombel": total_rombel,
                "total_guru": total_guru,
                "tagihan_lunas": tagihan_lunas,
                "tagihan_belum": tagihan_belum,
                "per_rombel": [{"rombel": nama, "jumlah_santri": jumlah} for nama, jumlah in per_rombel_rows],
            }
        )
    return hasil


async def rekap_umum(session: AsyncSession, unit_id: str | None = None) -> dict:
    santri_q = select(func.count()).select_from(SantriMadrasah)
    guru_q = select(func.count()).select_from(UserMadrasah).where(UserMadrasah.role.in_(STAF_UNIT_ROLES))
    rombel_q = select(func.count()).select_from(RombelMadrasah)
    lunas_q = (
        select(func.count())
        .select_from(TagihanSyahriyah)
        .join(SantriMadrasah, TagihanSyahriyah.santri_id == SantriMadrasah.id)
        .where(TagihanSyahriyah.status_bayar.is_(True))
    )
    belum_q = (
        select(func.count())
        .select_from(TagihanSyahriyah)
        .join(SantriMadrasah, TagihanSyahriyah.santri_id == SantriMadrasah.id)
        .where(TagihanSyahriyah.status_bayar.is_(False))
    )
    if unit_id:
        santri_q = santri_q.where(SantriMadrasah.madrasah_unit_id == unit_id)
        guru_q = guru_q.where(UserMadrasah.madrasah_unit_id == unit_id)
        rombel_q = rombel_q.where(RombelMadrasah.madrasah_unit_id == unit_id)
        lunas_q = lunas_q.where(SantriMadrasah.madrasah_unit_id == unit_id)
        belum_q = belum_q.where(SantriMadrasah.madrasah_unit_id == unit_id)
    total_santri = (await session.execute(santri_q)).scalar_one()
    total_guru = (await session.execute(guru_q)).scalar_one()
    total_rombel = (await session.execute(rombel_q)).scalar_one()
    tagihan_lunas = (await session.execute(lunas_q)).scalar_one()
    tagihan_belum = (await session.execute(belum_q)).scalar_one()
    per_rombel_stmt = (
        select(RombelMadrasah.nama, func.count(SantriMadrasah.id))
        .select_from(RombelMadrasah)
        .outerjoin(SantriMadrasah, SantriMadrasah.rombel_id == RombelMadrasah.id)
        .group_by(RombelMadrasah.id, RombelMadrasah.nama)
        .order_by(RombelMadrasah.nama)
    )
    if unit_id:
        per_rombel_stmt = per_rombel_stmt.where(RombelMadrasah.madrasah_unit_id == unit_id)
    per_rombel_rows = list((await session.execute(per_rombel_stmt)).all())
    unit_nama = None
    if unit_id:
        unit = await session.get(MadrasahUnit, unit_id)
        unit_nama = unit.nama if unit else None
    return {
        "total_santri": total_santri,
        "total_guru": total_guru,
        "total_rombel": total_rombel,
        "tagihan_lunas": tagihan_lunas,
        "tagihan_belum": tagihan_belum,
        "per_rombel": [{"rombel": nama, "jumlah_santri": jumlah} for nama, jumlah in per_rombel_rows],
        "unit_id": unit_id,
        "unit_nama": unit_nama,
        "per_unit": [],
    }


async def rekap_untuk_caller(session: AsyncSession, caller: UserMadrasah, unit_id: str | None = None) -> dict:
    if caller.role in CROSS_UNIT_ROLES or "admin" in effective_roles(caller) or "yayasan_admin" in effective_roles(caller):
        return await rekap_umum(session, unit_id)
    ids = sorted(unit_ids_tugas(caller))
    if unit_id:
        if unit_id not in ids:
            raise MadrasahForbiddenError("Tidak dapat mengakses unit lain")
        ids = [unit_id]
    if not ids:
        return {
            "total_santri": 0,
            "total_guru": 0,
            "total_rombel": 0,
            "tagihan_lunas": 0,
            "tagihan_belum": 0,
            "per_rombel": [],
            "unit_id": None,
            "unit_nama": None,
            "per_unit": [],
        }
    if len(ids) == 1:
        return await rekap_umum(session, ids[0])
    bagian = [await rekap_umum(session, uid) for uid in ids]
    return {
        "total_santri": sum(p["total_santri"] for p in bagian),
        "total_guru": sum(p["total_guru"] for p in bagian),
        "total_rombel": sum(p["total_rombel"] for p in bagian),
        "tagihan_lunas": sum(p["tagihan_lunas"] for p in bagian),
        "tagihan_belum": sum(p["tagihan_belum"] for p in bagian),
        "per_rombel": [],
        "unit_id": None,
        "unit_nama": "Beberapa unit",
        "per_unit": bagian,
    }


async def list_mapel(session: AsyncSession, unit_id: str | None = None) -> list[MapelMadrasah]:
    stmt = select(MapelMadrasah).options(selectinload(MapelMadrasah.materi)).order_by(MapelMadrasah.nama)
    if unit_id:
        stmt = stmt.where(MapelMadrasah.madrasah_unit_id == unit_id)
    return list((await session.execute(stmt)).scalars())


async def create_mapel(
    session: AsyncSession, payload: MapelIn, caller: UserMadrasah | None = None
) -> MapelMadrasah:
    row = MapelMadrasah(
        kode=payload.kode,
        nama=payload.nama,
        madrasah_unit_id=await assign_unit_id(session, caller, payload.madrasah_unit_id),
    )
    session.add(row)
    await session.flush()
    return row


async def patch_mapel(session: AsyncSession, mapel_id: str, payload: MapelPatch) -> MapelMadrasah:
    row = await session.get(MapelMadrasah, mapel_id)
    if not row:
        raise MadrasahNotFoundError("Mapel tidak ditemukan")
    if payload.kode is not None:
        row.kode = payload.kode
    if payload.nama is not None:
        row.nama = payload.nama
    await session.flush()
    return row


async def delete_mapel(session: AsyncSession, mapel_id: str) -> None:
    row = await session.get(MapelMadrasah, mapel_id)
    if not row:
        raise MadrasahNotFoundError("Mapel tidak ditemukan")
    await session.delete(row)


async def delete_materi(session: AsyncSession, materi_id: str) -> None:
    row = await session.get(MateriTarget, materi_id)
    if not row:
        raise MadrasahNotFoundError("Materi tidak ditemukan")
    await session.delete(row)


async def delete_jadwal(session: AsyncSession, jadwal_id: str) -> None:
    row = await session.get(JadwalMadrasah, jadwal_id)
    if not row:
        raise MadrasahNotFoundError("Jadwal tidak ditemukan")
    await session.delete(row)


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


async def list_jadwal(session: AsyncSession, rombel_id: str | None = None, unit_id: str | None = None) -> list[JadwalMadrasah]:
    stmt = select(JadwalMadrasah).options(selectinload(JadwalMadrasah.mapel), selectinload(JadwalMadrasah.rombel))
    if rombel_id:
        stmt = stmt.where(JadwalMadrasah.rombel_id == rombel_id)
    if unit_id:
        stmt = stmt.join(RombelMadrasah, JadwalMadrasah.rombel_id == RombelMadrasah.id).where(
            RombelMadrasah.madrasah_unit_id == unit_id
        )
    return list((await session.execute(stmt)).scalars())


async def create_jadwal(session: AsyncSession, payload: JadwalIn) -> JadwalMadrasah:
    row = JadwalMadrasah(
        rombel_id=payload.rombel_id,
        mapel_id=payload.mapel_id,
        hari=payload.hari,
        jam_mulai=payload.jam_mulai,
        jam_selesai=payload.jam_selesai,
        semester_id=await _semester_aktif_id(session),
    )
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
    row = GuruMapelRombel(
        guru_id=payload.guru_id, mapel_id=payload.mapel_id, rombel_id=payload.rombel_id, tarif_per_sesi=payload.tarif_per_sesi
    )
    session.add(row)
    guru = await session.get(UserMadrasah, payload.guru_id)
    rombel = await session.get(RombelMadrasah, payload.rombel_id)
    if guru and rombel and rombel.madrasah_unit_id:
        existing_tugas = (
            await session.execute(
                select(TugasMadrasah.id).where(
                    TugasMadrasah.user_id == guru.id,
                    TugasMadrasah.madrasah_unit_id == rombel.madrasah_unit_id,
                    TugasMadrasah.jenis == "guru_mapel",
                )
            )
        ).scalar_one_or_none()
        if not existing_tugas:
            session.add(TugasMadrasah(user_id=guru.id, madrasah_unit_id=rombel.madrasah_unit_id, jenis="guru_mapel"))
    await session.flush()
    return row


async def remove_penugasan(session: AsyncSession, penugasan_id: str) -> None:
    row = await session.get(GuruMapelRombel, penugasan_id)
    if not row:
        raise MadrasahNotFoundError("Penugasan tidak ditemukan")
    await session.delete(row)


async def list_penugasan(session: AsyncSession, guru_id: str | None = None, unit_id: str | None = None) -> list[GuruMapelRombel]:
    stmt = select(GuruMapelRombel).options(
        selectinload(GuruMapelRombel.guru), selectinload(GuruMapelRombel.mapel), selectinload(GuruMapelRombel.rombel)
    )
    if guru_id:
        stmt = stmt.where(GuruMapelRombel.guru_id == guru_id)
    if unit_id:
        stmt = stmt.join(RombelMadrasah, GuruMapelRombel.rombel_id == RombelMadrasah.id).where(
            RombelMadrasah.madrasah_unit_id == unit_id
        )
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

    semester_id = await _semester_aktif_id(session)
    rows: list[AbsensiMadrasah] = []
    for item in payload.items:
        row = AbsensiMadrasah(
            tanggal=payload.tanggal,
            status=item.status,
            santri_id=item.santri_id,
            guru_id=guru_id,
            mapel_id=payload.mapel_id,
            semester_id=semester_id,
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows


async def rekap_absensi_mapel(session: AsyncSession, rombel_id: str, mapel_id: str, guru: UserMadrasah) -> list[dict]:
    # Sama seperti bulk_insert_absensi_mapel/assert_guru_mengajar_santri: guru
    # mapel hanya boleh melihat rekap mapel yang benar-benar ditugaskan padanya
    # di rombel ini, supaya guru mapel A1 tidak bisa mengganti mapel_id di
    # query string untuk mengintip rekap mapel lain (A2) di rombel yang sama.
    # wali_kelas rombel ini boleh lihat rekap semua mapel lewat endpoint
    # /wali-kelas/rekap-absensi tersendiri, bukan dari sini.
    if guru.role not in ("admin", "kepala_sekolah"):
        penugasan = (
            await session.execute(
                select(GuruMapelRombel).where(
                    GuruMapelRombel.guru_id == guru.id,
                    GuruMapelRombel.mapel_id == mapel_id,
                    GuruMapelRombel.rombel_id == rombel_id,
                )
            )
        ).scalar_one_or_none()
        if not penugasan:
            raise MadrasahForbiddenError("Anda tidak ditugaskan untuk mapel/rombel ini")

    stmt = (
        select(AbsensiMadrasah)
        .join(SantriMadrasah, AbsensiMadrasah.santri_id == SantriMadrasah.id)
        .where(SantriMadrasah.rombel_id == rombel_id, AbsensiMadrasah.mapel_id == mapel_id)
        .order_by(AbsensiMadrasah.tanggal.desc())
    )
    rows = list((await session.execute(stmt)).scalars())
    return [{"id": r.id, "tanggal": r.tanggal.isoformat(), "santri_id": r.santri_id, "status": r.status} for r in rows]


async def rekap_absensi_rombel(session: AsyncSession, rombel_id: str) -> list[dict]:
    stmt = (
        select(AbsensiMadrasah)
        .options(selectinload(AbsensiMadrasah.santri))
        .join(SantriMadrasah, AbsensiMadrasah.santri_id == SantriMadrasah.id)
        .where(SantriMadrasah.rombel_id == rombel_id, AbsensiMadrasah.mapel_id.is_not(None))
    )
    rows = list((await session.execute(stmt)).scalars())
    ringkas: dict[str, dict] = {}
    for r in rows:
        item = ringkas.setdefault(
            r.santri_id,
            {"santri_id": r.santri_id, "nama": r.santri.nama if r.santri else "-", "hadir": 0, "sakit": 0, "izin": 0, "alpa": 0, "sesi": 0},
        )
        item["sesi"] += 1
        if r.status in item:
            item[r.status] += 1
    return list(ringkas.values())


# --- Honor mengajar (Fase 2.2): dihitung dari sesi yang benar-benar
# tercatat di AbsensiMadrasah, bukan cuma dari penugasan GuruMapelRombel --
# guru yang ditugaskan tapi tidak pernah mengisi absensi di bulan itu tidak
# ikut dihitung honornya. ---

def honor_out(row: HonorMengajar) -> dict:
    return {
        "id": row.id,
        "guru_id": row.guru_id,
        "guru": row.guru.nama if getattr(row, "guru", None) else None,
        "mapel_id": row.mapel_id,
        "mapel": row.mapel.nama if getattr(row, "mapel", None) else None,
        "bulan_tahun": row.bulan_tahun,
        "jumlah_sesi": row.jumlah_sesi,
        "tarif_per_sesi": str(row.tarif_per_sesi),
        "total": str(row.total),
        "status_bayar": row.status_bayar,
        "dibayar_pada": row.dibayar_pada.isoformat() if row.dibayar_pada else None,
    }


async def list_honor(session: AsyncSession, bulan: str | None = None, unit_id: str | None = None) -> list[HonorMengajar]:
    stmt = select(HonorMengajar).options(selectinload(HonorMengajar.guru), selectinload(HonorMengajar.mapel))
    if bulan:
        stmt = stmt.where(HonorMengajar.bulan_tahun == bulan)
    if unit_id:
        stmt = stmt.where(HonorMengajar.madrasah_unit_id == unit_id)
    stmt = stmt.order_by(HonorMengajar.bulan_tahun.desc())
    return list((await session.execute(stmt)).scalars())


async def generate_honor_massal(
    session: AsyncSession, bulan_tahun: str | None = None, unit_id: str | None = None, tarif_default: Decimal | None = None
) -> list[HonorMengajar]:
    """Satu baris per (guru, mapel) yang punya minimal satu sesi absensi di
    bulan itu. Idempoten per periode: guru+mapel yang sudah punya baris
    honor untuk bulan_tahun ini dilewati, tidak dibuat dobel maupun
    ditimpa -- generate ulang setelah honor sudah dibayar tidak mengubah
    baris yang sudah lunas."""
    period = bulan_tahun or _bulan_tahun()

    tarif_map = {
        (r.guru_id, r.mapel_id): r.tarif_per_sesi
        for r in (await session.execute(select(GuruMapelRombel))).scalars()
        if r.tarif_per_sesi is not None
    }

    # Filter bulan di Python (sama seperti list_buku_kas): tanggal per baris
    # absensi harus dilihat satu-satu untuk dihitung tanggal UNIK per
    # (guru, mapel) -- agregasi SQL count(distinct tanggal) tidak bisa
    # dikombinasikan dengan filter bulan_tahun karena itu bukan kolom asli,
    # jadi diambil mentah lalu dikelompokkan di Python (volume kecil).
    absensi_rows = list(
        (
            await session.execute(
                select(AbsensiMadrasah.guru_id, AbsensiMadrasah.mapel_id, AbsensiMadrasah.tanggal).where(
                    AbsensiMadrasah.mapel_id.is_not(None), AbsensiMadrasah.guru_id.is_not(None)
                )
            )
        ).all()
    )
    tanggal_unik: dict[tuple[str, str], set] = {}
    for guru_id, mapel_id, tanggal in absensi_rows:
        if _bulan_tahun(tanggal) != period:
            continue
        key = (guru_id, mapel_id)
        tanggal_unik.setdefault(key, set()).add(tanggal)
    sesi_per_guru_mapel = {key: len(dates) for key, dates in tanggal_unik.items()}

    existing = {
        (r.guru_id, r.mapel_id)
        for r in (await session.execute(select(HonorMengajar).where(HonorMengajar.bulan_tahun == period))).scalars()
    }

    hasil: list[HonorMengajar] = []
    guru_unit: dict[str, str | None] = {}
    if unit_id:
        guru_unit = {
            u.id: u.madrasah_unit_id
            for u in (await session.execute(select(UserMadrasah).where(UserMadrasah.madrasah_unit_id == unit_id))).scalars()
        }
    for (guru_id, mapel_id), jumlah_sesi in sesi_per_guru_mapel.items():
        if (guru_id, mapel_id) in existing or jumlah_sesi == 0:
            continue
        if unit_id and guru_id not in guru_unit:
            continue
        if guru_id not in guru_unit:
            guru = await session.get(UserMadrasah, guru_id)
            guru_unit[guru_id] = guru.madrasah_unit_id if guru else None
        # Tarif per penugasan (guru+mapel) tetap prioritas kalau sudah diisi
        # admin di GuruMapelRombel -- tarif_default (dari bendahara unit) cuma
        # dipakai untuk penugasan yang belum punya tarif spesifik, sebagai
        # pengganti DEFAULT_HONOR_PER_SESI global.
        tarif = tarif_map.get((guru_id, mapel_id)) or tarif_default or DEFAULT_HONOR_PER_SESI
        row = HonorMengajar(
            guru_id=guru_id,
            mapel_id=mapel_id,
            bulan_tahun=period,
            jumlah_sesi=jumlah_sesi,
            tarif_per_sesi=tarif,
            total=tarif * jumlah_sesi,
            madrasah_unit_id=guru_unit.get(guru_id),
        )
        session.add(row)
        hasil.append(row)
    await session.flush()
    for row in hasil:
        await session.refresh(row, attribute_names=["guru", "mapel"])
    return hasil


async def pay_honor(session: AsyncSession, honor_id: str, caller: UserMadrasah | None = None) -> HonorMengajar:
    row = await session.get(HonorMengajar, honor_id)
    if not row:
        raise MadrasahNotFoundError("Honor tidak ditemukan")
    if row.status_bayar:
        raise MadrasahForbiddenError("Honor ini sudah dibayar")
    _pastikan_akses_unit_keuangan(caller, row.madrasah_unit_id)
    row.status_bayar = True
    row.dibayar_pada = _utcnow()
    await session.flush()
    await session.refresh(row, attribute_names=["guru", "mapel"])
    keterangan = f"Honor {row.guru.nama} -- {row.mapel.nama if row.mapel else '-'} ({row.bulan_tahun})"
    session.add(
        BukuKasMadrasah(
            tanggal=_utcnow().date(),
            tipe="keluar",
            kategori="Honor Mengajar",
            jumlah=row.total,
            keterangan=keterangan,
            madrasah_unit_id=row.madrasah_unit_id,
        )
    )
    await catat_jurnal(
        session,
        tanggal=_utcnow().date(),
        akun_debit=AKUN_BEBAN_HONOR,
        akun_kredit=AKUN_KAS,
        jumlah=row.total,
        keterangan=keterangan,
        sumber_tipe="honor",
        sumber_id=row.id,
        madrasah_unit_id=row.madrasah_unit_id,
    )
    await session.flush()
    return row


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


# --- Audit log: jejak untuk aksi sensitif (login, keuangan, hapus akun/
# data, reset destruktif). Tabel baru; ensure_madrasah_schema membuatnya
# saat startup, tapi self-heal di sini juga (pola sama seperti
# get_pengaturan) untuk deployment yang belum sempat restart. ---

async def record_audit(
    session: AsyncSession,
    *,
    aktor: UserMadrasah | None,
    aksi: str,
    entitas: str = "",
    entitas_id: str | None = None,
    keterangan: str = "",
    ip: str = "",
) -> None:
    row = AuditLogMadrasah(
        aktor_id=aktor.id if aktor else None,
        aktor_nama=aktor.nama if aktor else "-",
        aktor_role=aktor.role if aktor else "-",
        aksi=aksi,
        entitas=entitas,
        entitas_id=entitas_id,
        keterangan=keterangan,
        ip=ip,
    )
    try:
        session.add(row)
        await session.flush()
    except (ProgrammingError, OperationalError):
        await session.rollback()
        conn = await session.connection()
        await conn.run_sync(lambda sync_conn: AuditLogMadrasah.__table__.create(sync_conn, checkfirst=True))
        session.add(row)
        await session.flush()


async def list_audit_log(session: AsyncSession, limit: int = 200) -> list[AuditLogMadrasah]:
    stmt = select(AuditLogMadrasah).order_by(AuditLogMadrasah.waktu.desc()).limit(limit)
    try:
        return list((await session.execute(stmt)).scalars())
    except (ProgrammingError, OperationalError):
        await session.rollback()
        return []


def audit_out(row: AuditLogMadrasah) -> dict:
    return {
        "id": row.id,
        "waktu": row.waktu.isoformat(),
        "aktor_nama": row.aktor_nama,
        "aktor_role": row.aktor_role,
        "aksi": row.aksi,
        "entitas": row.entitas,
        "entitas_id": row.entitas_id,
        "keterangan": row.keterangan,
        "ip": row.ip,
    }


async def rapor_santri(session: AsyncSession, guru: UserMadrasah, santri_id: str) -> dict:
    santri = await session.get(SantriMadrasah, santri_id)
    if not santri:
        raise MadrasahNotFoundError("Santri tidak ditemukan")
    if not await _can_view_santri(session, guru, santri):
        raise MadrasahForbiddenError("Anda tidak berwenang melihat rapor santri ini")
    return {"santri_id": santri.id, "santri_nama": santri.nama, "progres": await progres_series(session, santri_id)}
