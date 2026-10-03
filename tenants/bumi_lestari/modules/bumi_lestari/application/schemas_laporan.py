from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class BarisKategoriOut(BaseModel):
    kategori: str
    jumlah: Decimal
    jumlah_transaksi: int = 0


class ArusAkunOut(BaseModel):
    akun_id: str
    kode: str
    nama: str
    jenis: str
    saldo_awal: Decimal
    masuk: Decimal
    keluar: Decimal
    transfer_masuk: Decimal
    transfer_keluar: Decimal
    saldo_akhir: Decimal


class LaporanUmumOut(BaseModel):
    dari: date
    sampai: date
    pemasukan: list[BarisKategoriOut]
    total_pemasukan: Decimal
    biaya: list[BarisKategoriOut]
    total_biaya: Decimal
    laba_bersih: Decimal
    di_luar_laba: list[BarisKategoriOut]  # Prive, Bagi hasil, Setoran modal: di luar laba
    arus_kas: list[ArusAkunOut]
    total_kas_awal: Decimal
    total_kas_akhir: Decimal


class TransaksiLaporanOut(BaseModel):
    tanggal: date
    kategori: str
    keterangan: str
    jumlah: Decimal


class PengisianLaporanOut(BaseModel):
    tanggal: date
    jumlah: Decimal
    keterangan: str


class MingguImprestOut(BaseModel):
    minggu_ke: int
    dari: date
    sampai: date
    pemakaian: Decimal
    pengisian: Decimal
    saldo_akhir: Decimal


class LaporanImprestOut(BaseModel):
    """Laporan kas kecil (atau kas iklan) satu bulan, dengan rincian mingguan."""

    periode: str
    akun_id: str
    nama: str
    plafon: Decimal
    saldo_awal: Decimal
    total_pemakaian: Decimal
    total_pengisian: Decimal
    saldo_akhir: Decimal
    sesuai_plafon: bool  # saldo akhir == plafon (setelah pengisian)
    per_kategori: list[BarisKategoriOut]
    per_minggu: list[MingguImprestOut]
    transaksi: list[TransaksiLaporanOut]
    pengisian: list[PengisianLaporanOut]
    saldo_fisik: Optional[Decimal] = None
    selisih: Optional[Decimal] = None  # saldo_fisik - saldo_akhir
    status_selisih: Optional[str] = None  # sesuai | lebih | kurang


class AkunSaldoOut(BaseModel):
    id: str
    kode: str
    nama: str
    jenis: str
    saldo: Decimal
    plafon: Optional[Decimal] = None


class ImprestRingkasOut(BaseModel):
    saldo: Decimal
    plafon: Decimal
    perlu_diisi: Decimal


class DashboardOut(BaseModel):
    periode: str
    selasa: date  # Selasa acuan minggu ini
    akun: list[AkunSaldoOut]
    total_kas: Decimal  # tanpa kas iklan kecuali pengguna admin
    pemasukan_bulan_ini: Decimal
    biaya_bulan_ini: Decimal
    laba_bulan_ini: Decimal
    order_per_status: dict[str, int]
    order_bulan_ini: int
    omzet_order_bulan_ini: Decimal
    piutang_penjual_lain: Decimal  # tagihan penjual lain yang belum dibayar
    utang_pemasok_siap_bayar: Decimal  # tukang/supplier yang dibayar Selasa ini
    dana_cadangan: Decimal
    kas_kecil: Optional[ImprestRingkasOut]
    kas_iklan: Optional[ImprestRingkasOut] = None  # hanya admin
    bagian_admin_pratinjau: Optional[Decimal] = None
    bagian_owner_pratinjau: Optional[Decimal] = None
