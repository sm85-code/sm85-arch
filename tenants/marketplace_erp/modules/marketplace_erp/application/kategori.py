"""Categories of money coming in and going out, managed on the Kategori Transaksi page.

They used to be fixed in the code. The ids stay (Arus Kas sheets and Operasional entries point at them) while the names,
the order and the placement can be changed. A category is never deleted, only hidden from new choices, so nothing that
points at it is ever orphaned.
"""
from __future__ import annotations

import re
import unicodedata

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import KategoriTransaksi, TransaksiOperasional

ARAH = ("masuk", "keluar")
HPP = "Harga pokok penjualan"
BEBAN = "Beban operasional"
PEMASUKAN = "Pemasukan"
# Where a category can land in the Pemasukan / Pengeluaran tabs.
KELOMPOK = {"masuk": [PEMASUKAN], "keluar": [HPP, BEBAN]}
OPERASIONAL = ("umum", "packing")
# Typed into Arus Kas by an admin, never as entries of the Operasional pages.
ARUS_KAS_SAJA = ("gaji", "iklan")

# (id, arah, nama, kelompok, operasional, sistem, auto_sembunyi): what every installation starts with, in this order.
BAWAAN: list[tuple[str, str, str, str, str | None, bool, bool]] = [
    ("saldo_toko", "masuk", "Saldo toko", PEMASUKAN, None, True, False),
    ("produk", "masuk", "Penjualan produk", PEMASUKAN, None, True, False),
    ("cat", "masuk", "Jasa cat", PEMASUKAN, None, True, False),
    ("packing", "masuk", "Jasa packing", PEMASUKAN, None, True, False),
    ("proses", "masuk", "Jasa proses pesanan", PEMASUKAN, None, True, False),
    ("produk_proses", "masuk", "Penjualan produk + proses", PEMASUKAN, None, True, True),
    ("cat_packing", "masuk", "Jasa cat + packing", PEMASUKAN, None, True, True),
    ("lain_masuk", "masuk", "Pemasukan lainnya", PEMASUKAN, None, True, True),
    ("bukan_masuk", "masuk", "Bukan pemasukan", "", None, True, True),
    ("tukang", "keluar", "Tukang", HPP, None, True, False),
    ("non_kayu", "keluar", "Produk non kayu", HPP, None, True, False),
    ("bahan_cat", "keluar", "Bahan dan perlengkapan cat", HPP, "umum", True, False),
    ("perl_packing", "keluar", "Bahan dan perlengkapan packing", HPP, "packing", True, False),
    ("ongkir", "keluar", "Ongkir manual", HPP, "umum", True, False),
    ("gaji", "keluar", "Gaji", BEBAN, None, True, False),
    ("iklan", "keluar", "Iklan", BEBAN, None, True, False),
    ("langganan", "keluar", "Langganan", BEBAN, "umum", True, False),
    ("atk", "keluar", "Belanja ATK", BEBAN, "umum", True, True),
    ("atk_eceran", "keluar", "Belanja ATK eceran", BEBAN, "packing", True, True),
    ("lain_keluar", "keluar", "Operasional lainnya", BEBAN, "umum", True, True),
    ("bukan_keluar", "keluar", "Bukan pengeluaran", "", None, True, True),
]

async def pastikan_bawaan(session: AsyncSession) -> None:
    """Fill the table on a fresh installation. After that nothing is added or restored on its own."""
    if (await session.scalar(select(func.count()).select_from(KategoriTransaksi))) or 0:
        # Installations seeded before gaji and iklan moved to Arus Kas still offer them on Operasional Umum.
        lama = (await session.scalars(select(KategoriTransaksi).where(KategoriTransaksi.id.in_(ARUS_KAS_SAJA), KategoriTransaksi.operasional.is_not(None)))).all()
        for k in lama:
            k.operasional = None
        if lama:
            await session.commit()
        return
    for urut, (id_, arah, nama, kelompok, operasional, sistem, auto) in enumerate(BAWAAN, start=1):
        session.add(KategoriTransaksi(id=id_, arah=arah, nama=nama, kelompok=kelompok, operasional=operasional, urut=urut, aktif=True, sistem=sistem, auto_sembunyi=auto))
    try:
        await session.commit()
    except IntegrityError:  # another request filled it first
        await session.rollback()


def _baris(k: KategoriTransaksi, dipakai: int = 0) -> dict:
    return {
        "id": k.id, "arah": k.arah, "nama": k.nama, "kelompok": k.kelompok, "operasional": k.operasional, "urut": k.urut,
        "aktif": k.aktif, "sistem": k.sistem, "auto_sembunyi": k.auto_sembunyi, "dipakai": dipakai,
    }


async def daftar(session: AsyncSession) -> dict:
    await pastikan_bawaan(session)
    rows = (await session.scalars(select(KategoriTransaksi).order_by(KategoriTransaksi.arah, KategoriTransaksi.urut, KategoriTransaksi.id))).all()
    pakai = dict((await session.execute(select(TransaksiOperasional.kategori, func.count()).group_by(TransaksiOperasional.kategori))).all())
    return {"items": [_baris(k, pakai.get(k.id, 0)) for k in rows], "kelompok": KELOMPOK, "operasional": list(OPERASIONAL)}


def _slug(nama: str) -> str:
    teks = unicodedata.normalize("NFKD", nama).encode("ascii", "ignore").decode().lower()
    teks = re.sub(r"[^a-z0-9]+", "_", teks).strip("_")
    return (teks if teks[:1].isalpha() else f"kat_{teks}")[:44].rstrip("_")


def _periksa(arah: str, kelompok: str, operasional: str | None, sistem: bool = False) -> tuple[str, str | None]:
    if arah not in ARAH:
        raise HTTPException(422, "Jenis harus masuk atau keluar")
    if sistem:
        return "", None  # "not income / not an expense" lands nowhere, whatever is sent
    if kelompok not in KELOMPOK[arah]:
        raise HTTPException(422, f"Kelompok untuk {'pemasukan' if arah == 'masuk' else 'pengeluaran'}: {', '.join(KELOMPOK[arah])}")
    if operasional not in (None, *OPERASIONAL):
        raise HTTPException(422, "Halaman operasional harus umum atau packing")
    if operasional and arah != "keluar":
        raise HTTPException(422, "Hanya pengeluaran yang bisa dipakai di halaman operasional")
    return kelompok, operasional


async def _nama_bebas(session: AsyncSession, arah: str, nama: str, kecuali: str | None = None) -> None:
    q = select(KategoriTransaksi.id).where(KategoriTransaksi.arah == arah, func.lower(KategoriTransaksi.nama) == nama.lower())
    if kecuali:
        q = q.where(KategoriTransaksi.id != kecuali)
    if await session.scalar(q):
        raise HTTPException(409, "Nama kategori sudah dipakai")


def _nama(nama: str) -> str:
    nama = re.sub(r"\s+", " ", (nama or "").strip())
    if not 2 <= len(nama) <= 80:
        raise HTTPException(422, "Nama kategori 2-80 karakter")
    return nama


async def buat(session: AsyncSession, arah: str, nama: str, kelompok: str, operasional: str | None) -> dict:
    await pastikan_bawaan(session)
    nama = _nama(nama)
    kelompok, operasional = _periksa(arah, kelompok, operasional)
    await _nama_bebas(session, arah, nama)
    dasar = _slug(nama) or "kategori"
    id_ = dasar
    n = 2
    while await session.get(KategoriTransaksi, id_):
        id_ = f"{dasar}_{n}"[:48]
        n += 1
    urut = ((await session.scalar(select(func.max(KategoriTransaksi.urut)).where(KategoriTransaksi.arah == arah))) or 0) + 1
    k = KategoriTransaksi(id=id_, arah=arah, nama=nama, kelompok=kelompok, operasional=operasional, urut=urut, aktif=True, sistem=False, auto_sembunyi=True)
    session.add(k)
    await session.commit()
    return _baris(k)


async def ubah(session: AsyncSession, id_: str, nama: str, kelompok: str, operasional: str | None, aktif: bool) -> dict:
    await pastikan_bawaan(session)
    k = await session.get(KategoriTransaksi, id_)
    if k is None:
        raise HTTPException(404, "Kategori tidak ditemukan")
    nama = _nama(nama)
    await _nama_bebas(session, k.arah, nama, kecuali=k.id)
    kelompok, operasional = _periksa(k.arah, kelompok, None if id_ in ARUS_KAS_SAJA else operasional, sistem=k.sistem and k.kelompok == "")
    if k.kelompok == "" and k.sistem and not aktif:
        raise HTTPException(409, "Kategori ini tidak bisa disembunyikan")
    k.nama, k.kelompok, k.operasional, k.aktif = nama, kelompok, operasional, aktif
    await session.commit()
    return _baris(k)


async def urutkan(session: AsyncSession, arah: str, ids: list[str]) -> None:
    """Put the categories of one kind in this order (ids left out keep their relative order after the listed ones)."""
    await pastikan_bawaan(session)
    if arah not in ARAH:
        raise HTTPException(422, "Jenis harus masuk atau keluar")
    rows = {k.id: k for k in (await session.scalars(select(KategoriTransaksi).where(KategoriTransaksi.arah == arah).order_by(KategoriTransaksi.urut))).all()}
    if len(set(ids)) != len(ids) or any(i not in rows for i in ids):
        raise HTTPException(422, "Daftar urutan tidak cocok")
    selebihnya = [i for i in rows if i not in ids]
    for n, id_ in enumerate([*ids, *selebihnya], start=1):
        rows[id_].urut = n
    await session.commit()
