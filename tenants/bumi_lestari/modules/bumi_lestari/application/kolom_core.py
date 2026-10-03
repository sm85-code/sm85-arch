"""Kolom tambahan (spesifikasi 10.4): validasi nilai di backend (AB-DM-6) dan penerapan ke entitas.

Kolom tambahan tidak pernah dibaca rumus keuangan (AB-DM-7); modul ini hanya menyimpan nilai ke `kolom_tambahan`."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlTransaksi, BlUser
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_kolom import LAPISAN_TAMBAHAN, BlDefinisiKolom
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import BlOrder, BlPelanggan, BlPemasok, BlProduk
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_pembayaran import BlKaryawan

ENTITAS = {
    "order": BlOrder, "produk": BlProduk, "pemasok": BlPemasok, "pelanggan": BlPelanggan,
    "transaksi": BlTransaksi, "karyawan": BlKaryawan,
}
LABEL_ENTITAS = {
    "order": "Order", "produk": "Produk", "pemasok": "Tukang & supplier", "pelanggan": "Penjual lain",
    "transaksi": "Transaksi", "karyawan": "Karyawan",
}
_BUKAN_INTI = {"id", "created_at", "updated_at", "kolom_tambahan", "sumber_sistem", "sumber_ref"}
MAKS_TEKS = 500


def _bad(detail: str, code: int = 422) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


def kolom_inti(entitas: str) -> list[tuple[str, bool]]:
    """(kunci, wajib) kolom inti dari tabel entitas -- semua kolom yang ada di database adalah inti (10.2)."""
    tabel = ENTITAS[entitas].__table__
    hasil = []
    for col in tabel.columns:
        if col.name in _BUKAN_INTI:
            continue
        hasil.append((col.name, not col.nullable and col.default is None and col.server_default is None))
    return hasil


def kunci_dari_label(label: str) -> str:
    k = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return (k or "kolom")[:60]


def _angka(v: Any) -> Decimal:
    if isinstance(v, bool):
        raise ValueError
    if isinstance(v, (int, float, Decimal)):
        return Decimal(str(v))
    t = str(v).strip().replace("Rp", "").replace("rp", "").replace(" ", "")
    if "," in t:  # format Indonesia: titik ribuan, koma desimal
        t = t.replace(".", "").replace(",", ".")
    elif t.count(".") > 1 or re.fullmatch(r"-?\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    return Decimal(t)


def validasi_nilai(d: BlDefinisiKolom, v: Any, lama: Any = None) -> Any:
    """Nilai bersih sesuai jenis data (10.1); None = kosong. Galat 422 berbahasa sederhana."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        if d.tipe == "teks":
            t = str(v).strip()
            if len(t) > MAKS_TEKS:
                raise _bad(f"{d.label}: maksimal {MAKS_TEKS} karakter")
            return t
        if d.tipe in ("angka", "mata_uang"):
            n = _angka(v)
            if not n.is_finite():
                raise ValueError
            if d.tipe == "mata_uang":
                if n != n.quantize(Decimal("0.01")):
                    raise _bad(f"{d.label}: maksimal 2 angka desimal")
                if n < 0 and not (d.min is not None and Decimal(d.min) < 0):
                    raise _bad(f"{d.label}: tidak boleh negatif")
            if d.min is not None and n < Decimal(d.min):
                raise _bad(f"{d.label}: minimal {d.min}")
            if d.maks is not None and n > Decimal(d.maks):
                raise _bad(f"{d.label}: maksimal {d.maks}")
            return format(n.normalize(), "f") if d.tipe == "angka" else str(n.quantize(Decimal("0.01")))
        if d.tipe == "tanggal":
            t = str(v).strip()
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
                try:
                    return datetime.strptime(t, fmt).date().isoformat()
                except ValueError:
                    continue
            if isinstance(v, date):
                return v.isoformat()
            raise ValueError
        if d.tipe == "pilihan":
            t = str(v).strip()
            aktif = [p["nilai"] for p in d.pilihan or [] if not p.get("arsip")]
            semua = [p["nilai"] for p in d.pilihan or []]
            if t in aktif or (t == lama and t in semua):  # pilihan arsip tetap sah untuk nilai lama (AB-DM-3)
                return t
            raise _bad(f"{d.label}: pilih salah satu dari {', '.join(aktif)}")
        if d.tipe == "ya_tidak":
            if isinstance(v, bool):
                return v
            t = str(v).strip().lower()
            if t in ("ya", "true", "1", "y"):
                return True
            if t in ("tidak", "false", "0", "t", "n"):
                return False
            raise ValueError
    except HTTPException:
        raise
    except (ValueError, InvalidOperation, TypeError):
        pass
    raise _bad(f"{d.label}: nilai tidak sah untuk jenis {d.tipe.replace('_', '/')}")


async def definisi_aktif(session: AsyncSession, entitas: str) -> dict[str, BlDefinisiKolom]:
    rows = (
        await session.execute(
            select(BlDefinisiKolom).where(
                BlDefinisiKolom.entitas == entitas, BlDefinisiKolom.lapisan == LAPISAN_TAMBAHAN, BlDefinisiKolom.aktif.is_(True),
            )
        )
    ).scalars()
    return {d.kunci: d for d in rows}


async def terapkan(
    session: AsyncSession, entitas: str, obj, nilai: dict[str, Any] | None, *, baru: bool, user: BlUser | None = None,
) -> None:
    """Validasi lalu simpan nilai kolom tambahan ke `obj.kolom_tambahan`.

    `nilai=None` saat ubah = tidak menyentuh kolom tambahan. Wajib berlaku untuk data baru dan saat kolom tambahan
    dikirim ulang (AB-DM-5). Staf hanya boleh mengisi kolom Transaksi bertanda tampil untuk staf (AB-DM-9)."""
    if nilai is None and not baru:
        return
    defs = await definisi_aktif(session, entitas)
    lama = dict(obj.kolom_tambahan or {})
    masuk = nilai or {}
    staf = user is not None and (user.role or "").lower() == "staff"
    hasil = dict(lama)
    for k, v in masuk.items():
        d = defs.get(k)
        if d is None:
            raise _bad(f"Kolom tambahan '{k}' tidak dikenal atau nonaktif")
        if staf and not d.tampil_staf:
            raise _bad(f"Kolom '{d.label}' tidak bisa diisi staf", status.HTTP_403_FORBIDDEN)
        bersih = validasi_nilai(d, v, lama.get(k))
        if bersih is None:
            hasil.pop(k, None)
        else:
            hasil[k] = bersih
    if baru:
        for k, d in defs.items():
            if k not in hasil and d.nilai_bawaan not in (None, "") and not (staf and not d.tampil_staf):
                hasil[k] = validasi_nilai(d, d.nilai_bawaan)
    kurang = [
        d.label for k, d in defs.items()
        if d.wajib and hasil.get(k) in (None, "") and not (staf and not d.tampil_staf)
    ]
    if kurang:
        raise _bad(f"Kolom wajib belum diisi: {', '.join(sorted(kurang))}")
    obj.kolom_tambahan = hasil


class _Wadah:
    def __init__(self) -> None:
        self.kolom_tambahan: dict[str, Any] = {}


async def nilai_baru(session: AsyncSession, entitas: str, nilai: dict[str, Any] | None, *, user: BlUser | None = None) -> dict:
    """Nilai kolom tambahan tervalidasi untuk data baru (dipakai bila objek dibuat lebih dari satu, mis. talangan)."""
    w = _Wadah()
    await terapkan(session, entitas, w, nilai, baru=True, user=user)
    return w.kolom_tambahan
