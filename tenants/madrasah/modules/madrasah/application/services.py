"""Madrasah use-cases against the isolated Neon session (multi-role)."""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tenants.madrasah.modules.madrasah.application.schemas import (
    AbsenBulkRequest,
    AbsenMapelBulkRequest,
    BukuKasIn,
    GuruIn,
    JadwalIn,
    LoginRequest,
    MapelIn,
    MapelPatch,
    MateriIn,
    MateriPatch,
    PenugasanIn,
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
    KelasMadrasah,
    MadrasahUnit,
    MapelMadrasah,
    MateriTarget,
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
    }


def semester_out(row: SemesterMadrasah) -> dict:
    return {
        "id": row.id,
        "tahun_ajaran_id": row.tahun_ajaran_id,
        "tahun_ajaran": row.tahun_ajaran.kode if getattr(row, "tahun_ajaran", None) else None,
        "nama": row.nama,
        "tanggal_mulai": row.tanggal_mulai.isoformat(),
        "tanggal_selesai": row.tanggal_selesai.isoformat(),
        "status": row.status,
    }


async def create_tahun_ajaran(session: AsyncSession, payload: TahunAjaranIn) -> TahunAjaranMadrasah:
    row = TahunAjaranMadrasah(
        kode=payload.kode, tanggal_mulai=payload.tanggal_mulai, tanggal_selesai=payload.tanggal_selesai
    )
    session.add(row)
    await session.flush()
    return row


async def list_tahun_ajaran(session: AsyncSession) -> list[TahunAjaranMadrasah]:
    return list((await session.execute(select(TahunAjaranMadrasah).order_by(TahunAjaranMadrasah.kode.desc()))).scalars())


async def create_semester(session: AsyncSession, payload: SemesterIn) -> SemesterMadrasah:
    if not await session.get(TahunAjaranMadrasah, payload.tahun_ajaran_id):
        raise MadrasahNotFoundError("Tahun ajaran tidak ditemukan")
    row = SemesterMadrasah(
        tahun_ajaran_id=payload.tahun_ajaran_id,
        nama=payload.nama,
        tanggal_mulai=payload.tanggal_mulai,
        tanggal_selesai=payload.tanggal_selesai,
        status="draft",
    )
    session.add(row)
    await session.flush()
    return row


async def list_semester(session: AsyncSession) -> list[SemesterMadrasah]:
    stmt = (
        select(SemesterMadrasah)
        .options(selectinload(SemesterMadrasah.tahun_ajaran))
        .order_by(SemesterMadrasah.tanggal_mulai.desc())
    )
    return list((await session.execute(stmt)).scalars())


async def get_semester_aktif(session: AsyncSession) -> SemesterMadrasah | None:
    stmt = (
        select(SemesterMadrasah)
        .options(selectinload(SemesterMadrasah.tahun_ajaran))
        .where(SemesterMadrasah.status == "aktif")
        .limit(1)
    )
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
    await session.execute(
        update(SemesterMadrasah).where(SemesterMadrasah.status == "aktif").values(status="ditutup")
    )
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


def user_out(user: UserMadrasah) -> dict:
    return {"id": user.id, "nama": user.nama, "no_hp": user.no_hp, "role": user.role}


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


async def list_santri(session: AsyncSession, kelas_id: str | None = None, status: str | None = "aktif") -> list[SantriMadrasah]:
    """status="aktif" (default) menyembunyikan santri lulus/keluar/pindah
    dari listing biasa (dropdown absensi, dsb) -- barisnya tetap ada di DB,
    cuma tidak ikut ditampilkan. status=None/"semua" menonaktifkan filter
    ini untuk layar admin yang memang butuh melihat semuanya (mis. daftar
    alumni)."""
    stmt = select(SantriMadrasah).order_by(SantriMadrasah.nama)
    if kelas_id:
        stmt = stmt.where((SantriMadrasah.kelas_id == kelas_id) | (SantriMadrasah.rombel_id == kelas_id))
    if status and status != "semua":
        stmt = stmt.where(SantriMadrasah.status == status)
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


async def assert_own_rombel(session: AsyncSession, guru: UserMadrasah, rombel_id: str) -> None:
    """Untuk endpoint yang menerima rombel_id langsung (bukan santri_id):
    pastikan rombel_id itu memang milik wali kelas yang bersangkutan."""
    if guru.role in ("admin", "kepala_sekolah"):
        return
    if guru.role != "wali_kelas":
        return
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


async def list_rombel_for_caller(session: AsyncSession, caller: UserMadrasah) -> list[RombelMadrasah]:
    rows = await list_rombel(session)
    if caller.role != "wali_kelas":
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
    if caller.role != "wali_kelas":
        return await list_santri(session, kelas_id)
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


async def list_tagihan_menunggu(session: AsyncSession) -> list[TagihanSyahriyah]:
    stmt = (
        select(TagihanSyahriyah)
        .options(selectinload(TagihanSyahriyah.santri))
        .where(TagihanSyahriyah.diajukan_oleh.is_not(None), TagihanSyahriyah.status_bayar.is_(False))
        .order_by(TagihanSyahriyah.diajukan_pada.asc())
    )
    return list((await session.execute(stmt)).scalars())


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
    }


async def list_pengumuman(session: AsyncSession, limit: int = 50) -> list[PengumumanMadrasah]:
    return list((await session.execute(select(PengumumanMadrasah).order_by(PengumumanMadrasah.tanggal.desc()).limit(limit))).scalars())


async def generate_spp_massal(session: AsyncSession) -> list[TagihanSyahriyah]:
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
    santri_rows = list((await session.execute(select(SantriMadrasah))).scalars())
    existing = {r.santri_id for r in (await session.execute(select(TagihanSyahriyah).where(TagihanSyahriyah.bulan_tahun == period))).scalars()}
    for santri in santri_rows:
        if santri.id in existing:
            continue
        session.add(
            TagihanSyahriyah(
                bulan_tahun=period, nominal=DEFAULT_SPP_NOMINAL, status_bayar=False, santri_id=santri.id, semester_id=semester_id
            )
        )
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
    session.add(BukuKasMadrasah(
        tanggal=_utcnow().date(),
        tipe="masuk",
        kategori="SPP",
        jumlah=row.nominal,
        keterangan=f"SPP {row.santri.nama} -- {row.bulan_tahun}",
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
    )
    await session.flush()
    return row


async def list_buku_kas(session: AsyncSession, bulan: str | None = None) -> list[BukuKasMadrasah]:
    # Filter bulan (format "YYYY-MM") di Python, bukan lewat fungsi tanggal
    # SQL yang beda nama antar dialek (strftime di SQLite vs to_char di
    # Postgres) -- volume baris buku kas per sekolah kecil, jadi ini murah.
    stmt = select(BukuKasMadrasah).order_by(BukuKasMadrasah.tanggal.desc(), BukuKasMadrasah.created_at.desc())
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


async def create_buku_kas_entry(session: AsyncSession, payload: BukuKasIn, dicatat_oleh: str) -> BukuKasMadrasah:
    row = BukuKasMadrasah(
        tanggal=payload.tanggal,
        tipe=payload.tipe,
        kategori=payload.kategori,
        jumlah=payload.jumlah,
        keterangan=payload.keterangan,
        dicatat_oleh=dicatat_oleh,
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
    )
    return row


def buku_kas_out(row: BukuKasMadrasah) -> dict:
    return {
        "id": row.id,
        "tanggal": row.tanggal.isoformat(),
        "tipe": row.tipe,
        "kategori": row.kategori,
        "jumlah": str(row.jumlah),
        "keterangan": row.keterangan,
    }


async def laporan_keuangan(session: AsyncSession, bulan: str | None = None) -> dict:
    rows = await list_buku_kas(session, bulan)
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


async def list_jurnal(session: AsyncSession, bulan: str | None = None) -> list[JurnalMadrasah]:
    stmt = select(JurnalMadrasah).order_by(JurnalMadrasah.tanggal.desc(), JurnalMadrasah.created_at.desc())
    rows = list((await session.execute(stmt)).scalars())
    if bulan:
        rows = [r for r in rows if r.tanggal.strftime("%Y-%m") == bulan]
    return rows


async def laba_rugi(session: AsyncSession, bulan: str | None = None) -> dict:
    """Laba-rugi sederhana: setiap baris jurnal menambah SALDO akun
    kreditnya dan mengurangi saldo akun debitnya (konvensi normal
    akuntansi), lalu akun tipe pendapatan/beban diringkas per akun."""
    rows = await list_jurnal(session, bulan)
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


async def list_tingkat(session: AsyncSession) -> list[TingkatMadrasah]:
    return list((await session.execute(select(TingkatMadrasah).order_by(TingkatMadrasah.urutan))).scalars())


async def create_tingkat(session: AsyncSession, payload: TingkatIn) -> TingkatMadrasah:
    row = TingkatMadrasah(
        nama=payload.nama, urutan=payload.urutan, madrasah_unit_id=payload.madrasah_unit_id or await _default_unit_id(session)
    )
    session.add(row)
    await session.flush()
    return row


async def list_rombel(session: AsyncSession) -> list[RombelMadrasah]:
    return list((await session.execute(select(RombelMadrasah).options(selectinload(RombelMadrasah.wali_kelas), selectinload(RombelMadrasah.tingkat)).order_by(RombelMadrasah.nama))).scalars())


async def create_rombel(session: AsyncSession, payload: RombelIn) -> RombelMadrasah:
    row = RombelMadrasah(
        nama=payload.nama,
        tingkat_id=payload.tingkat_id,
        wali_kelas_id=payload.wali_kelas_id,
        madrasah_unit_id=payload.madrasah_unit_id or await _default_unit_id(session),
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


async def list_guru(session: AsyncSession) -> list[UserMadrasah]:
    return list((await session.execute(select(UserMadrasah).where(UserMadrasah.role.in_(["wali_kelas", "guru", "kepala_sekolah", "kurikulum", "bendahara"])).order_by(UserMadrasah.nama))).scalars())


async def list_wali_santri(session: AsyncSession) -> list[UserMadrasah]:
    return list((await session.execute(select(UserMadrasah).where(UserMadrasah.role == "wali_santri").order_by(UserMadrasah.nama))).scalars())


async def create_guru(session: AsyncSession, payload: GuruIn) -> UserMadrasah:
    row = UserMadrasah(
        nama=payload.nama,
        no_hp=payload.no_hp.strip(),
        password_hash=hash_password(payload.password),
        role=payload.role or "wali_kelas",
        madrasah_unit_id=payload.madrasah_unit_id or await _default_unit_id(session),
    )
    session.add(row)
    await session.flush()
    return row


async def patch_guru(session: AsyncSession, user_id: str, payload: UserPatch) -> UserMadrasah:
    row = await session.get(UserMadrasah, user_id)
    if not row:
        raise MadrasahNotFoundError("Akun tidak ditemukan")
    if payload.nama is not None:
        row.nama = payload.nama
    if payload.no_hp is not None:
        row.no_hp = payload.no_hp.strip()
    if payload.role is not None:
        row.role = payload.role
    if payload.password:
        row.password_hash = hash_password(payload.password)
    if payload.role is not None or payload.password:
        # Password atau role berubah -> setiap JWT yang sudah beredar untuk
        # akun ini (termasuk yang bocor) langsung ditolak di request
        # berikutnya, lihat get_current_user_madrasah.
        row.session_version += 1
    await session.flush()
    return row


async def delete_guru(session: AsyncSession, user_id: str) -> None:
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


async def create_wali_santri(session: AsyncSession, payload: GuruIn) -> UserMadrasah:
    row = UserMadrasah(
        nama=payload.nama,
        no_hp=payload.no_hp.strip(),
        password_hash=hash_password(payload.password),
        role="wali_santri",
        madrasah_unit_id=payload.madrasah_unit_id or await _default_unit_id(session),
    )
    session.add(row)
    await session.flush()
    return row


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


async def create_santri(session: AsyncSession, payload: SantriIn) -> SantriMadrasah:
    row = SantriMadrasah(
        nama=payload.nama,
        rombel_id=payload.rombel_id,
        kelas_id=payload.kelas_id or payload.rombel_id,
        orang_tua_id=payload.orang_tua_id,
        madrasah_unit_id=payload.madrasah_unit_id or await _default_unit_id(session),
    )
    session.add(row)
    await session.flush()
    if payload.rombel_id:
        await _catat_riwayat_penempatan(session, row.id, payload.rombel_id)
    return row


async def patch_santri(session: AsyncSession, santri_id: str, payload: SantriPatch) -> SantriMadrasah:
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
        row.orang_tua_id = payload.orang_tua_id
    await session.flush()
    return row


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


async def kenaikan_kelas_massal(session: AsyncSession, payload: KenaikanKelasRequest) -> dict:
    """Proses satu batch kenaikan kelas: tiap item pindah rombel (dicatat ke
    riwayat lewat _catat_riwayat_penempatan) atau, kalau rombel_tujuan_id
    kosong, ditandai lulus (tanggal hari ini, riwayat ditutup). Santri yang
    tidak disebutkan dalam payload tidak tersentuh sama sekali."""
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


async def delete_santri(session: AsyncSession, santri_id: str) -> None:
    row = await session.get(SantriMadrasah, santri_id)
    if not row:
        raise MadrasahNotFoundError("Santri tidak ditemukan")
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


async def create_pengumuman(session: AsyncSession, payload: PengumumanIn, dibuat_by: str) -> PengumumanMadrasah:
    row = PengumumanMadrasah(judul=payload.judul, isi=payload.isi, tanggal=date.today(), dibuat_by=dibuat_by)
    session.add(row)
    await session.flush()
    return row


# --- Yayasan & MadrasahUnit (Fase 3): satu yayasan bisa membawahi lebih
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
    return row


async def list_unit(session: AsyncSession) -> list[MadrasahUnit]:
    return list((await session.execute(select(MadrasahUnit).order_by(MadrasahUnit.created_at.asc()))).scalars())


async def patch_unit(session: AsyncSession, unit_id: str, payload: MadrasahUnitPatch) -> MadrasahUnit:
    row = await session.get(MadrasahUnit, unit_id)
    if not row:
        raise MadrasahNotFoundError("Unit madrasah tidak ditemukan")
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(row, field, value)
    await session.flush()
    return row


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

    for model in (TingkatMadrasah, RombelMadrasah, SantriMadrasah, MapelMadrasah, UserMadrasah):
        await session.execute(
            update(model).where(model.madrasah_unit_id.is_(None)).values(madrasah_unit_id=unit_id)
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
                .where(UserMadrasah.madrasah_unit_id == unit.id, UserMadrasah.role.in_(("guru", "wali_kelas")))
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
            }
        )
    return hasil


async def rekap_umum(session: AsyncSession) -> dict:
    total_santri = (await session.execute(select(func.count()).select_from(SantriMadrasah))).scalar_one()
    total_guru = (await session.execute(select(func.count()).select_from(UserMadrasah).where(UserMadrasah.role.in_(("guru", "wali_kelas"))))).scalar_one()
    total_rombel = (await session.execute(select(func.count()).select_from(RombelMadrasah))).scalar_one()
    tagihan_lunas = (await session.execute(select(func.count()).select_from(TagihanSyahriyah).where(TagihanSyahriyah.status_bayar.is_(True)))).scalar_one()
    tagihan_belum = (await session.execute(select(func.count()).select_from(TagihanSyahriyah).where(TagihanSyahriyah.status_bayar.is_(False)))).scalar_one()
    per_rombel_rows = list(
        (
            await session.execute(
                select(RombelMadrasah.nama, func.count(SantriMadrasah.id))
                .select_from(RombelMadrasah)
                .outerjoin(SantriMadrasah, SantriMadrasah.rombel_id == RombelMadrasah.id)
                .group_by(RombelMadrasah.id, RombelMadrasah.nama)
                .order_by(RombelMadrasah.nama)
            )
        ).all()
    )
    return {
        "total_santri": total_santri,
        "total_guru": total_guru,
        "total_rombel": total_rombel,
        "tagihan_lunas": tagihan_lunas,
        "tagihan_belum": tagihan_belum,
        "per_rombel": [{"rombel": nama, "jumlah_santri": jumlah} for nama, jumlah in per_rombel_rows],
    }


async def list_mapel(session: AsyncSession) -> list[MapelMadrasah]:
    return list((await session.execute(select(MapelMadrasah).options(selectinload(MapelMadrasah.materi)).order_by(MapelMadrasah.nama))).scalars())


async def create_mapel(session: AsyncSession, payload: MapelIn) -> MapelMadrasah:
    row = MapelMadrasah(
        kode=payload.kode, nama=payload.nama, madrasah_unit_id=payload.madrasah_unit_id or await _default_unit_id(session)
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


async def list_jadwal(session: AsyncSession, rombel_id: str | None = None) -> list[JadwalMadrasah]:
    stmt = select(JadwalMadrasah).options(selectinload(JadwalMadrasah.mapel), selectinload(JadwalMadrasah.rombel))
    if rombel_id:
        stmt = stmt.where(JadwalMadrasah.rombel_id == rombel_id)
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


async def rekap_absensi_mapel(session: AsyncSession, rombel_id: str, mapel_id: str) -> list[dict]:
    stmt = (
        select(AbsensiMadrasah)
        .join(SantriMadrasah, AbsensiMadrasah.santri_id == SantriMadrasah.id)
        .where(SantriMadrasah.rombel_id == rombel_id, AbsensiMadrasah.mapel_id == mapel_id)
        .order_by(AbsensiMadrasah.tanggal.desc())
    )
    rows = list((await session.execute(stmt)).scalars())
    return [{"id": r.id, "tanggal": r.tanggal.isoformat(), "santri_id": r.santri_id, "status": r.status} for r in rows]


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


async def list_honor(session: AsyncSession, bulan: str | None = None) -> list[HonorMengajar]:
    stmt = select(HonorMengajar).options(selectinload(HonorMengajar.guru), selectinload(HonorMengajar.mapel))
    if bulan:
        stmt = stmt.where(HonorMengajar.bulan_tahun == bulan)
    stmt = stmt.order_by(HonorMengajar.bulan_tahun.desc())
    return list((await session.execute(stmt)).scalars())


async def generate_honor_massal(session: AsyncSession, bulan_tahun: str | None = None) -> list[HonorMengajar]:
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
    for (guru_id, mapel_id), jumlah_sesi in sesi_per_guru_mapel.items():
        if (guru_id, mapel_id) in existing or jumlah_sesi == 0:
            continue
        tarif = tarif_map.get((guru_id, mapel_id), DEFAULT_HONOR_PER_SESI)
        row = HonorMengajar(
            guru_id=guru_id,
            mapel_id=mapel_id,
            bulan_tahun=period,
            jumlah_sesi=jumlah_sesi,
            tarif_per_sesi=tarif,
            total=tarif * jumlah_sesi,
        )
        session.add(row)
        hasil.append(row)
    await session.flush()
    for row in hasil:
        await session.refresh(row, attribute_names=["guru", "mapel"])
    return hasil


async def pay_honor(session: AsyncSession, honor_id: str) -> HonorMengajar:
    row = await session.get(HonorMengajar, honor_id)
    if not row:
        raise MadrasahNotFoundError("Honor tidak ditemukan")
    if row.status_bayar:
        raise MadrasahForbiddenError("Honor ini sudah dibayar")
    row.status_bayar = True
    row.dibayar_pada = _utcnow()
    await session.flush()
    await session.refresh(row, attribute_names=["guru", "mapel"])
    keterangan = f"Honor {row.guru.nama} -- {row.mapel.nama if row.mapel else '-'} ({row.bulan_tahun})"
    session.add(
        BukuKasMadrasah(
            tanggal=_utcnow().date(), tipe="keluar", kategori="Honor Mengajar", jumlah=row.total, keterangan=keterangan
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
