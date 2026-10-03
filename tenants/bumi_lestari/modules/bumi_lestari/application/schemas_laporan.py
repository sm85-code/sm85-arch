from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel

from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_kiriman import DrafSumberOut


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
    di_luar_laba: list[BarisKategoriOut]  # Prive dan Bagi hasil: tidak mengurangi laba
    arus_kas: list[ArusAkunOut]
    total_kas_awal: Decimal
    total_kas_akhir: Decimal
    # Draf yang belum dikirim ke laporan keuangan (tidak dihitung di atas), per sumber.
    draf_belum_dikirim: list[DrafSumberOut] = []
    # Belum cair per tanggal `sampai` (AB-BC-2): tidak masuk laba; perkiraan laba jika semua cair hanya informasi.
    belum_cair: Decimal = Decimal("0")
    perkiraan_laba_jika_cair: Optional[Decimal] = None
    # Bulan tutup buku: angka diambil dari snapshot saat ditutup.
    dari_snapshot: bool = False
    ditutup_pada: Optional[datetime] = None


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
    total_draf_belum_dikirim: Decimal = Decimal("0")  # pengeluaran draf bulan ini (belum masuk laporan)


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
    order_per_status: dict[str, int]  # order bulan ini (tanggal_order dalam periode) per status
    order_aktif_per_status: dict[str, int] = {}  # semua order yang masih berjalan (belum selesai/batal)
    order_bulan_ini: int
    omzet_order_bulan_ini: Decimal
    piutang_penjual_lain: Decimal  # tagihan penjual lain yang belum dibayar
    utang_pemasok_siap_bayar: Decimal  # tukang/supplier yang dibayar Selasa ini
    dana_cadangan: Decimal
    kas_kecil: Optional[ImprestRingkasOut]
    kas_iklan: Optional[ImprestRingkasOut] = None  # hanya admin
    bagian_admin_pratinjau: Optional[Decimal] = None
    bagian_owner_pratinjau: Optional[Decimal] = None
    tagihan_penjual_lain_minggu_ini: Decimal = Decimal("0")  # kirim s.d. Sabtu lalu, belum dibayar
    belum_cair: Decimal = Decimal("0")  # perkiraan uang cair order marketplace/Toko web yang sudah dikirim, belum cair
    belum_cair_sementara: Decimal = Decimal("0")  # nama lama (sama dengan belum_cair), dipertahankan untuk frontend lama
    draf_belum_dikirim: list[DrafSumberOut] = []  # hanya sumber yang punya draf


class OrderBelumCairOut(BaseModel):
    order_id: str
    no_order: str
    tgl_dikirim: date
    penjualan: Decimal  # harga jual bruto
    potongan: Decimal  # potongan aktual bila ada, selain itu perkiraan dari order
    perkiraan_cair: Decimal
    status: str


class SaluranBelumCairOut(BaseModel):
    saluran_id: str
    nama: str
    akun_id: Optional[str]
    jumlah_order: int
    total_penjualan: Decimal
    total_perkiraan_cair: Decimal
    tgl_kirim_tertua: Optional[date]
    order: list[OrderBelumCairOut]


class BelumCairOut(BaseModel):
    """Piutang marketplace/Toko web berisiko retur (AB-BC-1): sudah dikirim, belum cair, belum retur."""
    per_tanggal: date
    jumlah_order: int
    total_penjualan: Decimal
    total_perkiraan_cair: Decimal
    tgl_kirim_tertua: Optional[date]
    per_saluran: list[SaluranBelumCairOut]
