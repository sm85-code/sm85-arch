"""Operating-cost entries typed into the Operasional Umum and Operasional Packing pages.

Each entry has a date, a category, a description and an amount. The categories are the ones Arus Kas already knows
(kategoriKeuangan on the frontend), so the totals line up with its rows:
  umum    -> bahan_cat, ongkir, gaji, iklan, langganan, lain_keluar
  packing -> perl_packing
Who may do what is decided per page (jenis) by the role settings (akses.py): halaman operasional_umum and
operasional_packing, level baca or tulis.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date
from decimal import Decimal

from fastapi import HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import akses
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import TransaksiOperasional

KATEGORI: dict[str, dict[str, str]] = {
    "umum": {
        "bahan_cat": "Bahan dan perlengkapan cat",
        "ongkir": "Ongkir manual",
        "gaji": "Gaji",
        "iklan": "Iklan",
        "langganan": "Langganan",
        "lain_keluar": "Operasional lainnya",
    },
    "packing": {"perl_packing": "Bahan dan perlengkapan packing"},
}
HALAMAN = {"umum": "operasional_umum", "packing": "operasional_packing"}
MAKS_JUMLAH = Decimal("9999999999999.99")


class Isian(BaseModel):
    kategori: str
    tanggal: date
    uraian: str = Field(default="", max_length=255)
    jumlah: Decimal = Field(gt=0, le=MAKS_JUMLAH, decimal_places=2)

    @field_validator("tanggal")
    @classmethod
    def _tanggal_wajar(cls, v: date) -> date:
        if not 2000 <= v.year <= 2100:
            raise ValueError("Tanggal tidak wajar")
        return v

    @field_validator("uraian")
    @classmethod
    def _rapikan(cls, v: str) -> str:
        return v.strip()


def pastikan_jenis(jenis: str) -> None:
    if jenis not in KATEGORI:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Halaman tidak ditemukan")


async def pastikan_baca(session: AsyncSession, jenis: str, role: str | None) -> str:
    """Raises unless the role may open this page; returns its level ("baca" or "tulis")."""
    pastikan_jenis(jenis)
    tingkat = await akses.tingkat_halaman(session, role, HALAMAN[jenis])
    if tingkat is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Akses ditolak")
    return tingkat


async def pastikan_tulis(session: AsyncSession, jenis: str, role: str | None) -> None:
    if await pastikan_baca(session, jenis, role) != "tulis":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Akses ditolak")


def pastikan_kategori(jenis: str, kategori: str) -> None:
    if kategori not in KATEGORI[jenis]:
        raise HTTPException(422, "Kategori tidak dikenal untuk halaman ini")


def rentang(periode: str) -> tuple[date, date]:
    """First and last day of a YYYY-MM month."""
    try:
        tahun, bulan = int(periode[:4]), int(periode[5:7])
        return date(tahun, bulan, 1), date(tahun, bulan, monthrange(tahun, bulan)[1])
    except (ValueError, IndexError):
        raise HTTPException(422, "Periode harus berbentuk YYYY-MM") from None


def _baris(t: TransaksiOperasional) -> dict:
    return {
        "id": t.id,
        "kategori": t.kategori,
        "tanggal": t.tanggal.isoformat(),
        "uraian": t.uraian,
        "jumlah": str(t.jumlah),
        "dibuat_oleh": t.dibuat_oleh,
        "diubah_oleh": t.diubah_oleh,
    }


async def daftar(session: AsyncSession, jenis: str, periode: str, bisa_tulis: bool) -> dict:
    awal, akhir = rentang(periode)
    rows = (
        await session.scalars(
            select(TransaksiOperasional)
            .where(TransaksiOperasional.jenis == jenis, TransaksiOperasional.tanggal.between(awal, akhir))
            .order_by(TransaksiOperasional.tanggal, TransaksiOperasional.created_at)
        )
    ).all()
    per_kategori = {k: Decimal("0") for k in KATEGORI[jenis]}
    for r in rows:
        per_kategori[r.kategori] = per_kategori.get(r.kategori, Decimal("0")) + r.jumlah
    return {
        "jenis": jenis,
        "periode": periode,
        "kategori": [{"id": k, "label": v} for k, v in KATEGORI[jenis].items()],
        "items": [_baris(r) for r in rows],
        "ringkasan": {"per_kategori": {k: str(v) for k, v in per_kategori.items()}, "total": str(sum(per_kategori.values(), Decimal("0")))},
        "bisa_tulis": bisa_tulis,
    }


async def tambah(session: AsyncSession, jenis: str, isian: Isian, user_id: str | None) -> dict:
    pastikan_kategori(jenis, isian.kategori)
    t = TransaksiOperasional(jenis=jenis, kategori=isian.kategori, tanggal=isian.tanggal, uraian=isian.uraian, jumlah=isian.jumlah, dibuat_oleh=user_id, diubah_oleh=user_id)
    session.add(t)
    await session.commit()
    await session.refresh(t)
    return _baris(t)


async def _ambil(session: AsyncSession, jenis: str, id_: str) -> TransaksiOperasional:
    t = await session.get(TransaksiOperasional, id_)
    if t is None or t.jenis != jenis:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transaksi tidak ditemukan")
    return t


async def ubah(session: AsyncSession, jenis: str, id_: str, isian: Isian, user_id: str | None) -> dict:
    pastikan_kategori(jenis, isian.kategori)
    t = await _ambil(session, jenis, id_)
    t.kategori, t.tanggal, t.uraian, t.jumlah, t.diubah_oleh = isian.kategori, isian.tanggal, isian.uraian, isian.jumlah, user_id
    await session.commit()
    await session.refresh(t)
    return _baris(t)


async def hapus(session: AsyncSession, jenis: str, id_: str) -> None:
    t = await _ambil(session, jenis, id_)
    await session.delete(t)
    await session.commit()


async def total_per_kategori(session: AsyncSession, jenis: str, awal: date, akhir: date) -> dict[str, Decimal]:
    """Sum per category between two dates (inclusive); what Arus Kas will read in a later step."""
    rows = await session.execute(
        select(TransaksiOperasional.kategori, func.coalesce(func.sum(TransaksiOperasional.jumlah), 0))
        .where(TransaksiOperasional.jenis == jenis, TransaksiOperasional.tanggal.between(awal, akhir))
        .group_by(TransaksiOperasional.kategori)
    )
    return {k: Decimal(v) for k, v in rows.all()}
