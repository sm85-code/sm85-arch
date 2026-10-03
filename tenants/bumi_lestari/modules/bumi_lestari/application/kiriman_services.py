"""Posting berkelompok "Kirim ke laporan keuangan" (spesifikasi 8.14, Fase 1 item 1.13).

Sumber draf:
- kas_kecil / kas_iklan: transaksi manual di akun itu (BlTransaksi.status_kirim = draf);
- pembayaran_pemasok / penerimaan_reseller: baris sumber + transaksi otomatisnya, keduanya draf.

Kirim = satu BlKiriman per sumber; entri & transaksinya menjadi `terkirim` dengan tanggal entri masing-masing
(transaksi sudah dibuat dengan tanggal entri saat dicatat). Batal kiriman (wajib alasan, ditolak bila bulan
entrinya sudah tutup buku) mengembalikan entri dan transaksinya ke draf; kiriman tetap tersimpan sebagai
riwayat berstatus `dibatalkan`. Fungsi di sini dipanggil dari API dan bisa dipanggil dari job sinkronisasi.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application.audit_core import bulan_tertutup, catat_audit
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_kiriman import DrafSumberOut, EntriDrafOut
from tenants.bumi_lestari.modules.bumi_lestari.application.services import _hari_ini, _is_admin
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import (
    STATUS_DRAF,
    STATUS_KIRIMAN_DIBATALKAN,
    STATUS_TERKIRIM,
    SUMBER_KIRIMAN,
    BlAkunKas,
    BlKiriman,
    BlKirimanItem,
    BlTransaksi,
    BlUser,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_pembayaran import (
    REF_PEMBAYARAN_PEMASOK,
    REF_PENERIMAAN_RESELLER,
    BlPembayaranPemasok,
    BlPenerimaanReseller,
)

LABEL_SUMBER = {
    "kas_kecil": "Kas kecil",
    "kas_iklan": "Kas iklan",
    "penerimaan_reseller": "Penerimaan penjual lain",
    "pembayaran_pemasok": "Pembayaran tukang & supplier",
}
REF_TRANSAKSI = "transaksi"
_MODEL_SUMBER = {
    "pembayaran_pemasok": (BlPembayaranPemasok, REF_PEMBAYARAN_PEMASOK, "keluar"),
    "penerimaan_reseller": (BlPenerimaanReseller, REF_PENERIMAAN_RESELLER, "masuk"),
}


def _bad(detail: str, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


@dataclass
class _Entri:
    ref_jenis: str
    ref_id: str
    tanggal: date
    jenis: str
    jumlah: Decimal
    keterangan: str
    sumber_row: object | None = None  # BlPembayaranPemasok / BlPenerimaanReseller; None untuk transaksi manual
    transaksi: list[BlTransaksi] = field(default_factory=list)


def cek_sumber(sumber: str, user: BlUser) -> None:
    if sumber not in SUMBER_KIRIMAN:
        raise _bad(f"Sumber harus salah satu dari: {', '.join(SUMBER_KIRIMAN)}")
    if sumber == "kas_iklan" and not _is_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Kas iklan hanya bisa diurus admin")


def sumber_untuk(user: BlUser) -> tuple[str, ...]:
    return tuple(s for s in SUMBER_KIRIMAN if s != "kas_iklan" or _is_admin(user))


async def _entri_draf(session: AsyncSession, sumber: str, sampai: date | None = None) -> list[_Entri]:
    if sumber in ("kas_kecil", "kas_iklan"):
        stmt = (
            select(BlTransaksi)
            .join(BlAkunKas, BlAkunKas.id == BlTransaksi.akun_id)
            .where(
                BlAkunKas.jenis == sumber, BlTransaksi.status_kirim == STATUS_DRAF,
                BlTransaksi.dibatalkan.is_(False), BlTransaksi.ref_jenis.is_(None),
            )
            .order_by(BlTransaksi.tanggal, BlTransaksi.created_at)
        )
        if sampai:
            stmt = stmt.where(BlTransaksi.tanggal <= sampai)
        return [
            _Entri(REF_TRANSAKSI, t.id, t.tanggal, t.jenis, Decimal(t.jumlah), t.keterangan, None, [t])
            for t in (await session.execute(stmt)).scalars()
        ]
    model, ref_jenis, jenis = _MODEL_SUMBER[sumber]
    stmt = (
        select(model)
        .where(model.status_kirim == STATUS_DRAF, model.dibatalkan.is_(False))
        .order_by(model.tanggal, model.created_at)
    )
    if sampai:
        stmt = stmt.where(model.tanggal <= sampai)
    hasil = []
    for row in (await session.execute(stmt)).scalars():
        trx = list(
            (
                await session.execute(
                    select(BlTransaksi).where(
                        BlTransaksi.ref_jenis == ref_jenis, BlTransaksi.ref_id == row.id,
                        BlTransaksi.dibatalkan.is_(False),
                    )
                )
            ).scalars()
        )
        ket = trx[0].keterangan if trx else ""
        hasil.append(_Entri(ref_jenis, row.id, row.tanggal, jenis, Decimal(row.total), ket, row, trx))
    return hasil


def _ringkas(sumber: str, entri: list[_Entri], *, rinci: bool) -> DrafSumberOut:
    masuk = sum((e.jumlah for e in entri if e.jenis == "masuk"), Decimal("0"))
    keluar = sum((e.jumlah for e in entri if e.jenis == "keluar"), Decimal("0"))
    return DrafSumberOut(
        sumber=sumber, label=LABEL_SUMBER[sumber], jumlah_entri=len(entri), total_masuk=masuk,
        total_keluar=keluar, total=masuk + keluar, tanggal_tertua=min((e.tanggal for e in entri), default=None),
        entri=[
            EntriDrafOut(
                ref_jenis=e.ref_jenis, ref_id=e.ref_id, tanggal=e.tanggal, jenis=e.jenis, jumlah=e.jumlah,
                keterangan=e.keterangan,
            )
            for e in entri
        ] if rinci else [],
    )


async def ringkasan_draf(
    session: AsyncSession, user: BlUser, sumber: str | None = None, *, sampai: date | None = None, rinci: bool = True
) -> list[DrafSumberOut]:
    """Ringkasan draf per sumber (jumlah, total, tanggal tertua) -- untuk konfirmasi kirim & peringatan Beranda."""
    if sumber:
        cek_sumber(sumber, user)
    daftar = (sumber,) if sumber else sumber_untuk(user)
    return [_ringkas(s, await _entri_draf(session, s, sampai), rinci=rinci) for s in daftar]


async def _nomor_baru(session: AsyncSession) -> str:
    prefix = f"KRM-{_hari_ini():%Y%m%d}-"
    ada = (await session.execute(select(func.count(BlKiriman.id)).where(BlKiriman.nomor.like(f"{prefix}%")))).scalar_one()
    return f"{prefix}{int(ada) + 1:03d}"


async def kirim(
    session: AsyncSession, user: BlUser, sumber: str, sampai: date | None = None,
    tutup_kas_mingguan_id: str | None = None,
) -> BlKiriman:
    cek_sumber(sumber, user)
    entri = await _entri_draf(session, sumber, sampai)
    if not entri:
        raise _bad(f"Tidak ada draf {LABEL_SUMBER[sumber]} yang perlu dikirim")
    for e in entri:
        if await bulan_tertutup(session, e.tanggal):
            raise _bad(f"Ada draf bertanggal {e.tanggal:%d-%m-%Y} di bulan yang sudah tutup buku", 409)
    kiriman = BlKiriman(
        nomor=await _nomor_baru(session), sumber=sumber, sampai_tanggal=sampai, jumlah_entri=len(entri),
        total=sum((e.jumlah for e in entri), Decimal("0")), status=STATUS_TERKIRIM, dikirim_oleh=user.id,
        tutup_kas_mingguan_id=tutup_kas_mingguan_id,
    )
    session.add(kiriman)
    await session.flush()
    for e in entri:
        if e.sumber_row is not None:
            e.sumber_row.status_kirim = STATUS_TERKIRIM
            e.sumber_row.kiriman_id = kiriman.id
        for trx in e.transaksi:
            trx.status_kirim = STATUS_TERKIRIM
            trx.kiriman_id = kiriman.id
            session.add(
                BlKirimanItem(
                    kiriman_id=kiriman.id, ref_jenis=e.ref_jenis, ref_id=e.ref_id, transaksi_id=trx.id,
                    tanggal=trx.tanggal, jumlah=trx.jumlah,
                )
            )
    await catat_audit(
        session, user.id, "kirim", "kiriman", kiriman.id,
        sesudah={"nomor": kiriman.nomor, "sumber": sumber, "jumlah_entri": len(entri), "total": kiriman.total},
    )
    await session.flush()
    return kiriman


async def kirim_semua(
    session: AsyncSession, user: BlUser, sampai: date | None = None, tutup_kas_mingguan_id: str | None = None
) -> list[BlKiriman]:
    """Langkah akhir Tutup Kas Mingguan: satu kiriman untuk setiap sumber yang punya draf."""
    hasil = []
    for sumber in sumber_untuk(user):
        if await _entri_draf(session, sumber, sampai):
            hasil.append(await kirim(session, user, sumber, sampai, tutup_kas_mingguan_id))
    if not hasil:
        raise _bad("Tidak ada draf yang perlu dikirim")
    return hasil


async def _kiriman_or_404(session: AsyncSession, user: BlUser, kiriman_id: str) -> BlKiriman:
    kiriman = await session.get(BlKiriman, kiriman_id)
    if kiriman is None:
        raise _bad("Kiriman tidak ditemukan", status.HTTP_404_NOT_FOUND)
    cek_sumber(kiriman.sumber, user)
    return kiriman


async def batal_kiriman(session: AsyncSession, user: BlUser, kiriman_id: str, alasan: str) -> BlKiriman:
    kiriman = await _kiriman_or_404(session, user, kiriman_id)
    if kiriman.status != STATUS_TERKIRIM:
        raise _bad("Kiriman ini sudah dibatalkan", status.HTTP_409_CONFLICT)
    items = list((await session.execute(select(BlKirimanItem).where(BlKirimanItem.kiriman_id == kiriman.id))).scalars())
    for item in items:
        if await bulan_tertutup(session, item.tanggal):
            raise _bad(
                f"Ada entri bertanggal {item.tanggal:%d-%m-%Y} di bulan yang sudah tutup buku; "
                "catat sebagai koreksi bulan lalu di bulan berjalan",
                status.HTTP_409_CONFLICT,
            )
    for item in items:
        trx = await session.get(BlTransaksi, item.transaksi_id)
        if trx is not None and trx.kiriman_id == kiriman.id:
            trx.status_kirim = STATUS_DRAF
            trx.kiriman_id = None
        if item.ref_jenis in (REF_PEMBAYARAN_PEMASOK, REF_PENERIMAAN_RESELLER):
            model = BlPembayaranPemasok if item.ref_jenis == REF_PEMBAYARAN_PEMASOK else BlPenerimaanReseller
            row = await session.get(model, item.ref_id)
            if row is not None and row.kiriman_id == kiriman.id:
                row.status_kirim = STATUS_DRAF
                row.kiriman_id = None
    kiriman.status = STATUS_KIRIMAN_DIBATALKAN
    kiriman.dibatalkan_oleh = user.id
    kiriman.dibatalkan_pada = datetime.now(timezone.utc)
    kiriman.alasan_batal = alasan.strip()
    await catat_audit(
        session, user.id, "batal_kiriman", "kiriman", kiriman.id,
        sebelum={"nomor": kiriman.nomor, "status": STATUS_TERKIRIM, "jumlah_entri": kiriman.jumlah_entri},
        sesudah={"status": STATUS_KIRIMAN_DIBATALKAN}, alasan=alasan.strip(),
    )
    await session.flush()
    return kiriman


async def list_kiriman(
    session: AsyncSession, user: BlUser, sumber: str | None = None, limit: int = 100
) -> list[BlKiriman]:
    stmt = select(BlKiriman).order_by(BlKiriman.dikirim_pada.desc()).limit(limit)
    if sumber:
        cek_sumber(sumber, user)
        stmt = stmt.where(BlKiriman.sumber == sumber)
    else:
        stmt = stmt.where(BlKiriman.sumber.in_(sumber_untuk(user)))
    return list((await session.execute(stmt)).scalars())


async def detail_kiriman(session: AsyncSession, user: BlUser, kiriman_id: str) -> tuple[BlKiriman, list[BlKirimanItem]]:
    kiriman = await _kiriman_or_404(session, user, kiriman_id)
    items = (
        await session.execute(
            select(BlKirimanItem).where(BlKirimanItem.kiriman_id == kiriman.id).order_by(BlKirimanItem.tanggal)
        )
    ).scalars()
    return kiriman, list(items)
