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
    LoginIn,
    TransaksiIn,
    TransferIn,
    UserCreateIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import (
    JENIS_AKUN,
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


def _hari_ini() -> date:
    return datetime.now(WIB).date()


# --- Auth / users ---------------------------------------------------------------


async def authenticate_user(session: AsyncSession, payload: LoginIn) -> BlUser:
    user = (await session.execute(select(BlUser).where(BlUser.email == payload.email))).scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        raise _bad("Email atau password salah", status.HTTP_401_UNAUTHORIZED)
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


async def saldo_akun(session: AsyncSession, akun: BlAkunKas) -> Decimal:
    """saldo_awal + masuk - keluar + transfer masuk - transfer keluar (baris batal diabaikan)."""

    def trx(jenis: str):
        return select(func.coalesce(func.sum(BlTransaksi.jumlah), 0)).where(
            BlTransaksi.akun_id == akun.id, BlTransaksi.jenis == jenis, BlTransaksi.dibatalkan.is_(False)
        )

    def trf(kolom):
        return select(func.coalesce(func.sum(BlTransfer.jumlah), 0)).where(
            kolom == akun.id, BlTransfer.dibatalkan.is_(False)
        )

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
    akuns = list((await session.execute(stmt)).scalars())
    return [(a, await saldo_akun(session, a)) for a in akuns]


async def create_akun(session: AsyncSession, payload: AkunKasIn) -> BlAkunKas:
    if payload.jenis not in JENIS_AKUN:
        raise _bad(f"Jenis akun harus salah satu dari: {', '.join(JENIS_AKUN)}")
    if payload.jenis == "kas_kecil" and payload.plafon is None:
        raise _bad("Kas kecil wajib punya plafon")
    if payload.jenis != "kas_kecil" and payload.plafon is not None:
        raise _bad("Plafon hanya untuk akun kas kecil")
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
    if _is_staff(user) and not (akun.jenis == "kas_kecil" and payload.jenis == "keluar"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Staf hanya boleh mencatat pengeluaran kas kecil"
        )
    # Kas kecil adalah uang fisik dengan plafon: tidak boleh minus.
    if akun.jenis == "kas_kecil" and payload.jenis == "keluar":
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
        stmt = stmt.where(BlTransaksi.akun_id == akun_id)
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
    jenis = "pengisian_kas_kecil" if ke.jenis == "kas_kecil" else "biasa"
    return await _buat_transfer(
        session, user, tanggal=payload.tanggal, dari=dari, ke=ke, jumlah=payload.jumlah,
        jenis=jenis, keterangan=payload.keterangan,
    )


async def batalkan_transfer(session: AsyncSession, transfer_id: str, alasan: str) -> BlTransfer:
    transfer = await session.get(BlTransfer, transfer_id)
    if not transfer:
        raise _bad("Transfer tidak ditemukan", status.HTTP_404_NOT_FOUND)
    _batalkan(transfer, alasan)
    await session.flush()
    return transfer


async def hitung_pengisian_kas_kecil(session: AsyncSession) -> dict:
    kas_kecil = await get_kas_kecil(session)
    kas_utama = await _akun_by_kode(session, KODE_KAS_UTAMA)
    saldo = await saldo_akun(session, kas_kecil)
    perlu = max(Decimal(kas_kecil.plafon) - saldo, Decimal("0"))
    saldo_utama = await saldo_akun(session, kas_utama)
    return {
        "akun_id": kas_kecil.id,
        "plafon": Decimal(kas_kecil.plafon),
        "saldo": saldo,
        "perlu_diisi": perlu,
        "saldo_kas_utama": saldo_utama,
        "cukup": saldo_utama >= perlu,
    }


async def catat_pengisian_kas_kecil(session: AsyncSession, user: BlUser) -> BlTransfer:
    """Kembalikan kas kecil ke plafon dengan transfer dari kas utama (owner)."""
    info = await hitung_pengisian_kas_kecil(session)
    if info["perlu_diisi"] <= 0:
        raise _bad("Kas kecil sudah sesuai plafon, tidak perlu diisi")
    kas_kecil = await get_kas_kecil(session)
    kas_utama = await _akun_by_kode(session, KODE_KAS_UTAMA)
    return await _buat_transfer(
        session, user, tanggal=None, dari=kas_utama, ke=kas_kecil, jumlah=info["perlu_diisi"],
        jenis="pengisian_kas_kecil", keterangan=f"Pengisian kas kecil ke plafon ({_hari_ini():%d-%m-%Y})",
    )


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
        setattr(profil, kolom, nilai.strip())
    await session.flush()
    return profil


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
