from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class ProdukERPIn(BaseModel):
    platform: str
    akun_id: str
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
    akun_id: str
    id_eksternal_pembeli: str
    nama_pembeli: str = ""


# --- Akun Marketplace ---------------------------------------------------


class AkunMarketplaceIn(BaseModel):
    platform: str
    nama_toko: str
    id_toko_eksternal: Optional[str] = None
    catatan: Optional[str] = None


class AkunMarketplacePatch(BaseModel):
    nama_toko: Optional[str] = None
    id_toko_eksternal: Optional[str] = None
    status: Optional[str] = None
    catatan: Optional[str] = None
    # Kredensial -- diisi admin secara manual untuk sekarang (belum ada
    # callback OAuth sungguhan, lihat models.py::AkunMarketplace docstring).
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_kedaluwarsa: Optional[datetime] = None
