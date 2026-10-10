"""Explicit contracts from api-docs/schemas (Shopee SDK 0d38ec6)."""
from typing import Literal
from itertools import product

from pydantic import Field, HttpUrl, model_validator

from .workflow_schemas import Input, Model, Tier


class ShopProfileEdit(Input):
    shop_name: str | None = Field(None, min_length=1, max_length=100)
    shop_logo: HttpUrl | None = None
    description: str | None = Field(None, max_length=5000)

    @model_validator(mode="after")
    def changes(self):
        if not self.model_fields_set or all(getattr(self, k) is None for k in self.model_fields_set):
            raise ValueError("Isi perubahan profil toko")
        if self.shop_name is not None and not self.shop_name.strip():
            raise ValueError("Nama toko tidak boleh kosong")
        return self


class HolidayEdit(Input):
    holiday_mode_on: bool
    holiday_mode_type: Literal[0, 1] = 0
    holiday_mode_start_time: int | None = Field(None, gt=0)
    holiday_mode_end_time: int | None = Field(None, gt=0)
    holiday_mode_description: str | None = Field(None, max_length=500)

    @model_validator(mode="after")
    def schedule(self):
        start, end = self.holiday_mode_start_time, self.holiday_mode_end_time
        if (start is None) != (end is None):
            raise ValueError("Isi awal dan akhir jadwal libur")
        if start is not None and (start % 3600 or (end + 1) % 3600 or end <= start):
            raise ValueError("Awal libur harus tepat jam; akhir libur pada menit 59:59 setelah awal")
        if self.holiday_mode_on and self.holiday_mode_type == 1 and start is None:
            raise ValueError("Libur sebagian memerlukan jadwal")
        if not self.holiday_mode_on and start is not None:
            raise ValueError("Nonaktifkan libur tanpa jadwal")
        return self


class DriverSetting(Input):
    auto_call_driver_enabled: bool
    preparation_time: int | None = Field(None, ge=0)

    @model_validator(mode="after")
    def preparation(self):
        if self.auto_call_driver_enabled and self.preparation_time is None:
            raise ValueError("Isi waktu persiapan penjemputan")
        return self


class ChannelEdit(Input):
    enabled: bool | None = None
    cod_enabled: bool | None = None
    auto_call_driver_setting: DriverSetting | None = None

    @model_validator(mode="after")
    def changes(self):
        if not self.model_dump(exclude_none=True):
            raise ValueError("Isi perubahan jasa kirim")
        return self


class OrderNote(Input):
    note: str = Field(max_length=500)


class AddressConfig(Input):
    address_id: int = Field(gt=0)
    address_type: list[Literal["DEFAULT_ADDRESS", "PICKUP_ADDRESS", "RETURN_ADDRESS", "INBOUND_PICKUP_ADDRESS"]] = Field(min_length=1, max_length=4)
    show_pickup_address: bool | None = None

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.address_type)) != len(self.address_type):
            raise ValueError("Jenis alamat tidak boleh duplikat")
        return self


class ModelAdd(Input):
    # Reuse the validated price, stock, weight and tier-index publication contract.
    models: list[Model] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique(self):
        if any(i < 0 for model in self.models for i in model.tier_index):
            raise ValueError("Indeks pilihan varian tidak valid")
        if len({tuple(m.tier_index) for m in self.models}) != len(self.models):
            raise ValueError("Kombinasi varian tidak boleh duplikat")
        return self


class ModelsInit(ModelAdd):
    tiers: list[Tier] = Field(min_length=1, max_length=2)
    location_id: str | None = Field(None, max_length=64)

    @model_validator(mode="after")
    def combinations(self):
        expected = set(product(*(range(len(t.options)) for t in self.tiers)))
        if expected != {tuple(m.tier_index) for m in self.models}:
            raise ValueError("Isi semua kombinasi pilihan varian tepat satu kali")
        for tier in self.tiers:
            if len({o.option.casefold().strip() for o in tier.options}) != len(tier.options) or not tier.name.strip() or any(not o.option.strip() for o in tier.options):
                raise ValueError("Nama pilihan varian tidak boleh kosong/duplikat")
        return self
