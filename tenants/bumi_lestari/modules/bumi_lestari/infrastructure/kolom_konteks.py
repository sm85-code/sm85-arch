"""Konteks peran per request untuk menyaring kolom tambahan di respons (AB-DM-9).

Diisi oleh dependency autentikasi; serializer `kolom_tambahan` di skema keluaran membacanya. Tanpa request (tes,
skrip) tidak ada penyaringan."""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional

PERAN: ContextVar[Optional[str]] = ContextVar("bl_peran", default=None)
KUNCI_STAF: ContextVar[frozenset[str]] = ContextVar("bl_kunci_staf", default=frozenset())


def saring(nilai: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Owner tidak pernah melihat kolom tambahan; staf hanya kolom Transaksi bertanda "tampil untuk staf"."""
    nilai = nilai or {}
    peran = PERAN.get()
    if peran == "owner":
        return {}
    if peran == "staff":
        izin = KUNCI_STAF.get()
        return {k: v for k, v in nilai.items() if k in izin}
    return dict(nilai)
