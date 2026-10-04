"""Skema laporan keuangan Fase 2.13: laba rugi (9.1), neraca (9.2), HPP & margin (9.3), ringkasan Owner (9.7)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class BarisNilai(BaseModel):
    label: str
    jumlah: Decimal
    rincian: list["BarisNilai"] = []


class CatatanBelumCair(BaseModel):
    total_penjualan: Decimal
    total_perkiraan_cair: Decimal
    jumlah_order: int
    tgl_kirim_tertua: Optional[date]
    perkiraan_laba_jika_cair: Decimal  # info saja: laba bersih + perkiraan cair


class LabaRugiOut(BaseModel):
    periode: str
    sementara: bool  # True = bulan belum ditutup
    penjualan: list[BarisNilai]  # per saluran (bruto)
    total_penjualan: Decimal
    biaya_marketplace: list[BarisNilai]  # per saluran, rincian per jenis potongan
    total_biaya_marketplace: Decimal
    penjualan_bersih: Decimal
    hpp: Decimal
    laba_kotor: Decimal
    margin_persen: Optional[Decimal]
    biaya_operasional: list[BarisNilai]
    total_biaya_operasional: Decimal
    pendapatan_lain: list[BarisNilai]
    laba_bersih: Decimal
    # Bagian penjualan penjual lain yang berasal dari biaya proses (sudah termasuk di total_penjualan).
    pendapatan_biaya_proses: Decimal = Decimal("0")
    hpp_dicocokkan: Decimal  # info: biaya pokok order yang penjualannya diakui bulan ini (lihat /laporan/hpp-margin)
    belum_cair: CatatanBelumCair
    di_luar_laba: list[BarisNilai]  # Prive, Bagi hasil


class NeracaOut(BaseModel):
    per_tanggal: date
    aset_kas: list[BarisNilai]
    piutang_penjual_lain: Decimal
    belum_cair: list[BarisNilai]  # per saluran, nilai perkiraan cair
    total_aset: Decimal
    utang_pemasok: list[BarisNilai]  # per tukang/supplier
    dana_gaji_belum_dibayar: Decimal
    talangan: list[BarisNilai]  # per orang
    total_kewajiban: Decimal
    modal: list[BarisNilai]
    total_modal: Decimal
    selisih: Decimal  # aset - (kewajiban + modal); != 0 berarti ada saldo/transaksi yang perlu diperiksa


class MarginBaris(BaseModel):
    label: str
    qty: int = 0
    jumlah_order: int = 0
    penjualan: Decimal
    potongan: Decimal
    hpp: Decimal
    laba_kotor: Decimal
    margin_persen: Optional[Decimal]
    # Biaya proses (penjual lain) di luar margin produk; ditampilkan sebagai pendapatan terpisah.
    biaya_proses: Decimal = Decimal("0")


class HppMarginOut(BaseModel):
    periode: str
    sementara: bool
    per_produk: list[MarginBaris]
    per_saluran: list[MarginBaris]
    total: MarginBaris
    pendapatan_biaya_proses: Decimal = Decimal("0")


class TrenBulan(BaseModel):
    periode: str
    penjualan: Decimal
    laba_bersih: Decimal
    sementara: bool


class BagiHasilOwner(BaseModel):
    periode: str
    laba_bersih: Decimal
    persen_owner: Decimal
    bagian_owner: Decimal
    dibayar: bool
    tanggal_bayar: Optional[date]


class RingkasanOwnerOut(BaseModel):
    periode: str  # bulan terakhir yang ditutup (atau bulan berjalan bila belum ada)
    sementara: bool
    penjualan: Decimal
    hpp: Decimal
    biaya_iklan: Decimal
    biaya_operasional_lain: Decimal  # termasuk biaya marketplace
    laba_bersih: Decimal
    tren: list[TrenBulan]
    total_kas: Decimal
    piutang: Decimal  # tagihan penjual lain + belum cair
    utang: Decimal
    modal: Decimal
    setoran_modal: Decimal
    laba_ditahan: Decimal
    bagi_hasil_dibayar: Decimal
    bagi_hasil: list[BagiHasilOwner]
