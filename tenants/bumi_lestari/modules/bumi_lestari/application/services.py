"""Business logic -- bumi_lestari Tahap 1 (keuangan dasar + kas kecil)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.security import hash_password, verify_password
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas import (
    AkunKasIn,
    ChangePasswordIn,
    KategoriIn,
    ProfilIn,
    ProporsiIn,
    ResetPasswordIn,
    LoginIn,
    TransaksiIn,
    TransferIn,
    UserCreateIn,
    UserPatchIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import (
    JENIS_AKUN,
    JENIS_IMPRESET,
    JENIS_KATEGORI,
    JENIS_TRANSAKSI,
    KODE_KAS_UTAMA,
    PROFIL_ID,
    BlAkunKas,
    BlKategori,
    BlProfil,
    BlProporsiBagiHasil,
    BlTransaksi,
    BlTransfer,
    BlUser,
)

WIB = ZoneInfo("Asia/Jakarta")


def _bad(detail: str, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


def _is_staff(user: BlUser) -> bool:
    return (user.role or "").strip().lower() == "staff"


def _is_admin(user: BlUser) -> bool:
    return (user.role or "").strip().lower() == "admin"


def boleh_akses_akun(user: BlUser, akun: BlAkunKas) -> bool:
    """admin: semua akun; owner: semua kecuali kas iklan; staf: hanya kas kecil."""
    if _is_admin(user):
        return True
    if akun.jenis == "kas_iklan":
        return False
    if _is_staff(user):
        return akun.jenis == "kas_kecil"
    return True


def _hari_ini() -> date:
    return datetime.now(WIB).date()


# --- Auth / users ---------------------------------------------------------------


async def authenticate_user(session: AsyncSession, payload: LoginIn) -> BlUser:
    user = (await session.execute(select(BlUser).where(BlUser.email == payload.email))).scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        raise _bad("Email atau password salah", status.HTTP_401_UNAUTHORIZED)
    if not user.aktif:
        raise _bad("Akun dinonaktifkan", status.HTTP_403_FORBIDDEN)
    return user


async def create_user(session: AsyncSession, actor: BlUser, payload: UserCreateIn) -> BlUser:
    # Hanya admin yang boleh membuat akun admin/owner; owner hanya membuat staf.
    if payload.role in ("admin", "owner") and (actor.role or "").strip().lower() != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Hanya admin yang boleh membuat akun admin/owner")
    exists = (await session.execute(select(BlUser.id).where(BlUser.email == payload.email))).first()
    if exists:
        raise _bad("Email sudah terdaftar", status.HTTP_409_CONFLICT)
    user = BlUser(
        nama=payload.nama.strip(),
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=payload.role,
        must_change_password=True,
    )
    session.add(user)
    await session.flush()
    return user


async def update_user(session: AsyncSession, actor: BlUser, user_id: str, payload: UserPatchIn) -> BlUser:
    """Admin mengubah nama/email/role/aktif pengguna lain. Tidak boleh mengubah role atau menonaktifkan diri sendiri."""
    user = await session.get(BlUser, user_id)
    if user is None:
        raise _bad("Pengguna tidak ditemukan", status.HTTP_404_NOT_FOUND)
    data = payload.model_dump(exclude_unset=True)
    if user.id == actor.id and (data.get("role", user.role) != user.role or data.get("aktif") is False):
        raise _bad("Admin tidak bisa mengubah role atau menonaktifkan akunnya sendiri")
    if "email" in data and data["email"] != user.email:
        if (await session.execute(select(BlUser.id).where(BlUser.email == data["email"]))).first():
            raise _bad("Email sudah terdaftar", status.HTTP_409_CONFLICT)
    for kolom, nilai in data.items():
        if nilai is not None:
            setattr(user, kolom, nilai.strip() if isinstance(nilai, str) else nilai)
    await session.flush()
    return user


async def reset_password(session: AsyncSession, user_id: str, payload: ResetPasswordIn) -> BlUser:
    """Admin menyetel password baru untuk pengguna lain; pengguna wajib menggantinya saat login."""
    user = await session.get(BlUser, user_id)
    if user is None:
        raise _bad("Pengguna tidak ditemukan", status.HTTP_404_NOT_FOUND)
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = True
    await session.flush()
    return user


async def list_users(session: AsyncSession) -> list[BlUser]:
    return list((await session.execute(select(BlUser).order_by(BlUser.created_at))).scalars())


async def change_password(session: AsyncSession, user: BlUser, payload: ChangePasswordIn) -> BlUser:
    if not verify_password(payload.current_password, user.password_hash):
        raise _bad("Password saat ini salah")
    if payload.new_password == payload.current_password:
        raise _bad("Password baru harus berbeda dari password saat ini")
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    await session.flush()
    return user


# --- Akun kas & saldo -------------------------------------------------------------


async def _akun_or_404(session: AsyncSession, akun_id: str) -> BlAkunKas:
    akun = await session.get(BlAkunKas, akun_id)
    if not akun or not akun.aktif:
        raise _bad("Akun kas tidak ditemukan", status.HTTP_404_NOT_FOUND)
    return akun


async def _akun_by_kode(session: AsyncSession, kode: str) -> BlAkunKas:
    akun = (await session.execute(select(BlAkunKas).where(BlAkunKas.kode == kode))).scalar_one_or_none()
    if not akun:
        raise _bad(f"Akun {kode} belum dibuat (jalankan seed-now)", status.HTTP_409_CONFLICT)
    return akun


async def get_kas_kecil(session: AsyncSession) -> BlAkunKas:
    akun = (
        await session.execute(select(BlAkunKas).where(BlAkunKas.jenis == "kas_kecil", BlAkunKas.aktif.is_(True)))
    ).scalars().first()
    if not akun:
        raise _bad("Akun kas kecil belum dibuat (jalankan seed-now)", status.HTTP_409_CONFLICT)
    return akun


async def _sum(session: AsyncSession, stmt) -> Decimal:
    return Decimal(str((await session.execute(stmt)).scalar_one() or 0))


async def saldo_akun(session: AsyncSession, akun: BlAkunKas, sampai: date | None = None) -> Decimal:
    """saldo_awal + masuk - keluar + transfer masuk - transfer keluar (baris batal diabaikan).
    `sampai`: saldo pada akhir hari itu (kosong = saldo terkini)."""

    def trx(jenis: str):
        stmt = select(func.coalesce(func.sum(BlTransaksi.jumlah), 0)).where(
            BlTransaksi.akun_id == akun.id, BlTransaksi.jenis == jenis, BlTransaksi.dibatalkan.is_(False)
        )
        return stmt.where(BlTransaksi.tanggal <= sampai) if sampai else stmt

    def trf(kolom):
        stmt = select(func.coalesce(func.sum(BlTransfer.jumlah), 0)).where(
            kolom == akun.id, BlTransfer.dibatalkan.is_(False)
        )
        return stmt.where(BlTransfer.tanggal <= sampai) if sampai else stmt

    return (
        Decimal(akun.saldo_awal)
        + await _sum(session, trx("masuk"))
        - await _sum(session, trx("keluar"))
        + await _sum(session, trf(BlTransfer.ke_akun_id))
        - await _sum(session, trf(BlTransfer.dari_akun_id))
    )


async def list_akun(session: AsyncSession, user: BlUser) -> list[tuple[BlAkunKas, Decimal]]:
    stmt = select(BlAkunKas).where(BlAkunKas.aktif.is_(True)).order_by(BlAkunKas.created_at)
    if _is_staff(user):
        stmt = stmt.where(BlAkunKas.jenis == "kas_kecil")
    akuns = [a for a in (await session.execute(stmt)).scalars() if boleh_akses_akun(user, a)]
    return [(a, await saldo_akun(session, a)) for a in akuns]


async def create_akun(session: AsyncSession, payload: AkunKasIn) -> BlAkunKas:
    if payload.jenis not in JENIS_AKUN:
        raise _bad(f"Jenis akun harus salah satu dari: {', '.join(JENIS_AKUN)}")
    if payload.jenis in JENIS_IMPRESET and payload.plafon is None:
        raise _bad("Akun kas kecil/kas iklan wajib punya plafon")
    if payload.jenis not in JENIS_IMPRESET and payload.plafon is not None:
        raise _bad("Plafon hanya untuk akun kas kecil/kas iklan")
    if (await session.execute(select(BlAkunKas.id).where(BlAkunKas.kode == payload.kode))).first():
        raise _bad("Kode akun sudah dipakai", status.HTTP_409_CONFLICT)
    akun = BlAkunKas(
        kode=payload.kode.strip().upper(),
        nama=payload.nama.strip(),
        jenis=payload.jenis,
        saldo_awal=payload.saldo_awal,
        plafon=payload.plafon,
    )
    session.add(akun)
    await session.flush()
    return akun


# --- Kategori --------------------------------------------------------------------


async def list_kategori(session: AsyncSession) -> list[BlKategori]:
    stmt = select(BlKategori).where(BlKategori.aktif.is_(True)).order_by(BlKategori.jenis, BlKategori.nama)
    return list((await session.execute(stmt)).scalars())


async def create_kategori(session: AsyncSession, payload: KategoriIn) -> BlKategori:
    if payload.jenis not in JENIS_KATEGORI:
        raise _bad(f"Jenis kategori harus salah satu dari: {', '.join(JENIS_KATEGORI)}")
    if (await session.execute(select(BlKategori.id).where(BlKategori.nama == payload.nama.strip()))).first():
        raise _bad("Nama kategori sudah dipakai", status.HTTP_409_CONFLICT)
    kategori = BlKategori(nama=payload.nama.strip(), jenis=payload.jenis)
    session.add(kategori)
    await session.flush()
    return kategori


# --- Transaksi -------------------------------------------------------------------


async def _pastikan_saldo_cukup(session: AsyncSession, akun: BlAkunKas, jumlah: Decimal) -> None:
    if await saldo_akun(session, akun) < jumlah:
        raise _bad(f"Saldo {akun.nama} tidak cukup")


async def create_transaksi(session: AsyncSession, user: BlUser, payload: TransaksiIn) -> BlTransaksi:
    if payload.jenis not in JENIS_TRANSAKSI:
        raise _bad(f"Jenis transaksi harus salah satu dari: {', '.join(JENIS_TRANSAKSI)}")
    akun = await _akun_or_404(session, payload.akun_id)
    kategori = await session.get(BlKategori, payload.kategori_id)
    if not kategori or not kategori.aktif:
        raise _bad("Kategori tidak ditemukan", status.HTTP_404_NOT_FOUND)
    if (payload.jenis == "masuk") != (kategori.jenis == "pemasukan"):
        raise _bad("Jenis transaksi tidak cocok dengan jenis kategori")
    if not boleh_akses_akun(user, akun):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akun ini hanya bisa diakses admin")
    if _is_staff(user) and not (akun.jenis == "kas_kecil" and payload.jenis == "keluar"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Staf hanya boleh mencatat pengeluaran kas kecil"
        )
    # Akun imprest (kas kecil, kas iklan) berplafon: tidak boleh minus.
    if akun.jenis in JENIS_IMPRESET and payload.jenis == "keluar":
        await _pastikan_saldo_cukup(session, akun, payload.jumlah)
    trx = BlTransaksi(
        tanggal=payload.tanggal or _hari_ini(),
        akun_id=akun.id,
        kategori_id=kategori.id,
        jenis=payload.jenis,
        jumlah=payload.jumlah,
        keterangan=payload.keterangan.strip(),
        dibuat_oleh=user.id,
    )
    session.add(trx)
    await session.flush()
    return trx


async def list_transaksi(
    session: AsyncSession,
    user: BlUser,
    *,
    akun_id: str | None = None,
    dari: date | None = None,
    sampai: date | None = None,
    termasuk_batal: bool = False,
) -> list[BlTransaksi]:
    stmt = select(BlTransaksi).order_by(BlTransaksi.tanggal.desc(), BlTransaksi.created_at.desc())
    if _is_staff(user):
        akun_id = (await get_kas_kecil(session)).id  # staf: hanya kas kecil, parameter diabaikan
    if akun_id:
        akun = await session.get(BlAkunKas, akun_id)
        if akun is not None and not boleh_akses_akun(user, akun):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akun ini hanya bisa diakses admin")
        stmt = stmt.where(BlTransaksi.akun_id == akun_id)
    elif not _is_admin(user):  # owner/staf tidak melihat transaksi kas iklan
        stmt = stmt.where(BlTransaksi.akun_id.not_in(select(BlAkunKas.id).where(BlAkunKas.jenis == "kas_iklan")))
    if dari:
        stmt = stmt.where(BlTransaksi.tanggal >= dari)
    if sampai:
        stmt = stmt.where(BlTransaksi.tanggal <= sampai)
    if not termasuk_batal:
        stmt = stmt.where(BlTransaksi.dibatalkan.is_(False))
    return list((await session.execute(stmt)).scalars())


def _batalkan(row, alasan: str) -> None:
    if row.dibatalkan:
        raise _bad("Sudah dibatalkan", status.HTTP_409_CONFLICT)
    row.dibatalkan = True
    row.dibatalkan_at = datetime.now(timezone.utc)
    row.alasan_batal = alasan.strip()


async def batalkan_transaksi(session: AsyncSession, trx_id: str, alasan: str) -> BlTransaksi:
    trx = await session.get(BlTransaksi, trx_id)
    if not trx:
        raise _bad("Transaksi tidak ditemukan", status.HTTP_404_NOT_FOUND)
    _batalkan(trx, alasan)
    await session.flush()
    return trx


# --- Transfer & pengisian kas kecil -------------------------------------------------


async def _buat_transfer(
    session: AsyncSession, user: BlUser, *, tanggal, dari: BlAkunKas, ke: BlAkunKas, jumlah: Decimal,
    jenis: str, keterangan: str,
) -> BlTransfer:
    if dari.id == ke.id:
        raise _bad("Akun asal dan tujuan harus berbeda")
    await _pastikan_saldo_cukup(session, dari, jumlah)
    transfer = BlTransfer(
        tanggal=tanggal or _hari_ini(),
        dari_akun_id=dari.id,
        ke_akun_id=ke.id,
        jumlah=jumlah,
        jenis=jenis,
        keterangan=keterangan.strip(),
        dibuat_oleh=user.id,
    )
    session.add(transfer)
    await session.flush()
    return transfer


async def create_transfer(session: AsyncSession, user: BlUser, payload: TransferIn) -> BlTransfer:
    dari = await _akun_or_404(session, payload.dari_akun_id)
    ke = await _akun_or_404(session, payload.ke_akun_id)
    if not (boleh_akses_akun(user, dari) and boleh_akses_akun(user, ke)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akun ini hanya bisa diakses admin")
    jenis = f"pengisian_{ke.jenis}" if ke.jenis in JENIS_IMPRESET else "biasa"
    return await _buat_transfer(
        session, user, tanggal=payload.tanggal, dari=dari, ke=ke, jumlah=payload.jumlah,
        jenis=jenis, keterangan=payload.keterangan,
    )


async def list_transfer(
    session: AsyncSession,
    user: BlUser,
    *,
    dari: date | None = None,
    sampai: date | None = None,
    termasuk_batal: bool = False,
    limit: int = 200,
) -> list[BlTransfer]:
    """Riwayat transfer antar akun (terbaru dulu). Transfer yang menyentuh kas iklan hanya terlihat oleh admin."""
    stmt = select(BlTransfer).order_by(BlTransfer.tanggal.desc(), BlTransfer.created_at.desc()).limit(limit)
    if not _is_admin(user):
        iklan = select(BlAkunKas.id).where(BlAkunKas.jenis == "kas_iklan")
        stmt = stmt.where(BlTransfer.dari_akun_id.not_in(iklan), BlTransfer.ke_akun_id.not_in(iklan))
    if dari:
        stmt = stmt.where(BlTransfer.tanggal >= dari)
    if sampai:
        stmt = stmt.where(BlTransfer.tanggal <= sampai)
    if not termasuk_batal:
        stmt = stmt.where(BlTransfer.dibatalkan.is_(False))
    return list((await session.execute(stmt)).scalars())


async def batalkan_transfer(session: AsyncSession, transfer_id: str, alasan: str, user: BlUser | None = None) -> BlTransfer:
    transfer = await session.get(BlTransfer, transfer_id)
    if not transfer:
        raise _bad("Transfer tidak ditemukan", status.HTTP_404_NOT_FOUND)
    if user is not None:  # akun kas iklan hanya boleh diurus admin
        for akun_id in (transfer.dari_akun_id, transfer.ke_akun_id):
            akun = await session.get(BlAkunKas, akun_id)
            if akun is not None and not boleh_akses_akun(user, akun):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akun ini hanya bisa diakses admin")
    _batalkan(transfer, alasan)
    await session.flush()
    return transfer


async def _akun_imprest(session: AsyncSession, jenis: str) -> BlAkunKas:
    akun = (
        await session.execute(select(BlAkunKas).where(BlAkunKas.jenis == jenis, BlAkunKas.aktif.is_(True)))
    ).scalars().first()
    if not akun:
        raise _bad(f"Akun {jenis} belum dibuat (jalankan seed-now)", status.HTTP_409_CONFLICT)
    return akun


async def hitung_pengisian(session: AsyncSession, jenis: str) -> dict:
    """Berapa yang perlu diisi agar akun imprest (kas_kecil / kas_iklan) kembali ke plafon."""
    akun = await _akun_imprest(session, jenis)
    kas_utama = await _akun_by_kode(session, KODE_KAS_UTAMA)
    saldo = await saldo_akun(session, akun)
    perlu = max(Decimal(akun.plafon) - saldo, Decimal("0"))
    saldo_utama = await saldo_akun(session, kas_utama)
    return {
        "akun_id": akun.id,
        "plafon": Decimal(akun.plafon),
        "saldo": saldo,
        "perlu_diisi": perlu,
        "saldo_kas_utama": saldo_utama,
        "cukup": saldo_utama >= perlu,
    }


async def catat_pengisian(session: AsyncSession, user: BlUser, jenis: str) -> BlTransfer:
    """Kembalikan akun imprest ke plafon dengan transfer dari kas utama (owner/admin)."""
    info = await hitung_pengisian(session, jenis)
    nama = "kas kecil" if jenis == "kas_kecil" else "kas iklan"
    if info["perlu_diisi"] <= 0:
        raise _bad(f"{nama.capitalize()} sudah sesuai plafon, tidak perlu diisi")
    akun = await _akun_imprest(session, jenis)
    kas_utama = await _akun_by_kode(session, KODE_KAS_UTAMA)
    return await _buat_transfer(
        session, user, tanggal=None, dari=kas_utama, ke=akun, jumlah=info["perlu_diisi"],
        jenis=f"pengisian_{jenis}", keterangan=f"Pengisian {nama} ke plafon ({_hari_ini():%d-%m-%Y})",
    )


async def hitung_pengisian_kas_kecil(session: AsyncSession) -> dict:
    return await hitung_pengisian(session, "kas_kecil")


async def catat_pengisian_kas_kecil(session: AsyncSession, user: BlUser) -> BlTransfer:
    return await catat_pengisian(session, user, "kas_kecil")


# --- Profil UMKM & proporsi bagi hasil -----------------------------------------------


async def get_profil(session: AsyncSession) -> BlProfil:
    profil = await session.get(BlProfil, PROFIL_ID)
    if profil is None:
        profil = BlProfil(id=PROFIL_ID)
        session.add(profil)
        await session.flush()
    return profil


async def update_profil(session: AsyncSession, payload: ProfilIn) -> BlProfil:
    profil = await get_profil(session)
    for kolom, nilai in payload.model_dump().items():
        setattr(profil, kolom, nilai.strip() if isinstance(nilai, str) else nilai)
    await session.flush()
    return profil


def nama_usaha_pada(profil: BlProfil, tanggal: date) -> str:
    """Nama badan usaha yang berlaku pada `tanggal` dokumen (CV sebelum tanggal peralihan, PT sesudahnya)."""
    mulai = profil.nama_usaha_berlaku_mulai
    if profil.nama_usaha_lama and mulai and tanggal < mulai:
        return profil.nama_usaha_lama
    return profil.nama_usaha


PENERIMA_BAGI_HASIL = ("admin", "owner")


async def get_proporsi(session: AsyncSession) -> list[BlProporsiBagiHasil]:
    rows = list((await session.execute(select(BlProporsiBagiHasil))).scalars())
    return sorted(rows, key=lambda r: PENERIMA_BAGI_HASIL.index(r.penerima))


async def set_proporsi(session: AsyncSession, payload: ProporsiIn) -> list[BlProporsiBagiHasil]:
    """Ubah proporsi bagi hasil admin/owner (hanya admin). Total persen harus tepat 100."""
    total = payload.persen_admin + payload.persen_owner
    if total != Decimal("100"):
        raise _bad(f"Total proporsi harus 100%, sekarang {total}%")
    nilai = {"admin": payload.persen_admin, "owner": payload.persen_owner}
    ada = {r.penerima: r for r in await get_proporsi(session)}
    for penerima, persen in nilai.items():
        if penerima in ada:
            ada[penerima].persen = persen
        else:
            session.add(BlProporsiBagiHasil(penerima=penerima, persen=persen))
    await session.flush()
    return await get_proporsi(session)
