"""Validated inputs for the keu ledger. Money remains Decimal and actor comes from auth."""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AliasChoices, AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, SecretStr, StrictBool, StrictInt, StringConstraints, field_validator, model_validator

Id = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Ref = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
Money = Annotated[Decimal, Field(max_digits=20, decimal_places=2, allow_inf_nan=False)]
NonNegativeMoney = Annotated[Decimal, Field(ge=0, max_digits=20, decimal_places=2, allow_inf_nan=False)]
PositiveMoney = Annotated[Decimal, Field(gt=0, max_digits=20, decimal_places=2, allow_inf_nan=False)]
Qty = Annotated[StrictInt, Field(ge=1, le=10000)]
Segment = Literal["umkm", "reseller"]


class InputBase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, from_attributes=True)


class SaluranIn(InputBase):
    nama: str = Field(min_length=1, max_length=128)
    sistem: Literal["store", "marketplace_erp", "manual"]
    akun_ref: str = Field(min_length=1, max_length=128)
    aktif: bool = False


class AkunIn(InputBase):
    kode: str = Field(min_length=1, max_length=64)
    nama: str = Field(min_length=1, max_length=128)
    jenis: Literal["kas", "bank", "ewallet"]
    saldo_awal: Money = Decimal("0")


class PelangganIn(InputBase):
    nama: str = Field(min_length=1, max_length=255)
    segmen: Segment | None = None
    kontak: str = Field(default="", max_length=255)


class MasterStatusIn(InputBase):
    aktif: StrictBool


class ResetKeuIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: Id
    token: str = Field(min_length=32, max_length=128)
    konfirmasi: Literal["RESET-KEUANGAN"]
    password: SecretStr = Field(min_length=1, max_length=255)


class VendorIn(InputBase):
    kode: str | None = Field(default=None, min_length=1, max_length=128)
    nama: str = Field(min_length=1, max_length=255)
    jenis: Literal["tukang_kayu", "supplier"] = Field(validation_alias=AliasChoices("jenis", "tipe"))
    kontak: str = Field(default="", max_length=255)
    alamat: str = Field(default="", max_length=2000)
    keterangan: str = Field(default="", max_length=2000)
    aktif: StrictBool = True

    @field_validator("jenis", mode="before")
    @classmethod
    def vendor_kind(cls, value):
        return {"kayu": "tukang_kayu", "non_kayu": "supplier"}.get(value, value) if isinstance(value, str) else value


class VendorEditIn(InputBase):
    kode: str | None = Field(default=None, min_length=1, max_length=128)
    nama: str | None = Field(default=None, min_length=1, max_length=255)
    jenis: Literal["tukang_kayu", "supplier"] | None = Field(default=None, validation_alias=AliasChoices("jenis", "tipe"))
    kontak: str | None = Field(default=None, max_length=255)
    alamat: str | None = Field(default=None, max_length=2000)
    keterangan: str | None = Field(default=None, max_length=2000)
    aktif: StrictBool | None = None
    vendor_kind = field_validator("jenis", mode="before")(VendorIn.vendor_kind.__func__)

    @model_validator(mode="after")
    def non_null_updates(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Isi sedikitnya satu perubahan; nilai null tidak diizinkan")
        return self


class VendorSlotIn(InputBase):
    jenis: Literal["tukang_kayu", "supplier"]
    nomor: Annotated[StrictInt, Field(ge=1, le=2147483647)]
    vendor_id: Id


class VarianIn(InputBase):
    kategori: str = Field(min_length=1, max_length=128)
    nilai: str = Field(min_length=1, max_length=255)


class ProdukSumberIn(InputBase):
    sku: str = Field(min_length=1, max_length=128)
    sku_induk: str | None = Field(default=None, min_length=1, max_length=128)
    nama_asli: str = Field(min_length=1, max_length=255)
    gambar_url: str = Field(default="", max_length=2048)
    varian_list: list[VarianIn] = Field(default_factory=list)
    harga_jual: NonNegativeMoney = Decimal("0")

    @field_validator("gambar_url")
    @classmethod
    def image_url(cls, value):
        if value:
            parts = urlsplit(value)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
                raise ValueError("Gambar harus berupa URL HTTP/HTTPS tanpa kredensial")
        return value


class ProdukEditIn(InputBase):
    nama: str = Field(min_length=1, max_length=255)
    jenis: Literal["kayu", "non_kayu"]
    biaya_acuan: NonNegativeMoney = Decimal("0")
    varian_list: list[VarianIn] = Field(default_factory=list)


class ProdukIn(ProdukEditIn):
    sku: str = Field(min_length=1, max_length=128)
    sku_induk: str | None = Field(default=None, min_length=1, max_length=128)
    gambar_url: str = Field(default="", max_length=2048)
    harga_jual: NonNegativeMoney = Decimal("0")
    image_url = field_validator("gambar_url")(ProdukSumberIn.image_url.__func__)


class ItemIn(InputBase):
    produk_sumber: ProdukSumberIn | None = None
    sumber_ref: Ref
    produk_id: Id | None = None
    nama_snapshot: str = Field(min_length=1, max_length=255)
    varian_snapshot: str = Field(default="", max_length=255)
    qty: Qty
    harga_satuan: NonNegativeMoney
    subtotal_sumber: NonNegativeMoney


class PesananIn(InputBase):
    saluran_id: Id
    sumber_ref: Ref
    nomor: str = Field(min_length=1, max_length=128)
    tanggal: date
    pelanggan_id: Id | None = None
    segmen_snapshot: Segment | None = None
    status_sumber: str = Field(min_length=1, max_length=64)
    total_sumber: NonNegativeMoney
    sumber_updated_at: AwareDatetime | None = None
    items: list[ItemIn] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def item_unik(self):
        refs = [item.sumber_ref for item in self.items]
        if len(refs) != len(set(refs)):
            raise ValueError("Referensi item duplikat")
        return self


class AlokasiVendorIn(InputBase):
    item_id: Id
    vendor_id: Id
    qty: Qty
    biaya_satuan: NonNegativeMoney


class SettlementIn(InputBase):
    saluran_id: Id
    sumber_ref: Ref
    tanggal_cair: date
    bruto: Money
    potongan: Money
    penyesuaian: Money = Decimal("0")
    neto: Money
    rincian: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def rekonsiliasi(self):
        if self.neto != self.bruto - self.potongan + self.penyesuaian:
            raise ValueError("Neto harus bruto - potongan + penyesuaian")
        return self


class AlokasiSettlementIn(InputBase):
    settlement_id: Id
    item_id: Id
    jumlah: Money


class TransaksiIn(InputBase):
    saluran_id: Id
    sumber_ref: Ref
    akun_id: Id
    kategori_id: Id
    tanggal: date
    jenis: Literal["masuk", "keluar"]
    jumlah: PositiveMoney
    keterangan: str = Field(default="", max_length=2000)


class BiayaOperasionalIn(TransaksiIn):
    jenis: Literal["keluar"] = "keluar"


class ImporIn(InputBase):
    saluran_id: Id
    jenis: Literal["order", "settlement", "biaya"]
    nama_file: str = Field(min_length=1, max_length=255)
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    versi_format: Annotated[StrictInt, Field(ge=1)] = 1
    pemetaan: dict[str, JsonValue] = Field(default_factory=dict)


class MasukanIn(InputBase):
    saluran_id: Id
    entitas: Literal["produk", "order", "settlement", "biaya"]
    sumber_ref: Ref
    sumber_updated_at: AwareDatetime | None = None
    impor_id: Id | None = None
    nomor_baris: Annotated[StrictInt, Field(ge=1, le=50000)] | None = None
    payload: dict[str, JsonValue]

    @model_validator(mode="after")
    def asal_impor(self):
        if (self.impor_id is None) != (self.nomor_baris is None):
            raise ValueError("impor_id dan nomor_baris harus diisi bersama")
        return self


def checksum_revisi(value: MasukanIn) -> str:
    encoded = json.dumps(value.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class BatalIn(InputBase):
    alasan: str = Field(min_length=3, max_length=2000)


class ItemPetaIn(InputBase):
    produk_id: Id


class PostingSettlementIn(InputBase):
    akun_id: Id
    kategori_id: Id


class PesananStatusIn(InputBase):
    status: Literal["draf", "aktif", "selesai", "batal"]
    alasan: str = Field(default="", max_length=2000)


class PageOut(BaseModel):
    rows: list[dict]
    total: int
    limit: int
    offset: int
