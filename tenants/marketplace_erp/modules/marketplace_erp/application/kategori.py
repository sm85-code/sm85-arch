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

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import KategoriTransaksi, KelompokTransaksi, TransaksiOperasional

ARAH = ("masuk", "keluar")
# The groups every installation starts with: (id, arah, nama). Categories point at the id; the name can be changed on the
# Kategori Transaksi page and groups can be added there.
HPP = "hpp"
BEBAN = "beban"
PEMASUKAN = "pemasukan"
KELOMPOK_BAWAAN = [(PEMASUKAN, "masuk", "Pemasukan"), (HPP, "keluar", "Harga pokok penjualan"), (BEBAN, "keluar", "Beban operasional")]
# Categories saved before groups had ids hold the name text.
_NAMA_LAMA = {nama: id_ for id_, _, nama in KELOMPOK_BAWAAN}
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

async def _pastikan_kelompok(session: AsyncSession) -> bool:
    """The three starting groups exist, and categories that still hold a group's name text point at its id. Names that an
    admin changed are never overwritten. True when something was written."""
    ada = set((await session.scalars(select(KelompokTransaksi.id))).all())
    ubah = False
    for urut, (id_, arah, nama) in enumerate(KELOMPOK_BAWAAN, start=1):
        if id_ not in ada:
            session.add(KelompokTransaksi(id=id_, arah=arah, nama=nama, urut=urut))
            ubah = True
    for k in (await session.scalars(select(KategoriTransaksi).where(KategoriTransaksi.kelompok.in_(list(_NAMA_LAMA))))).all():
        k.kelompok = _NAMA_LAMA[k.kelompok]
        ubah = True
    return ubah


async def pastikan_bawaan(session: AsyncSession) -> None:
    """Fill the tables on a fresh installation. After that nothing is added or restored on its own."""
    kelompok_ditulis = await _pastikan_kelompok(session)
    if (await session.scalar(select(func.count()).select_from(KategoriTransaksi))) or 0:
        # Installations seeded before gaji and iklan moved to Arus Kas still offer them on Operasional Umum.
        lama = (await session.scalars(select(KategoriTransaksi).where(KategoriTransaksi.id.in_(ARUS_KAS_SAJA), KategoriTransaksi.operasional.is_not(None)))).all()
        for k in lama:
            k.operasional = None
        if lama or kelompok_ditulis:
            await session.commit()
        return
    for urut, (id_, arah, nama, kelompok, operasional, sistem, auto) in enumerate(BAWAAN, start=1):
        session.add(KategoriTransaksi(id=id_, arah=arah, nama=nama, kelompok=kelompok, operasional=operasional, urut=urut, aktif=True, sistem=sistem, auto_sembunyi=auto))
    try:
        await session.commit()
    except IntegrityError:  # another request filled it first
        await session.rollback()


def _baris(k: KategoriTransaksi, nama_kelompok: dict[str, str], dipakai: int = 0) -> dict:
    # `kelompok` is the group's current name, `kelompok_id` its id.
    return {
        "id": k.id, "arah": k.arah, "nama": k.nama, "kelompok": nama_kelompok.get(k.kelompok, k.kelompok), "kelompok_id": k.kelompok,
        "operasional": k.operasional, "urut": k.urut, "aktif": k.aktif, "sistem": k.sistem, "auto_sembunyi": k.auto_sembunyi, "dipakai": dipakai,
    }


async def _nama_kelompok(session: AsyncSession) -> dict[str, str]:
    return {g.id: g.nama for g in (await session.scalars(select(KelompokTransaksi))).all()}


async def daftar(session: AsyncSession) -> dict:
    await pastikan_bawaan(session)
    rows = (await session.scalars(select(KategoriTransaksi).order_by(KategoriTransaksi.arah, KategoriTransaksi.urut, KategoriTransaksi.id))).all()
    pakai = dict((await session.execute(select(TransaksiOperasional.kategori, func.count()).group_by(TransaksiOperasional.kategori))).all())
    grup = (await session.scalars(select(KelompokTransaksi).order_by(KelompokTransaksi.arah, KelompokTransaksi.urut, KelompokTransaksi.id))).all()
    nama = {g.id: g.nama for g in grup}
    por_arah = {a: [{"id": g.id, "nama": g.nama} for g in grup if g.arah == a] for a in ARAH}
    return {
        "items": [_baris(k, nama, pakai.get(k.id, 0)) for k in rows],
        "kelompok": {a: [g["nama"] for g in daftar_] for a, daftar_ in por_arah.items()},
        "kelompok_daftar": por_arah,
        "operasional": list(OPERASIONAL),
    }


def _slug(nama: str) -> str:
    teks = unicodedata.normalize("NFKD", nama).encode("ascii", "ignore").decode().lower()
    teks = re.sub(r"[^a-z0-9]+", "_", teks).strip("_")
    return (teks if teks[:1].isalpha() else f"kat_{teks}")[:44].rstrip("_")


async def _kelompok_id(session: AsyncSession, arah: str, kelompok: str) -> str:
    """The id of a group of this kind, given its id or (as older callers send it) its name."""
    grup = (await session.scalars(select(KelompokTransaksi).where(KelompokTransaksi.arah == arah))).all()
    for g in grup:
        if g.id == kelompok:
            return g.id
    for g in grup:
        if g.nama.lower() == (kelompok or "").strip().lower():
            return g.id
    raise HTTPException(422, f"Kelompok untuk {'pemasukan' if arah == 'masuk' else 'pengeluaran'}: {', '.join(g.nama for g in grup)}")


async def _periksa(session: AsyncSession, arah: str, kelompok: str, operasional: str | None, sistem: bool = False) -> tuple[str, str | None]:
    if arah not in ARAH:
        raise HTTPException(422, "Jenis harus masuk atau keluar")
    if sistem:
        return "", None  # "not income / not an expense" lands nowhere, whatever is sent
    kelompok = await _kelompok_id(session, arah, kelompok)
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
    kelompok, operasional = await _periksa(session, arah, kelompok, operasional)
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
    return _baris(k, await _nama_kelompok(session))


async def ubah(session: AsyncSession, id_: str, nama: str, kelompok: str, operasional: str | None, aktif: bool) -> dict:
    await pastikan_bawaan(session)
    k = await session.get(KategoriTransaksi, id_)
    if k is None:
        raise HTTPException(404, "Kategori tidak ditemukan")
    nama = _nama(nama)
    await _nama_bebas(session, k.arah, nama, kecuali=k.id)
    kelompok, operasional = await _periksa(session, k.arah, kelompok, None if id_ in ARUS_KAS_SAJA else operasional, sistem=k.sistem and k.kelompok == "")
    if k.kelompok == "" and k.sistem and not aktif:
        raise HTTPException(409, "Kategori ini tidak bisa disembunyikan")
    k.nama, k.kelompok, k.operasional, k.aktif = nama, kelompok, operasional, aktif
    await session.commit()
    return _baris(k, await _nama_kelompok(session))


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


async def buat_kelompok(session: AsyncSession, arah: str, nama: str) -> dict:
    await pastikan_bawaan(session)
    if arah not in ARAH:
        raise HTTPException(422, "Jenis harus masuk atau keluar")
    nama = _nama(nama)
    if await session.scalar(select(KelompokTransaksi.id).where(KelompokTransaksi.arah == arah, func.lower(KelompokTransaksi.nama) == nama.lower())):
        raise HTTPException(409, "Nama kelompok sudah dipakai")
    dasar = f"kel_{_slug(nama) or 'baru'}"[:44]
    id_ = dasar
    n = 2
    while await session.get(KelompokTransaksi, id_):
        id_ = f"{dasar}_{n}"[:48]
        n += 1
    urut = ((await session.scalar(select(func.max(KelompokTransaksi.urut)).where(KelompokTransaksi.arah == arah))) or 0) + 1
    g = KelompokTransaksi(id=id_, arah=arah, nama=nama, urut=urut)
    session.add(g)
    await session.commit()
    return {"id": g.id, "arah": g.arah, "nama": g.nama, "urut": g.urut}


async def ubah_kelompok(session: AsyncSession, id_: str, nama: str) -> dict:
    await pastikan_bawaan(session)
    g = await session.get(KelompokTransaksi, id_)
    if g is None:
        raise HTTPException(404, "Kelompok tidak ditemukan")
    nama = _nama(nama)
    if await session.scalar(select(KelompokTransaksi.id).where(KelompokTransaksi.arah == g.arah, func.lower(KelompokTransaksi.nama) == nama.lower(), KelompokTransaksi.id != g.id)):
        raise HTTPException(409, "Nama kelompok sudah dipakai")
    g.nama = nama
    await session.commit()
    return {"id": g.id, "arah": g.arah, "nama": g.nama, "urut": g.urut}
