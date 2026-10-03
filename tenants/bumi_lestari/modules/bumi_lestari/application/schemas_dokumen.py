"""Data dokumen cetak: Purchase Order ke tukang/supplier dan Invoice ke penjual lain (reseller).

Backend hanya menyusun datanya; tampilan/cetak PDF (logo, tata letak) dilakukan frontend.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class PerusahaanOut(BaseModel):
    nama: str
    alamat: str


class MingguOut(BaseModel):
    label: str  # "Minggu ke-4 September"
    periode_awal: date  # Senin
    periode_akhir: date  # Sabtu


class PoItemOut(BaseModel):
    tanggal_selesai: date
    hari: str
    kode_pesanan: str
    nama_barang: str
    ukuran: str
    qty: int
    harga_barang: Decimal  # per unit
    total: Decimal
    terlambat: bool = False


class PoKepadaOut(BaseModel):
    pemasok_id: str
    nama: str
    nama_bank: str
    no_rekening: str
    atas_nama: str


class PurchaseOrderOut(BaseModel):
    nomor: str  # PO/MG.4-005/IX/2026
    perusahaan: PerusahaanOut
    minggu: MingguOut
    tgl_pembayaran: date  # Selasa
    kepada: PoKepadaOut
    items: list[PoItemOut]
    total_qty: int
    grand_total: Decimal


class InvoiceItemOut(BaseModel):
    order_id: str
    tanggal: date
    hari: str
    nama_barang: str
    ukuran: str
    qty: int
    harga_barang: Decimal
    biaya_jasa_pengecatan: Decimal  # cat + jasa + packing; packing tetap ada pada order polos
    biaya_proses: Decimal
    total: Decimal
    terlambat: bool = False


class InvoiceKepadaOut(BaseModel):
    pelanggan_id: str
    nama: str
    alamat: str


class InvoiceOut(BaseModel):
    nomor: str  # INV/MG.4-002/IX/2026
    perusahaan: PerusahaanOut
    minggu: MingguOut
    tgl_invoice: date  # Senin setelah periode
    jatuh_tempo: date  # Selasa
    kepada: InvoiceKepadaOut
    items: list[InvoiceItemOut]
    total_barang: Decimal
    total_jasa_pengecatan: Decimal
    total_biaya_proses: Decimal
    grand_total: Decimal
    syarat: list[str]
    info_pembayaran: Optional[str] = None
