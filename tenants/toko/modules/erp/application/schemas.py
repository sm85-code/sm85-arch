from __future__ import annotations

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class ProdukERPIn(BaseModel):
    platform: str
    id_eksternal: str
    nama: str
    deskripsi: str = ""
    harga: Decimal
    stok: int = 0
    foto_url: Optional[str] = None


class ProdukERPPatch(BaseModel):
    nama: Optional[str] = None
    deskripsi: Optional[str] = None
    harga: Optional[Decimal] = None
    stok: Optional[int] = None
    foto_url: Optional[str] = None


class StatusPesananERPIn(BaseModel):
    status: str


class PesanChatERPIn(BaseModel):
    isi: str


class PercakapanERPIn(BaseModel):
    """Dipakai admin untuk membuka/membuat thread chat marketplace secara
    manual (mis. saat pesan masuk lewat sinkronisasi platform yang belum
    tersambung -- lihat infrastructure/erp_<platform>.py)."""

    platform: str
    id_eksternal_pembeli: str
    nama_pembeli: str = ""
