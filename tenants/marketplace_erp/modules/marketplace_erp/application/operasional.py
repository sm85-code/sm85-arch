"""Operating-cost entries typed into the Operasional Umum and Operasional Packing pages.

Each entry has a date, a category, a description and an amount. The categories are the expense categories of the
Kategori Transaksi page (kategori.py) that are offered on this page, so the totals line up with Arus Kas:
  umum    -> by default bahan_cat, ongkir, langganan, atk, lain_keluar  (gaji and iklan are typed in Arus Kas)
  packing -> by default perl_packing, atk_eceran  (ATK bought piecemeal to cover what the general ATK shopping fell short of)
Which page offers a category, its name and its order are managed there.
Who may do what is decided per page (jenis) by the role settings (akses.py): halaman operasional_umum and
operasional_packing, level baca or tulis.
"""
from __future__ import annotations

import json
import re

from calendar import monthrange
from datetime import date
from decimal import Decimal

from fastapi import HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import case, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import akses
from tenants.marketplace_erp.modules.marketplace_erp.application import kategori as kat
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import KategoriTransaksi, PengaturanUi, ProcessingCatalog, ProcessingParty, TransaksiOperasional

# "supplier" is Belanja Supplier: purchases of goods from suppliers, typed by hand for now. Every row here is a manual
# entry. When the supplier order mapping (Pemetaan Order, tab Supplier) is linked later, its amounts should be computed
# from that data and added beside these rows (e.g. in untuk_arus_kas), not written into this table, so manual and
# automatic stay apart and the choice between them can be made per period.
HALAMAN = {"umum": "operasional_umum", "packing": "operasional_packing", "supplier": "belanja_supplier", "tukang": "belanja_tukang"}
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
    if jenis not in HALAMAN:
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


async def pastikan_kategori(session: AsyncSession, jenis: str, kategori: str) -> None:
    """New and changed entries may only use a category this page offers (and that is not hidden)."""
    await kat.pastikan_bawaan(session)
    k = await session.get(KategoriTransaksi, kategori)
    if k is None or k.arah != "keluar" or k.operasional != jenis or not k.aktif:
        raise HTTPException(422, "Kategori tidak dikenal untuk halaman ini")


def rentang(periode: str) -> tuple[date, date]:
    """First and last day of a YYYY-MM month."""
    try:
        tahun, bulan = int(periode[:4]), int(periode[5:7])
        return date(tahun, bulan, 1), date(tahun, bulan, monthrange(tahun, bulan)[1])
    except (ValueError, IndexError):
        raise HTTPException(422, "Periode harus berbentuk YYYY-MM") from None


async def untuk_arus_kas(session: AsyncSession, periode: str) -> dict:
    """What Arus Kas takes from both pages: the amount per date and category for a month. Gaji and iklan are left out,
    an admin types them in Arus Kas itself."""
    awal, akhir = rentang(periode)
    # Belanja Supplier entries keep their description (the supplier's name) so Arus Kas can put each supplier in its own row.
    uraian = case((TransaksiOperasional.jenis.in_(tuple(PIHAK)), TransaksiOperasional.uraian), else_=literal(""))
    jenis = case((TransaksiOperasional.jenis.in_(tuple(PIHAK)), TransaksiOperasional.jenis), else_=literal(""))
    rows = (
        await session.execute(
            select(TransaksiOperasional.tanggal, TransaksiOperasional.kategori, uraian, func.sum(TransaksiOperasional.jumlah), jenis)
            .where(TransaksiOperasional.tanggal.between(awal, akhir), TransaksiOperasional.kategori.not_in(kat.ARUS_KAS_SAJA))
            .group_by(TransaksiOperasional.tanggal, TransaksiOperasional.kategori, uraian, jenis)
            .order_by(TransaksiOperasional.tanggal, TransaksiOperasional.kategori, uraian)
        )
    ).all()
    # A supplier / tukang picked on its page goes to the Arus Kas row linked to it (when one was set), else its own name.
    tujuan: dict[str, dict[str, str]] = {}
    for j in {r[4] for r in rows if r[4]}:
        # Several linked rows ("A; B") = one row per linked row on the page; an entry under the party's own name goes to the first.
        tujuan[j] = {_kunci(p["nama"]): p["baris_arus_kas"].split(";")[0].strip() for p in await pihak(session, j) if p["baris_arus_kas"].split(";")[0].strip()}
    return {
        "periode": periode,
        "items": [
            {"tanggal": t.isoformat(), "kategori": k, "jumlah": str(j), **({"uraian": tujuan.get(jn, {}).get(_kunci(u), u)} if u else {})}
            for t, k, u, j, jn in rows
        ],
    }


# Belanja Supplier / Belanja Tukang: each entry is for one party of the Supplier / Tukang Kayu list (Data Produksi);
# its name is kept in uraian. Each party may be linked to an Arus Kas row by that row's name.
PIHAK = {"supplier": "supplier", "tukang": "tukang"}
_PETA = "peta_arus_kas:"


def _kunci(nama: str) -> str:
    return re.sub(r"\s+", " ", nama or "").strip().lower()


async def _peta(session: AsyncSession, jenis: str) -> dict[str, str]:
    r = await session.get(PengaturanUi, _PETA + jenis)
    try:
        return dict(json.loads(r.teks)) if r else {}
    except (ValueError, TypeError):
        return {}


async def pihak(session: AsyncSession, jenis: str) -> list[dict]:
    """The parties of the matching Data Produksi list (id, name) with the Arus Kas row each is linked to ("" = by name)."""
    kind = PIHAK.get(jenis)
    if not kind:
        return []
    asal = await session.get(ProcessingCatalog, "global")
    try:
        katalog = json.loads(asal.payload_json) if asal else {}
    except ValueError:
        katalog = {}
    daftar_ = {str(p.get("id")): str(p.get("nama") or "") for p in katalog.get(kind, []) if isinstance(p, dict)}
    for row in (await session.scalars(select(ProcessingParty).where(ProcessingParty.kind == kind))).all():
        try:
            daftar_[row.source_id] = str(json.loads(row.payload_json).get("nama") or "")
        except ValueError:
            continue
    peta = await _peta(session, jenis)
    return [{"id": i, "nama": n, "baris_arus_kas": peta.get(i, "")} for i, n in daftar_.items() if n]


class PetaIsian(BaseModel):
    baris_arus_kas: str = Field(default="", max_length=200)


async def simpan_peta(session: AsyncSession, jenis: str, pihak_id: str, isian: PetaIsian) -> dict:
    if jenis not in PIHAK:
        raise HTTPException(404, "Halaman ini tidak memakai daftar supplier/tukang")
    if pihak_id not in {p["id"] for p in await pihak(session, jenis)}:
        raise HTTPException(404, "Tidak ada di daftar")
    peta = await _peta(session, jenis)
    nama = isian.baris_arus_kas.strip()
    if nama:
        peta[pihak_id] = nama
    else:
        peta.pop(pihak_id, None)
    r = await session.get(PengaturanUi, _PETA + jenis)
    if r is None:
        session.add(PengaturanUi(id=_PETA + jenis, teks=json.dumps(peta)))
    else:
        r.teks = json.dumps(peta)
    await session.commit()
    return {"id": pihak_id, "baris_arus_kas": nama}


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
    await kat.pastikan_bawaan(session)
    awal, akhir = rentang(periode)
    rows = (
        await session.scalars(
            select(TransaksiOperasional)
            .where(TransaksiOperasional.jenis == jenis, TransaksiOperasional.tanggal.between(awal, akhir))
            .order_by(TransaksiOperasional.tanggal, TransaksiOperasional.created_at)
        )
    ).all()
    # The categories this page offers, plus any that still hold entries here (hidden, or moved to the other page).
    dipakai = {r.kategori for r in rows}
    semua = (await session.scalars(select(KategoriTransaksi).where(KategoriTransaksi.arah == "keluar").order_by(KategoriTransaksi.urut, KategoriTransaksi.id))).all()
    tampil = [k for k in semua if (k.operasional == jenis and k.aktif) or k.id in dipakai]
    per_kategori = {k.id: Decimal("0") for k in tampil}
    for r in rows:
        per_kategori[r.kategori] = per_kategori.get(r.kategori, Decimal("0")) + r.jumlah
    return {
        "jenis": jenis,
        "periode": periode,
        "kategori": [{"id": k.id, "label": k.nama, "aktif": k.aktif and k.operasional == jenis} for k in tampil],
        "items": [_baris(r) for r in rows],
        "ringkasan": {"per_kategori": {k: str(v) for k, v in per_kategori.items()}, "total": str(sum(per_kategori.values(), Decimal("0")))},
        "bisa_tulis": bisa_tulis,
        **({"pihak": await pihak(session, jenis)} if jenis in PIHAK else {}),
    }


async def tambah(session: AsyncSession, jenis: str, isian: Isian, user_id: str | None) -> dict:
    await pastikan_kategori(session, jenis, isian.kategori)
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
    t = await _ambil(session, jenis, id_)
    # An entry of a category that was hidden or moved can still be edited, but only to another category the page offers.
    if isian.kategori != t.kategori:
        await pastikan_kategori(session, jenis, isian.kategori)
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
