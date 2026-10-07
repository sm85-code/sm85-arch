"""Strict, tenant-local accounting inputs; no actor or posting status accepted from clients."""
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from .schemas_keu import Id, InputBase, NonNegativeMoney, PositiveMoney, Qty, Ref, SettlementIn

Arus = Literal["operasional", "investasi", "pendanaan"]


class MutasiIn(InputBase):
    referensi: Ref
    tanggal: date
    akun_asal_id: Id
    akun_tujuan_id: Id
    nominal: PositiveMoney
    biaya_admin: NonNegativeMoney = Decimal("0")
    keterangan: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def different_accounts(self):
        if self.akun_asal_id == self.akun_tujuan_id:
            raise ValueError("Akun asal dan tujuan harus berbeda")
        return self


class CoaIn(InputBase):
    kode: str = Field(min_length=1, max_length=64)
    nama: str = Field(min_length=1, max_length=128)
    jenis: Literal["aset", "kewajiban", "ekuitas", "pendapatan", "beban"]
    kelompok: Literal["aset_lain", "kewajiban_lain", "ekuitas_lain", "pendapatan_lain", "beban_operasional", "beban_langganan", "beban_sewa", "beban_pemeliharaan", "beban_admin"]

    @model_validator(mode="after")
    def matching_group(self):
        expected = {"aset_lain": "aset", "kewajiban_lain": "kewajiban", "ekuitas_lain": "ekuitas", "pendapatan_lain": "pendapatan"}.get(self.kelompok, "beban")
        if self.jenis != expected:
            raise ValueError("Jenis akun tidak sesuai kelompok")
        return self


class KategoriCoaIn(InputBase):
    coa_id: Id
    arus: Arus


class JurnalBarisIn(InputBase):
    coa_id: Id
    debet: NonNegativeMoney = Decimal("0")
    kredit: NonNegativeMoney = Decimal("0")
    arus: Arus | None = None
    vendor_id: Id | None = None

    @model_validator(mode="after")
    def one_side(self):
        if not ((self.debet > 0 and self.kredit == 0) or (self.kredit > 0 and self.debet == 0)):
            raise ValueError("Isi hanya debet atau kredit positif pada setiap baris")
        return self


class JurnalIn(InputBase):
    referensi: Ref
    tanggal: date
    keterangan: str = Field(min_length=3, max_length=2000)
    baris: list[JurnalBarisIn] = Field(min_length=2, max_length=100)

    @model_validator(mode="after")
    def balanced(self):
        if sum(b.debet for b in self.baris) != sum(b.kredit for b in self.baris):
            raise ValueError("Total debet harus sama dengan kredit")
        return self


class StokMasukIn(InputBase):
    referensi: Ref
    tanggal: date
    produk_id: Id
    qty: Qty
    biaya_total: PositiveMoney
    sumber: Literal["pembelian", "produksi"]
    akun_kas_id: Id | None = None
    vendor_id: Id | None = None
    keterangan: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def one_payment_source(self):
        if bool(self.akun_kas_id) == bool(self.vendor_id):
            raise ValueError("Pilih satu sumber: kas/bank atau utang vendor")
        return self


class StokKeluarIn(InputBase):
    referensi: Ref
    tanggal: date
    item_id: Id
    qty: Qty
    keterangan: str = Field(default="", max_length=2000)


class BayarVendorIn(InputBase):
    referensi: Ref
    tanggal: date
    vendor_id: Id
    akun_kas_id: Id
    jumlah: PositiveMoney
    keterangan: str = Field(default="", max_length=2000)


class PiutangIn(InputBase):
    referensi: Ref
    tanggal: date
    pesanan_id: Id
    posisi: Literal["pengiriman", "escrow"]


class BukuIn(InputBase):
    tanggal_awal: date
    metode_stok: Literal["fifo"]
    tanggal_status: Literal["updated_at"]
    histori: Literal[True]
    konfirmasi: Literal["AKTIFKAN-BUKU-KEU"]


class SettlementBuktiIn(SettlementIn):
    bukti_pencairan: str = Field(min_length=3, max_length=2000)
