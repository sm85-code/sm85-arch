"""Kas iklan (spesifikasi 8.7, Fase 2.9/2.10): platform iklan, budget 25/75, top up = Biaya iklan (draf),
pengembalian kas iklan ke Kas utama, perubahan plafon kas kecil/kas iklan beserta lognya."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application import iklan_core, kategori_core
from tenants.bumi_lestari.modules.bumi_lestari.application.audit_core import catat_audit
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas import TransaksiIn
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_iklan import (
    BudgetGrupOut,
    BudgetIklanOut,
    PengaturanIklanIn,
    PengaturanIklanOut,
    PengembalianIklanIn,
    PlafonIn,
    PlatformIklanIn,
    PlatformIklanPatch,
    TopupIklanIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.services import (
    _akun_by_kode,
    _akun_imprest,
    _akun_or_404,
    _buat_transfer,
    _hari_ini,
    boleh_akses_akun,
    create_transaksi,
    get_profil,
    pastikan_bulan_terbuka,
    saldo_akun,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import (
    JENIS_IMPRESET,
    KODE_KAS_UTAMA,
    BlKategori,
    BlTransaksi,
    BlTransfer,
    BlUser,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_iklan import BlPlafonLog, BlPlatformIklan
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import BlSaluran


def _bad(detail: str, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


# ---- platform ----

async def list_platform(session: AsyncSession) -> list[BlPlatformIklan]:
    return list((await session.execute(select(BlPlatformIklan).order_by(BlPlatformIklan.grup, BlPlatformIklan.nama))).scalars())


async def _cek_platform(session: AsyncSession, nama: str | None, saluran_id: str | None, kecuali: str | None = None) -> None:
    if nama is not None:
        stmt = select(BlPlatformIklan.id).where(BlPlatformIklan.nama == nama.strip())
        if kecuali:
            stmt = stmt.where(BlPlatformIklan.id != kecuali)
        if (await session.execute(stmt)).first():
            raise _bad("Nama platform sudah ada", status.HTTP_409_CONFLICT)
    if saluran_id and await session.get(BlSaluran, saluran_id) is None:
        raise _bad("Saluran tidak ditemukan", status.HTTP_404_NOT_FOUND)


async def create_platform(session: AsyncSession, payload: PlatformIklanIn) -> BlPlatformIklan:
    await _cek_platform(session, payload.nama, payload.saluran_id)
    p = BlPlatformIklan(nama=payload.nama.strip(), grup=payload.grup, saluran_id=payload.saluran_id)
    session.add(p)
    await session.flush()
    return p


async def update_platform(session: AsyncSession, platform_id: str, payload: PlatformIklanPatch) -> BlPlatformIklan:
    p = await session.get(BlPlatformIklan, platform_id)
    if not p:
        raise _bad("Platform tidak ditemukan", status.HTTP_404_NOT_FOUND)
    data = payload.model_dump(exclude_unset=True)
    await _cek_platform(session, data.get("nama"), data.get("saluran_id"), kecuali=p.id)
    for k, v in data.items():
        setattr(p, k, v.strip() if k == "nama" else v)
    await session.flush()
    return p


# ---- budget & pengaturan ----

async def sisa_budget(session: AsyncSession, tanggal: date | None = None) -> BudgetIklanOut:
    b = await iklan_core.budget_iklan(session, tanggal or _hari_ini())
    return BudgetIklanOut(periode=b.periode, budget_total=b.budget_total, dasar=b.dasar, grup=[BudgetGrupOut(**g.__dict__) for g in b.grup])


async def get_pengaturan(session: AsyncSession) -> PengaturanIklanOut:
    p = await get_profil(session)
    return PengaturanIklanOut(porsi_internal=p.porsi_iklan_internal, porsi_eksternal=p.porsi_iklan_eksternal, budget_bulanan=p.budget_iklan_bulanan)


async def set_pengaturan(session: AsyncSession, user: BlUser, payload: PengaturanIklanIn) -> PengaturanIklanOut:
    if payload.porsi_internal + payload.porsi_eksternal != 100:
        raise _bad("Porsi internal + eksternal harus 100%", 422)
    p = await get_profil(session)
    sebelum = {"internal": p.porsi_iklan_internal, "eksternal": p.porsi_iklan_eksternal, "budget": p.budget_iklan_bulanan}
    p.porsi_iklan_internal, p.porsi_iklan_eksternal, p.budget_iklan_bulanan = payload.porsi_internal, payload.porsi_eksternal, payload.budget_bulanan
    await catat_audit(session, user.id, "ubah", "budget_iklan", p.id, sebelum=sebelum, sesudah=payload.model_dump())
    await session.flush()
    return await get_pengaturan(session)


# ---- top up & pengembalian ----

async def topup(session: AsyncSession, user: BlUser, payload: TopupIklanIn) -> BlTransaksi:
    """Top up = Biaya iklan dari Kas iklan (draf; masuk laporan saat dikirim)."""
    akun = await _akun_imprest(session, "kas_iklan")
    kategori = (await session.execute(select(BlKategori).where(BlKategori.nama == kategori_core.KATEGORI_BIAYA_IKLAN))).scalar_one_or_none()
    if kategori is None:
        raise _bad("Kategori Biaya iklan belum ada. Jalankan penyiapan data awal.", status.HTTP_409_CONFLICT)
    return await create_transaksi(
        session, user,
        TransaksiIn(
            tanggal=payload.tanggal, akun_id=akun.id, kategori_id=kategori.id, jenis="keluar", jumlah=payload.jumlah,
            keterangan=payload.keterangan, platform_iklan_id=payload.platform_iklan_id, talangan_oleh=payload.talangan_oleh,
        ),
    )


async def pengembalian(session: AsyncSession, user: BlUser, payload: PengembalianIklanIn) -> BlTransfer:
    """Kas iklan → Kas utama (AB-KI-5/6): bawaan sebesar kelebihan di atas plafon; boleh jumlah lain (mis. iklan dihentikan)."""
    akun = await _akun_imprest(session, "kas_iklan")
    tanggal = payload.tanggal or _hari_ini()
    await pastikan_bulan_terbuka(session, tanggal)
    jumlah = payload.jumlah
    if jumlah is None:
        jumlah = await saldo_akun(session, akun, termasuk_draf=True) - Decimal(akun.plafon or 0)
        if jumlah <= 0:
            raise _bad("Saldo kas iklan tidak melebihi plafon; tulis jumlah yang dikembalikan")
    return await _buat_transfer(
        session, user, tanggal=tanggal, dari=akun, ke=await _akun_by_kode(session, KODE_KAS_UTAMA), jumlah=jumlah,
        jenis="pengembalian_kas_iklan", keterangan=payload.keterangan or "Pengembalian kas iklan ke Kas utama",
    )


# ---- plafon ----

async def ubah_plafon(session: AsyncSession, user: BlUser, akun_id: str, payload: PlafonIn) -> BlPlafonLog:
    akun = await _akun_or_404(session, akun_id)
    if akun.jenis not in JENIS_IMPRESET:
        raise _bad("Plafon hanya untuk kas kecil/kas iklan")
    if not boleh_akses_akun(user, akun):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akun ini hanya bisa diakses admin")
    dari = Decimal(akun.plafon or 0)
    if dari == payload.plafon:
        raise _bad("Plafon tidak berubah")
    akun.plafon = payload.plafon
    log = BlPlafonLog(akun_id=akun.id, tanggal=_hari_ini(), dari=dari, ke=payload.plafon, oleh=user.id, alasan=payload.alasan.strip())
    session.add(log)
    await catat_audit(session, user.id, "ubah_plafon", "akun_kas", akun.id, sebelum={"plafon": dari}, sesudah={"plafon": payload.plafon}, alasan=payload.alasan.strip() or None)
    await session.flush()
    return log


async def log_plafon(session: AsyncSession, user: BlUser, akun_id: str) -> list[BlPlafonLog]:
    akun = await _akun_or_404(session, akun_id)
    if not boleh_akses_akun(user, akun):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akun ini hanya bisa diakses admin")
    return list((await session.execute(select(BlPlafonLog).where(BlPlafonLog.akun_id == akun.id).order_by(BlPlafonLog.created_at.desc()))).scalars())
