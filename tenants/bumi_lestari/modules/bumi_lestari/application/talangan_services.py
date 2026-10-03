"""Talangan: daftar per orang, pelunasan dari Kas utama (boleh sebagian), pembatalan (spesifikasi 8.8, 11.3).

Pencatatan talangan terjadi saat pengeluaran kas kecil/kas iklan disimpan dengan `talangan_oleh`
(`services.create_transaksi`). Pelunasan = transfer Kas utama → akun TALANGAN, tidak menambah biaya.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application.audit_core import catat_audit
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_talangan import LunasiTalanganIn, TalanganBayarOut, TalanganOut
from tenants.bumi_lestari.modules.bumi_lestari.application.services import (
    _akun_by_kode,
    _buat_transfer,
    _hari_ini,
    batalkan_transfer,
    boleh_akses_akun,
    pastikan_bulan_terbuka,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import (
    KODE_KAS_UTAMA,
    KODE_TALANGAN,
    STATUS_TERKIRIM,
    BlAkunKas,
    BlTransaksi,
    BlUser,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_talangan import BlTalangan, BlTalanganBayar

NOL = Decimal("0")


def _bad(detail: str, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


async def _keluaran(session: AsyncSession, tl: BlTalangan, akun: BlAkunKas) -> TalanganOut:
    bayar = list(
        (await session.execute(select(BlTalanganBayar).where(BlTalanganBayar.talangan_id == tl.id).order_by(BlTalanganBayar.created_at))).scalars()
    )
    terbayar = sum((Decimal(b.jumlah) for b in bayar if not b.dibatalkan), NOL)
    bagian = await session.get(BlTransaksi, tl.transaksi_talangan_id)
    utama = await session.get(BlTransaksi, tl.transaksi_id) if tl.transaksi_id else None
    return TalanganOut(
        id=tl.id, tanggal=tl.tanggal, nama=tl.nama, akun_asal_id=akun.id, akun_asal_nama=akun.nama,
        kategori_id=bagian.kategori_id, keterangan=tl.keterangan,
        total_pengeluaran=Decimal(tl.jumlah) + (Decimal(utama.jumlah) if utama else NOL), jumlah=Decimal(tl.jumlah),
        terbayar=terbayar, sisa=NOL if tl.dibatalkan else Decimal(tl.jumlah) - terbayar, status_kirim=bagian.status_kirim,
        dibatalkan=tl.dibatalkan, alasan_batal=tl.alasan_batal, created_at=tl.created_at,
        bayar=[TalanganBayarOut(id=b.id, transfer_id=b.transfer_id, tanggal=b.tanggal, jumlah=b.jumlah, dibatalkan=b.dibatalkan) for b in bayar],
    )


async def list_talangan(
    session: AsyncSession, user: BlUser, *, status_filter: str = "semua", nama: str | None = None
) -> list[TalanganOut]:
    """status_filter: belum_lunas | lunas | semua (termasuk dibatalkan). Talangan kas iklan hanya untuk admin."""
    stmt = (
        select(BlTalangan, BlAkunKas)
        .join(BlAkunKas, BlAkunKas.id == BlTalangan.akun_asal_id)
        .order_by(BlTalangan.tanggal.desc(), BlTalangan.created_at.desc())
    )
    if status_filter != "semua":
        stmt = stmt.where(BlTalangan.dibatalkan.is_(False))
    if nama:
        stmt = stmt.where(BlTalangan.nama == nama)
    hasil = []
    for tl, akun in (await session.execute(stmt)).all():
        if not boleh_akses_akun(user, akun):
            continue
        out = await _keluaran(session, tl, akun)
        if status_filter == "belum_lunas" and out.sisa <= 0:
            continue
        if status_filter == "lunas" and out.sisa > 0:
            continue
        hasil.append(out)
    return hasil


async def daftar_nama(session: AsyncSession) -> list[str]:
    """Nama yang pernah menalangi + nama pengguna aktif (saran isian "talangan oleh")."""
    nama = {n for (n,) in (await session.execute(select(BlTalangan.nama).distinct())).all()}
    nama |= {n for (n,) in (await session.execute(select(BlUser.nama).where(BlUser.aktif.is_(True)))).all() if n}
    return sorted(nama, key=str.lower)


async def _ambil(session: AsyncSession, user: BlUser, talangan_id: str) -> tuple[BlTalangan, BlAkunKas]:
    tl = await session.get(BlTalangan, talangan_id)
    if not tl:
        raise _bad("Talangan tidak ditemukan", status.HTTP_404_NOT_FOUND)
    akun = await session.get(BlAkunKas, tl.akun_asal_id)
    if not boleh_akses_akun(user, akun):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Talangan kas iklan hanya bisa diurus admin")
    return tl, akun


async def lunasi(session: AsyncSession, user: BlUser, talangan_id: str, payload: LunasiTalanganIn) -> TalanganOut:
    tl, akun = await _ambil(session, user, talangan_id)
    if tl.dibatalkan:
        raise _bad("Talangan sudah dibatalkan", status.HTTP_409_CONFLICT)
    sisa = (await _keluaran(session, tl, akun)).sisa
    if sisa <= 0:
        raise _bad("Talangan sudah lunas", status.HTTP_409_CONFLICT)
    jumlah = payload.jumlah or sisa
    if jumlah > sisa:
        raise _bad(f"Melebihi sisa talangan (Rp {sisa:,.0f})".replace(",", "."))
    tanggal = payload.tanggal or _hari_ini()
    await pastikan_bulan_terbuka(session, tanggal)
    transfer = await _buat_transfer(
        session, user, tanggal=tanggal, dari=await _akun_by_kode(session, KODE_KAS_UTAMA),
        ke=await _akun_by_kode(session, KODE_TALANGAN), jumlah=jumlah, jenis="pelunasan_talangan",
        keterangan=f"Pelunasan talangan {tl.nama}" + (f": {tl.keterangan}" if tl.keterangan else ""),
    )
    session.add(BlTalanganBayar(talangan_id=tl.id, transfer_id=transfer.id, tanggal=tanggal, jumlah=jumlah, dibuat_oleh=user.id))
    await session.flush()
    return await _keluaran(session, tl, akun)


async def batal_bayar(session: AsyncSession, user: BlUser, bayar_id: str, alasan: str) -> TalanganOut:
    bayar = await session.get(BlTalanganBayar, bayar_id)
    if not bayar:
        raise _bad("Pelunasan tidak ditemukan", status.HTTP_404_NOT_FOUND)
    tl, akun = await _ambil(session, user, bayar.talangan_id)
    if bayar.dibatalkan:
        raise _bad("Sudah dibatalkan", status.HTTP_409_CONFLICT)
    await batalkan_transfer(session, bayar.transfer_id, alasan, user, dari_halaman_asal=True)
    bayar.dibatalkan = True
    await session.flush()
    return await _keluaran(session, tl, akun)


async def batal_talangan(session: AsyncSession, user: BlUser, talangan_id: str, alasan: str) -> TalanganOut:
    """Batalkan pengeluaran bertalangan seluruhnya (bagian akun asal + bagian talangan)."""
    tl, akun = await _ambil(session, user, talangan_id)
    if tl.dibatalkan:
        raise _bad("Sudah dibatalkan", status.HTTP_409_CONFLICT)
    if (await _keluaran(session, tl, akun)).terbayar > 0:
        raise _bad("Talangan sudah (sebagian) dilunasi; batalkan pelunasannya dulu", status.HTTP_409_CONFLICT)
    trx = [t for t in (await session.get(BlTransaksi, tl.transaksi_talangan_id), await session.get(BlTransaksi, tl.transaksi_id) if tl.transaksi_id else None) if t]
    if any(t.status_kirim == STATUS_TERKIRIM and t.kiriman_id for t in trx):
        raise _bad("Pengeluaran ini sudah dikirim ke laporan keuangan; batalkan kirimannya dulu", status.HTTP_409_CONFLICT)
    await pastikan_bulan_terbuka(session, tl.tanggal)
    kini = datetime.now(timezone.utc)
    for row in (*trx, tl):
        if not row.dibatalkan:
            row.dibatalkan, row.dibatalkan_at, row.alasan_batal = True, kini, alasan.strip()
    await catat_audit(session, user.id, "batal", "talangan", tl.id, sebelum={"nama": tl.nama, "jumlah": tl.jumlah}, alasan=alasan.strip())
    await session.flush()
    return await _keluaran(session, tl, akun)
