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
    PengumumanIn,
    PesanIn,
    TingkatIn,
    TingkatPatch,
    UserPatch,
)
from tenants.madrasah.modules.madrasah.infrastructure.models import (
    AbsensiMadrasah,
    BukuKasMadrasah,
    GuruMapelRombel,
    JadwalMadrasah,
    KelasMadrasah,
    MapelMadrasah,
    MateriTarget,
    PengaturanSekolah,
    PengumumanMadrasah,
    PesanMadrasah,
    ProgresHafalan,
    RombelMadrasah,
    SantriMadrasah,
    TagihanSyahriyah,
    TingkatMadrasah,
    UserMadrasah,
)
from shared.security import hash_password, verify_password

STATUS_BELUM = "Belum Bayar"
STATUS_MENUNGGU = "Menunggu Verifikasi"
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
    session.add(BukuKasMadrasah(
        tanggal=_utcnow().date(),
        tipe="masuk",
        kategori="SPP",
        jumlah=row.nominal,
        keterangan=f"SPP {row.santri.nama} -- {row.bulan_tahun}",
    ))
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
    row = UserMadrasah(nama=payload.nama, no_hp=payload.no_hp.strip(), password_hash=hash_password(payload.password), role=payload.role or "wali_kelas")
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
    row = UserMadrasah(nama=payload.nama, no_hp=payload.no_hp.strip(), password_hash=hash_password(payload.password), role="wali_santri")
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


async def patch_santri(session: AsyncSession, santri_id: str, payload: SantriPatch) -> SantriMadrasah:
    row = await session.get(SantriMadrasah, santri_id)
    if not row:
        raise MadrasahNotFoundError("Santri tidak ditemukan")
    if payload.nama is not None:
        row.nama = payload.nama
    if payload.rombel_id is not None:
        row.rombel_id = payload.rombel_id
        row.kelas_id = payload.rombel_id
    if payload.orang_tua_id is not None:
        row.orang_tua_id = payload.orang_tua_id
    await session.flush()
    return row


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
    row = MapelMadrasah(kode=payload.kode, nama=payload.nama)
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
