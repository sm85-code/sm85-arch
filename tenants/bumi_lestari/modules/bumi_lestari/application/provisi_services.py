"""Gaji dicicil 4 minggu (tiap Selasa) ke Dana cadangan, dibayar awal bulan berikutnya.

Di laporan keuangan beban gaji diakui mingguan (BlProvisi, 1/4 per Selasa); uangnya disisihkan lewat transfer
kas utama -> Dana cadangan. Pembayaran gaji di awal bulan keluar dari Dana cadangan dan hanya menyesuaikan selisih.
Langganan (listrik, air, wifi, ...) TIDAK dicicil: dibayar langsung saat tagihan datang, bebannya diakui saat itu.
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_DOWN, Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application.provisi_core import (
    batalkan_provisi_sumber,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_t3 import (
    LanggananIn,
    LanggananPatch,
    SisihanItemOut,
    SisihanOut,
    TagihanBayarIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.services import (
    _akun_by_kode,
    _batalkan,
    _buat_transfer,
    _hari_ini,
    _pastikan_saldo_cukup,
    batalkan_transfer,
    saldo_akun,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.t3_services import (
    KATEGORI_TAGIHAN,
    _akun_bayar,
    _batalkan_transaksi_ref,
    _transaksi_otomatis,
    list_karyawan,
    selasa_acuan,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import (
    KODE_DANA_CADANGAN,
    KODE_KAS_UTAMA,
    BlUser,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_t3 import (
    REF_SISIHAN,
    REF_TAGIHAN,
    BlLangganan,
    BlProvisi,
    BlSisihan,
    BlTagihan,
)

JUMLAH_CICILAN = 4


def _bad(detail: str, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


def periode_dan_minggu(selasa: date) -> tuple[str, int]:
    """Bulan yang didanai dan Selasa ke-N bulan itu (Selasa tanggal 1-7 = minggu ke-1, dst.)."""
    return f"{selasa.year}-{selasa.month:02d}", (selasa.day - 1) // 7 + 1


def cicilan(bulanan: Decimal, minggu_ke: int) -> Decimal:
    """1/4 per minggu; cicilan ke-4 menampung sisa pembulatan supaya totalnya tepat sebulan."""
    per = (Decimal(bulanan) / JUMLAH_CICILAN).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return per if minggu_ke < JUMLAH_CICILAN else Decimal(bulanan) - per * (JUMLAH_CICILAN - 1)


# --- Langganan -------------------------------------------------------------------------------


async def list_langganan(session: AsyncSession) -> list[BlLangganan]:
    stmt = select(BlLangganan).where(BlLangganan.aktif.is_(True)).order_by(BlLangganan.nama)
    return list((await session.execute(stmt)).scalars())


async def create_langganan(session: AsyncSession, payload: LanggananIn) -> BlLangganan:
    nama = payload.nama.strip()
    if (await session.execute(select(BlLangganan.id).where(BlLangganan.nama == nama))).first():
        raise _bad("Nama langganan sudah dipakai", status.HTTP_409_CONFLICT)
    row = BlLangganan(nama=nama, jumlah_bulanan=payload.jumlah_bulanan)
    session.add(row)
    await session.flush()
    return row


async def update_langganan(session: AsyncSession, langganan_id: str, payload: LanggananPatch) -> BlLangganan:
    row = await session.get(BlLangganan, langganan_id)
    if row is None:
        raise _bad("Langganan tidak ditemukan", status.HTTP_404_NOT_FOUND)
    for kolom, nilai in payload.model_dump(exclude_unset=True).items():
        if nilai is not None:
            setattr(row, kolom, nilai.strip() if isinstance(nilai, str) else nilai)
    await session.flush()
    return row


# --- Sisihan mingguan -----------------------------------------------------------------------


async def _sisihan_aktif(session: AsyncSession, periode: str, minggu_ke: int) -> BlSisihan | None:
    stmt = select(BlSisihan).where(
        BlSisihan.periode == periode, BlSisihan.minggu_ke == minggu_ke, BlSisihan.dibatalkan.is_(False)
    )
    return (await session.execute(stmt)).scalars().first()


async def hitung_sisihan(session: AsyncSession, tanggal: date | None = None) -> SisihanOut:
    selasa = selasa_acuan(tanggal or _hari_ini())
    periode, minggu_ke = periode_dan_minggu(selasa)
    items: list[SisihanItemOut] = []
    catatan = ""
    if minggu_ke > JUMLAH_CICILAN:
        catatan = "Selasa ke-5 bulan ini: cicilan sudah 4 kali, tidak ada yang disisihkan."
    else:
        for k in await list_karyawan(session):
            if Decimal(k.gaji_bulanan) > 0:
                items.append(SisihanItemOut(jenis="gaji", nama=k.nama, jumlah=cicilan(k.gaji_bulanan, minggu_ke)))
    total = sum((i.jumlah for i in items), Decimal("0"))
    saldo_utama = await saldo_akun(session, await _akun_by_kode(session, KODE_KAS_UTAMA))
    existing = await _sisihan_aktif(session, periode, minggu_ke)
    return SisihanOut(
        selasa=selasa, periode=periode, minggu_ke=minggu_ke, items=items, total=total, saldo_kas_utama=saldo_utama,
        cukup=saldo_utama >= total, sudah_dicatat_id=existing.id if existing else None, catatan=catatan,
    )


async def catat_sisihan(session: AsyncSession, user: BlUser, tanggal: date | None = None) -> BlSisihan:
    info = await hitung_sisihan(session, tanggal)
    if info.sudah_dicatat_id:
        raise _bad("Sisihan Selasa ini sudah dicatat (batalkan dulu bila ingin mengulang)", status.HTTP_409_CONFLICT)
    if not info.items:
        raise _bad(info.catatan or "Tidak ada gaji yang perlu disisihkan")
    kas_utama = await _akun_by_kode(session, KODE_KAS_UTAMA)
    dana = await _akun_by_kode(session, KODE_DANA_CADANGAN)
    transfer = await _buat_transfer(
        session, user, tanggal=info.selasa, dari=kas_utama, ke=dana, jumlah=info.total, jenis="sisihan_dana",
        keterangan=f"Sisihan gaji {info.periode} minggu ke-{info.minggu_ke}",
    )
    row = BlSisihan(
        selasa=info.selasa, periode=info.periode, minggu_ke=info.minggu_ke, total=info.total,
        transfer_id=transfer.id, dibuat_oleh=user.id,
    )
    session.add(row)
    await session.flush()
    for k in await list_karyawan(session):
        if Decimal(k.gaji_bulanan) > 0:
            session.add(
                BlProvisi(
                    tanggal=info.selasa, periode=info.periode, minggu_ke=info.minggu_ke, jenis="gaji",
                    karyawan_id=k.id, jumlah=cicilan(k.gaji_bulanan, info.minggu_ke), sumber_jenis=REF_SISIHAN,
                    sumber_id=row.id,
                )
            )
    await session.flush()
    return row


async def list_sisihan(session: AsyncSession) -> list[BlSisihan]:
    return list((await session.execute(select(BlSisihan).order_by(BlSisihan.selasa.desc()))).scalars())


async def batalkan_sisihan(session: AsyncSession, sisihan_id: str, alasan: str) -> BlSisihan:
    row = await session.get(BlSisihan, sisihan_id)
    if row is None:
        raise _bad("Sisihan tidak ditemukan", status.HTTP_404_NOT_FOUND)
    _batalkan(row, alasan)
    await batalkan_transfer(session, row.transfer_id, alasan)
    await batalkan_provisi_sumber(session, REF_SISIHAN, row.id, alasan)
    await session.flush()
    return row


# --- Tagihan langganan (dibayar awal bulan berikutnya dari Dana cadangan) --------------------------


async def bayar_tagihan(session: AsyncSession, user: BlUser, payload: TagihanBayarIn) -> list[BlTagihan]:
    """Bayar langganan langsung (default dari kas utama); beban diakui saat dibayar."""
    langganan = {lg.id: lg for lg in await list_langganan(session)}
    if payload.items is None:
        daftar = [(lg.id, Decimal(lg.jumlah_bulanan)) for lg in langganan.values() if Decimal(lg.jumlah_bulanan) > 0]
    else:
        daftar = [(i.langganan_id, i.jumlah) for i in payload.items]
    if not daftar:
        raise _bad("Tidak ada tagihan langganan yang dibayar")
    sudah = set(
        (
            await session.execute(
                select(BlTagihan.langganan_id).where(BlTagihan.periode == payload.periode, BlTagihan.dibatalkan.is_(False))
            )
        ).scalars()
    )
    for lid, _ in daftar:
        if lid not in langganan:
            raise _bad("Langganan tidak ditemukan", status.HTTP_404_NOT_FOUND)
        if lid in sudah:
            raise _bad(f"Tagihan {langganan[lid].nama} periode {payload.periode} sudah dibayar", status.HTTP_409_CONFLICT)
    total = sum((jumlah for _, jumlah in daftar), Decimal("0"))
    akun = await _akun_bayar(session, payload.akun_id)
    await _pastikan_saldo_cukup(session, akun, total)
    tanggal = payload.tanggal or _hari_ini()
    hasil = []
    for lid, jumlah in daftar:
        row = BlTagihan(periode=payload.periode, langganan_id=lid, jumlah=jumlah, tanggal_bayar=tanggal, dibayar_oleh=user.id)
        session.add(row)
        await session.flush()
        if jumlah > 0:
            await _transaksi_otomatis(
                session, user, tanggal=tanggal, akun=akun, kategori=KATEGORI_TAGIHAN, jenis="keluar", jumlah=jumlah,
                keterangan=f"{langganan[lid].nama} {payload.periode}", ref_jenis=REF_TAGIHAN, ref_id=row.id,
            )
        hasil.append(row)
    await session.flush()
    return hasil


async def list_tagihan(session: AsyncSession, periode: str) -> list[BlTagihan]:
    stmt = select(BlTagihan).where(BlTagihan.periode == periode).order_by(BlTagihan.created_at)
    return list((await session.execute(stmt)).scalars())


async def batalkan_tagihan(session: AsyncSession, tagihan_id: str, alasan: str) -> BlTagihan:
    row = await session.get(BlTagihan, tagihan_id)
    if row is None:
        raise _bad("Tagihan tidak ditemukan", status.HTTP_404_NOT_FOUND)
    _batalkan(row, alasan)
    await _batalkan_transaksi_ref(session, REF_TAGIHAN, row.id, alasan)
    await session.flush()
    return row
