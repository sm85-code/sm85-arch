"""Business logic -- bumi_lestari Tahap 1 (keuangan dasar + kas kecil)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.security import hash_password, verify_password
from tenants.bumi_lestari.modules.bumi_lestari.application import kolom_core
from tenants.bumi_lestari.modules.bumi_lestari.application import iklan_core, kategori_core
from tenants.bumi_lestari.modules.bumi_lestari.application.audit_core import (
    bulan_tertutup,
    catat_audit,
    periode_dari,
    periode_tertutup,
    pesan_bulan_tertutup,
)
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
    JENIS_KEWAJIBAN,
    JENIS_TRANSAKSI,
    KODE_KAS_UTAMA,
    KODE_TALANGAN,
    PROFIL_ID,
    STATUS_DRAF,
    STATUS_TERKIRIM,
    BlAkunKas,
    BlKategori,
    BlProfil,
    BlProporsiBagiHasil,
    BlTransaksi,
    BlTransfer,
    BlUser,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_iklan import BlPlatformIklan
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_talangan import REF_TALANGAN, BlTalangan

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
    cabut_sesi = (data.get("role") not in (None, user.role)) or (data.get("aktif") is False and user.aktif)
    for kolom, nilai in data.items():
        if nilai is not None:
            setattr(user, kolom, nilai.strip() if isinstance(nilai, str) else nilai)
    if cabut_sesi:  # ganti role / nonaktif: token lama langsung tidak berlaku
        naikkan_versi_sesi(user)
    await session.flush()
    return user


def naikkan_versi_sesi(user: BlUser) -> None:
    """Cabut semua token yang sudah diterbitkan untuk pengguna ini."""
    user.session_version = int(user.session_version or 0) + 1


async def reset_password(session: AsyncSession, user_id: str, payload: ResetPasswordIn) -> BlUser:
    """Admin menyetel password baru untuk pengguna lain; pengguna wajib menggantinya saat login."""
    user = await session.get(BlUser, user_id)
    if user is None:
        raise _bad("Pengguna tidak ditemukan", status.HTTP_404_NOT_FOUND)
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = True
    naikkan_versi_sesi(user)
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
    naikkan_versi_sesi(user)  # perangkat lain keluar; router menerbitkan token baru untuk perangkat ini
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
        raise _bad(f"Akun {kode} belum dibuat. Aplikasi belum siap dipakai, hubungi admin.", status.HTTP_409_CONFLICT)
    return akun


async def get_kas_kecil(session: AsyncSession) -> BlAkunKas:
    akun = (
        await session.execute(select(BlAkunKas).where(BlAkunKas.jenis == "kas_kecil", BlAkunKas.aktif.is_(True)))
    ).scalars().first()
    if not akun:
        raise _bad("Akun kas kecil belum dibuat. Aplikasi belum siap dipakai, hubungi admin.", status.HTTP_409_CONFLICT)
    return akun


async def _sum(session: AsyncSession, stmt) -> Decimal:
    return Decimal(str((await session.execute(stmt)).scalar_one() or 0))


def hanya_terkirim(stmt):
    """Filter buku besar: entri draf (belum "Kirim ke laporan keuangan") tidak dihitung."""
    return stmt.where(BlTransaksi.status_kirim == STATUS_TERKIRIM)


async def saldo_akun(
    session: AsyncSession, akun: BlAkunKas, sampai: date | None = None, *, termasuk_draf: bool = False
) -> Decimal:
    """saldo_awal + masuk - keluar + transfer masuk - transfer keluar (baris batal diabaikan).
    `sampai`: saldo pada akhir hari itu (kosong = saldo terkini).
    `termasuk_draf`: False = saldo resmi (laporan); True = uang fisik (dipakai untuk cek saldo cukup)."""

    def trx(jenis: str):
        stmt = select(func.coalesce(func.sum(BlTransaksi.jumlah), 0)).where(
            BlTransaksi.akun_id == akun.id, BlTransaksi.jenis == jenis, BlTransaksi.dibatalkan.is_(False)
        )
        if not termasuk_draf:
            stmt = hanya_terkirim(stmt)
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


async def list_akun(session: AsyncSession, user: BlUser) -> list[tuple[BlAkunKas, Decimal, Decimal]]:
    """(akun, saldo resmi, saldo setelah draf)."""
    stmt = select(BlAkunKas).where(BlAkunKas.aktif.is_(True), BlAkunKas.jenis != JENIS_KEWAJIBAN).order_by(BlAkunKas.created_at)
    if _is_staff(user):
        stmt = stmt.where(BlAkunKas.jenis == "kas_kecil")
    akuns = [a for a in (await session.execute(stmt)).scalars() if boleh_akses_akun(user, a)]
    return [(a, await saldo_akun(session, a), await saldo_akun(session, a, termasuk_draf=True)) for a in akuns]


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
    # Uang fisik: draf yang belum dikirim tetap sudah keluar/masuk dari kas.
    if await saldo_akun(session, akun, termasuk_draf=True) < jumlah:
        raise _bad(f"Saldo {akun.nama} tidak cukup")


async def pastikan_bulan_terbuka(session: AsyncSession, tanggal: date) -> None:
    if await bulan_tertutup(session, tanggal):
        raise _bad(pesan_bulan_tertutup(periode_dari(tanggal)), 409)


def status_awal_transaksi(akun: BlAkunKas) -> str:
    """Kas kecil & kas iklan masuk laporan lewat posting berkelompok; akun lain langsung terkirim."""
    return STATUS_DRAF if akun.jenis in JENIS_IMPRESET else STATUS_TERKIRIM


async def _cek_kategori_manual(
    session: AsyncSession, user: BlUser, akun: BlAkunKas, kategori: BlKategori, payload: TransaksiIn
) -> None:
    """Aturan kategori entri manual (spesifikasi 6.2, 8.6, 8.7, 8.12)."""
    nama = kategori.nama
    if kategori_core.is_sistem(nama):
        lewat = (
            " Pemasukan marketplace/Toko web dicatat lewat Pencairan > Catat manual."
            if nama in (kategori_core.KATEGORI_PENJUALAN_MARKETPLACE, kategori_core.KATEGORI_PENJUALAN_WEB) else ""
        )
        raise _bad(
            f"Kategori '{nama}' diisi otomatis oleh aplikasi dan tidak bisa dipakai di catatan manual.{lewat}",
            422,
        )
    if _is_staff(user) and not kategori_core.untuk_staf(nama):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Staf hanya boleh memilih Transport, Packing, Operasional, atau Lainnya",
        )
    if nama in kategori_core.KATEGORI_KHUSUS_ADMIN and not _is_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Kategori '{nama}' khusus admin")
    if (nama == kategori_core.KATEGORI_BIAYA_IKLAN) != (akun.jenis == "kas_iklan"):
        raise _bad(
            "Biaya iklan hanya dicatat dari Kas iklan, dan Kas iklan hanya untuk biaya iklan",
            422,
        )
    if nama == kategori_core.KATEGORI_SETORAN_MODAL:
        if akun.jenis in JENIS_IMPRESET:
            raise _bad("Setoran modal masuk ke Kas utama atau rekening, bukan kas kecil/kas iklan", 422)
        sudah = (
            await session.execute(
                select(BlTransaksi.id).where(
                    BlTransaksi.kategori_id == kategori.id, BlTransaksi.dibatalkan.is_(False)
                )
            )
        ).first()
        if sudah and not payload.konfirmasi_setoran_modal_kedua:
            raise _bad(
                "Setoran modal sudah pernah dicatat. Konfirmasi bila ini memang setoran modal tambahan.",
                status.HTTP_409_CONFLICT,
            )


async def create_transaksi(session: AsyncSession, user: BlUser, payload: TransaksiIn) -> BlTransaksi:
    if payload.jenis not in JENIS_TRANSAKSI:
        raise _bad(f"Jenis transaksi harus salah satu dari: {', '.join(JENIS_TRANSAKSI)}")
    akun = await _akun_or_404(session, payload.akun_id)
    if akun.jenis == JENIS_KEWAJIBAN:
        raise _bad("Akun talangan tidak bisa dipakai langsung; catat pengeluaran kas kecil/kas iklan dengan talangan")
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
    await _cek_kategori_manual(session, user, akun, kategori, payload)
    tanggal = payload.tanggal or _hari_ini()
    await pastikan_bulan_terbuka(session, tanggal)
    if payload.koreksi_periode is not None:
        if payload.koreksi_periode >= periode_dari(tanggal) or not await periode_tertutup(session, payload.koreksi_periode):
            raise _bad("Koreksi bulan lalu hanya untuk bulan yang sudah tutup buku, dicatat di bulan berjalan", 422)
    # Top up kas iklan wajib memilih platform; ditandai bila melebihi porsi grup bulan ini (AB-KI-3/4, KP-KI-1/2).
    iklan: dict = {}
    if akun.jenis == "kas_iklan" and payload.jenis == "keluar":
        platform = await session.get(BlPlatformIklan, payload.platform_iklan_id) if payload.platform_iklan_id else None
        if platform is None or not platform.aktif:
            raise _bad("Pilih platform iklan untuk top up kas iklan", 422)
        iklan = {
            "platform_iklan_id": platform.id,
            "melebihi_porsi": await iklan_core.melebihi_porsi(session, platform, tanggal, payload.jumlah),
        }
    elif payload.platform_iklan_id:
        raise _bad("Platform iklan hanya untuk pengeluaran kas iklan")
    # Akun imprest (kas kecil, kas iklan) berplafon: tidak boleh minus. Kekurangannya boleh dicatat sebagai
    # talangan atas nama seseorang (AB-TL-1): biaya tetap penuh, bagian akun asal hanya sebesar saldonya.
    kt = await kolom_core.nilai_baru(session, "transaksi", payload.kolom_tambahan, user=user)
    if akun.jenis in JENIS_IMPRESET and payload.jenis == "keluar":
        saldo = await saldo_akun(session, akun, termasuk_draf=True)
        if saldo < payload.jumlah:
            if not (payload.talangan_oleh or "").strip():
                raise _bad(f"Saldo {akun.nama} tidak cukup")
            return await _catat_dengan_talangan(
                session, user, payload, akun, kategori, tanggal, max(saldo, Decimal("0")), iklan, kt,
            )
    elif payload.talangan_oleh:
        raise _bad("Talangan hanya untuk pengeluaran kas kecil/kas iklan")
    trx = BlTransaksi(
        tanggal=tanggal,
        akun_id=akun.id,
        kategori_id=kategori.id,
        jenis=payload.jenis,
        jumlah=payload.jumlah,
        keterangan=payload.keterangan.strip(),
        dibuat_oleh=user.id,
        status_kirim=status_awal_transaksi(akun),
        koreksi_periode=payload.koreksi_periode,
        kolom_tambahan=kt,
        **iklan,
    )
    session.add(trx)
    await session.flush()
    if kategori.nama == kategori_core.KATEGORI_SETORAN_MODAL:
        await catat_audit(
            session, user.id, "setoran_modal", "transaksi", trx.id,
            sesudah={"tanggal": trx.tanggal, "jumlah": trx.jumlah, "akun": akun.kode},
            alasan="konfirmasi setoran modal tambahan" if payload.konfirmasi_setoran_modal_kedua else None,
        )
    return trx


async def list_transaksi(
    session: AsyncSession,
    user: BlUser,
    *,
    akun_id: str | None = None,
    dari: date | None = None,
    sampai: date | None = None,
    termasuk_batal: bool = False,
    termasuk_draf: bool = False,
    hanya_draf: bool = False,
) -> list[BlTransaksi]:
    """Default hanya entri terkirim (buku besar). Halaman sumber (Kas kecil, Kas iklan) meminta draf juga."""
    stmt = select(BlTransaksi).order_by(BlTransaksi.tanggal.desc(), BlTransaksi.created_at.desc())
    if _is_staff(user):
        akun_id = (await get_kas_kecil(session)).id  # staf: hanya kas kecil, parameter diabaikan
        termasuk_draf = True  # staf melihat catatannya sendiri walau belum dikirim
    if hanya_draf:
        stmt = stmt.where(BlTransaksi.status_kirim == STATUS_DRAF)
    elif not termasuk_draf:
        stmt = hanya_terkirim(stmt)
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


async def _catat_dengan_talangan(
    session: AsyncSession, user: BlUser, payload: TransaksiIn, akun: BlAkunKas, kategori: BlKategori,
    tanggal: date, saldo: Decimal, iklan: dict | None = None, kt: dict | None = None,
) -> BlTransaksi:
    """Pecah pengeluaran: `saldo` dari akun asal + kekurangannya di akun TALANGAN (keduanya biaya, keduanya draf
    dan ikut kiriman sumber akun asal). Mengembalikan transaksi akun asal (atau transaksi talangan bila saldo 0)."""
    talangan_akun = await _akun_by_kode(session, KODE_TALANGAN)
    nama = payload.talangan_oleh.strip()
    ket = payload.keterangan.strip()
    kurang = payload.jumlah - saldo

    def baris(akun_id: str, jumlah: Decimal, keterangan: str, ref: bool) -> BlTransaksi:
        return BlTransaksi(
            tanggal=tanggal, akun_id=akun_id, kategori_id=kategori.id, jenis="keluar", jumlah=jumlah,
            keterangan=keterangan, dibuat_oleh=user.id, status_kirim=STATUS_DRAF, koreksi_periode=payload.koreksi_periode,
            ref_jenis=REF_TALANGAN if ref else None, **(iklan or {}),
        )

    utama = baris(akun.id, saldo, ket, False) if saldo > 0 else None
    bagian = baris(talangan_akun.id, kurang, f"{ket} (talangan oleh {nama})".strip(), True)
    for x in (utama, bagian):
        if x is not None:
            x.kolom_tambahan = dict(kt or {})
    session.add_all([x for x in (utama, bagian) if x is not None])
    await session.flush()
    tl = BlTalangan(
        tanggal=tanggal, nama=nama, akun_asal_id=akun.id, transaksi_id=utama.id if utama else None,
        transaksi_talangan_id=bagian.id, jumlah=kurang, keterangan=ket, dibuat_oleh=user.id,
    )
    session.add(tl)
    await session.flush()
    bagian.ref_id = tl.id
    await catat_audit(
        session, user.id, "talangan", "talangan", tl.id,
        sesudah={"nama": nama, "jumlah": kurang, "akun": akun.kode, "total_pengeluaran": payload.jumlah},
    )
    await session.flush()
    return utama or bagian


def _batalkan(row, alasan: str) -> None:
    if row.dibatalkan:
        raise _bad("Sudah dibatalkan", status.HTTP_409_CONFLICT)
    row.dibatalkan = True
    row.dibatalkan_at = datetime.now(timezone.utc)
    row.alasan_batal = alasan.strip()


async def batalkan_transaksi(
    session: AsyncSession, trx_id: str, alasan: str, user: BlUser | None = None
) -> BlTransaksi:
    """Batal dari Kas & transaksi: hanya entri manual. Entri otomatis dibatalkan dari halaman asalnya;
    entri yang sudah dikirim ke laporan keuangan dibatalkan lewat batal kiriman."""
    trx = await session.get(BlTransaksi, trx_id)
    if not trx:
        raise _bad("Transaksi tidak ditemukan", status.HTTP_404_NOT_FOUND)
    if user is not None:
        akun = await session.get(BlAkunKas, trx.akun_id)
        if akun is not None and not boleh_akses_akun(user, akun):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akun ini hanya bisa diakses admin")
    if trx.ref_jenis:
        raise _bad("Transaksi otomatis; batalkan dari halaman asalnya", status.HTTP_409_CONFLICT)
    if (
        await session.execute(select(BlTalangan.id).where(BlTalangan.transaksi_id == trx.id, BlTalangan.dibatalkan.is_(False)))
    ).first():
        raise _bad("Pengeluaran ini punya talangan; batalkan dari daftar talangan", status.HTTP_409_CONFLICT)
    if trx.kiriman_id and trx.status_kirim == STATUS_TERKIRIM:
        raise _bad(
            "Entri ini sudah dikirim ke laporan keuangan; batalkan kirimannya dulu", status.HTTP_409_CONFLICT
        )
    await pastikan_bulan_terbuka(session, trx.tanggal)
    sebelum = {"status_kirim": trx.status_kirim, "jumlah": trx.jumlah, "tanggal": trx.tanggal}
    _batalkan(trx, alasan)
    await catat_audit(
        session, user.id if user else None, "batal", "transaksi", trx.id, sebelum=sebelum, alasan=alasan.strip()
    )
    await session.flush()
    return trx


# --- Transfer & pengisian kas kecil -------------------------------------------------


async def _buat_transfer(
    session: AsyncSession, user: BlUser, *, tanggal, dari: BlAkunKas, ke: BlAkunKas, jumlah: Decimal,
    jenis: str, keterangan: str, di_luar_jadwal: bool = False, alasan_luar_jadwal: str | None = None,
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
        di_luar_jadwal=di_luar_jadwal,
        alasan_luar_jadwal=alasan_luar_jadwal,
    )
    session.add(transfer)
    await session.flush()
    return transfer


async def create_transfer(session: AsyncSession, user: BlUser, payload: TransferIn) -> BlTransfer:
    dari = await _akun_or_404(session, payload.dari_akun_id)
    ke = await _akun_or_404(session, payload.ke_akun_id)
    if not (boleh_akses_akun(user, dari) and boleh_akses_akun(user, ke)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akun ini hanya bisa diakses admin")
    if JENIS_KEWAJIBAN in (dari.jenis, ke.jenis):
        raise _bad("Pelunasan talangan dicatat dari daftar talangan")
    jenis = f"pengisian_{ke.jenis}" if ke.jenis in JENIS_IMPRESET else "biasa"
    await pastikan_bulan_terbuka(session, payload.tanggal or _hari_ini())
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


async def batalkan_transfer(
    session: AsyncSession, transfer_id: str, alasan: str, user: BlUser | None = None, *,
    dari_halaman_asal: bool = False,
) -> BlTransfer:
    transfer = await session.get(BlTransfer, transfer_id)
    if not transfer:
        raise _bad("Transfer tidak ditemukan", status.HTTP_404_NOT_FOUND)
    if transfer.jenis == "sisihan_dana" and not dari_halaman_asal:
        raise _bad("Transfer sisihan hanya bisa dibatalkan dari halaman Sisihan", status.HTTP_409_CONFLICT)
    if transfer.jenis == "pelunasan_talangan" and not dari_halaman_asal:
        raise _bad("Pelunasan talangan hanya bisa dibatalkan dari daftar talangan", status.HTTP_409_CONFLICT)
    await pastikan_bulan_terbuka(session, transfer.tanggal)
    if user is not None:  # akun kas iklan hanya boleh diurus admin
        for akun_id in (transfer.dari_akun_id, transfer.ke_akun_id):
            akun = await session.get(BlAkunKas, akun_id)
            if akun is not None and not boleh_akses_akun(user, akun):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akun ini hanya bisa diakses admin")
    _batalkan(transfer, alasan)
    await catat_audit(
        session, user.id if user else None, "batal", "transfer", transfer.id,
        sebelum={"jenis": transfer.jenis, "jumlah": transfer.jumlah, "tanggal": transfer.tanggal}, alasan=alasan.strip(),
    )
    await session.flush()
    return transfer


async def _akun_imprest(session: AsyncSession, jenis: str) -> BlAkunKas:
    akun = (
        await session.execute(select(BlAkunKas).where(BlAkunKas.jenis == jenis, BlAkunKas.aktif.is_(True)))
    ).scalars().first()
    if not akun:
        raise _bad(f"Akun {jenis} belum dibuat. Aplikasi belum siap dipakai, hubungi admin.", status.HTTP_409_CONFLICT)
    return akun


async def hitung_pengisian(session: AsyncSession, jenis: str) -> dict:
    """Berapa yang perlu diisi agar akun imprest (kas_kecil / kas_iklan) kembali ke plafon.
    Memakai uang fisik (termasuk draf belum dikirim): isi ulang mengganti uang yang benar-benar terpakai."""
    akun = await _akun_imprest(session, jenis)
    kas_utama = await _akun_by_kode(session, KODE_KAS_UTAMA)
    saldo = await saldo_akun(session, akun, termasuk_draf=True)
    perlu = max(Decimal(akun.plafon) - saldo, Decimal("0"))
    saldo_utama = await saldo_akun(session, kas_utama, termasuk_draf=True)
    return {
        "akun_id": akun.id,
        "plafon": Decimal(akun.plafon),
        "saldo": saldo,
        "perlu_diisi": perlu,
        "saldo_kas_utama": saldo_utama,
        "cukup": saldo_utama >= perlu,
    }


async def catat_pengisian(
    session: AsyncSession, user: BlUser, jenis: str, tanggal: date | None = None, *,
    di_luar_jadwal: bool = False, alasan: str | None = None,
) -> BlTransfer:
    """Kembalikan akun imprest ke plafon dengan transfer dari kas utama (owner/admin).
    `tanggal`: tanggal isi ulang (bawaan hari ini), mis. Selasa Tutup Kas Mingguan yang dicatat belakangan."""
    tanggal = tanggal or _hari_ini()
    alasan = (alasan or "").strip() or None
    if di_luar_jadwal and (alasan is None or len(alasan) < 3):
        raise _bad("Isi ulang di luar jadwal wajib menulis alasan", 422)
    await pastikan_bulan_terbuka(session, tanggal)
    info = await hitung_pengisian(session, jenis)
    nama = "kas kecil" if jenis == "kas_kecil" else "kas iklan"
    if info["perlu_diisi"] <= 0:
        raise _bad(f"{nama.capitalize()} sudah sesuai plafon, tidak perlu diisi")
    akun = await _akun_imprest(session, jenis)
    kas_utama = await _akun_by_kode(session, KODE_KAS_UTAMA)
    return await _buat_transfer(
        session, user, tanggal=tanggal, dari=kas_utama, ke=akun, jumlah=info["perlu_diisi"],
        jenis=f"pengisian_{jenis}", keterangan=f"Pengisian {nama} ke plafon ({tanggal:%d-%m-%Y})"
        + (" — di luar jadwal" if di_luar_jadwal else ""),
        di_luar_jadwal=di_luar_jadwal, alasan_luar_jadwal=alasan if di_luar_jadwal else None,
    )


async def hitung_pengisian_kas_kecil(session: AsyncSession) -> dict:
    return await hitung_pengisian(session, "kas_kecil")


async def catat_pengisian_kas_kecil(
    session: AsyncSession, user: BlUser, tanggal: date | None = None, *, di_luar_jadwal: bool = False, alasan: str | None = None
) -> BlTransfer:
    return await catat_pengisian(session, user, "kas_kecil", tanggal, di_luar_jadwal=di_luar_jadwal, alasan=alasan)


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
