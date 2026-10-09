"""Explicit contracts for existing listings and Shop GMV Max (api-docs)."""
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo
from pydantic import Field, model_validator
from .workflow_schemas import Input, Dimension, PreOrder, Attribute


class Brand(Input):
    brand_id: int = Field(ge=0)
    original_brand_name: str = Field(min_length=1, max_length=255)


class ItemEdit(Input):
    item_name: str | None = Field(default=None, min_length=1, max_length=255)
    item_sku: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, min_length=1, max_length=12000)
    category_id: int | None = Field(default=None, gt=0)
    attribute_list: list[Attribute] | None = Field(default=None, max_length=100)
    brand: Brand | None = None
    image_ids: list[str] | None = Field(default=None, min_length=1, max_length=9)
    weight: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    dimension: Dimension | None = None
    pre_order: PreOrder | None = None
    # Provider overwrites every model's weight/dimensions when parent fields change.
    apply_to_all_models: bool = False

    @model_validator(mode="after")
    def changes(self):
        values = self.model_dump(exclude_none=True, exclude={"apply_to_all_models"})
        if not values:
            raise ValueError("Isi perubahan produk")
        for name in ("item_name", "description"):
            value = getattr(self, name)
            if value is not None and not value.strip():
                raise ValueError("Nama/deskripsi tidak boleh kosong")
        if self.image_ids is not None and (len(set(self.image_ids)) != len(self.image_ids) or any(not v.strip() for v in self.image_ids)):
            raise ValueError("Foto tidak boleh kosong/duplikat")
        if self.category_id is not None and self.attribute_list is None:
            raise ValueError("Sertakan atribut kategori tujuan saat mengubah kategori")
        return self


class ModelEdit(Input):
    model_id: int = Field(gt=0)
    model_sku: str = Field(max_length=100)
    weight: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    dimension: Dimension | None = None
    pre_order: PreOrder | None = None
    gtin_code: str | None = Field(default=None, pattern=r"^(00|[0-9]{8,14})$")

    @model_validator(mode="after")
    def physical(self):
        if self.dimension is not None and self.weight is None:
            raise ValueError("Berat varian wajib jika dimensi diubah")
        return self


class ModelsEdit(Input):
    model: list[ModelEdit] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique(self):
        if len({m.model_id for m in self.model}) != len(self.model):
            raise ValueError("Varian duplikat")
        return self


class StandardOption(Input):
    variation_option_id: int = Field(ge=0)
    variation_option_name: str | None = Field(default=None, min_length=1, max_length=100)
    image_id: str | None = Field(default=None, min_length=1)


class StandardTier(Input):
    variation_id: int = Field(ge=0)
    variation_name: str | None = Field(default=None, min_length=1, max_length=100)
    variation_group_id: int | None = Field(default=None, ge=0)
    variation_option_list: list[StandardOption] = Field(min_length=1, max_length=50)


class ModelIndex(Input):
    model_id: int = Field(gt=0)
    tier_index: list[int] = Field(min_length=1, max_length=2)


class TiersEdit(Input):
    standardise_tier_variation: list[StandardTier] = Field(min_length=1, max_length=2)
    model_list: list[ModelIndex] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def mapping(self):
        dimensions = [len(t.variation_option_list) for t in self.standardise_tier_variation]
        seen, indices = set(), set()
        for model in self.model_list:
            idx = tuple(model.tier_index)
            if model.model_id in seen or idx in indices or len(idx) != len(dimensions) or any(i < 0 or i >= n for i, n in zip(idx, dimensions)):
                raise ValueError("Pemetaan pilihan varian tidak valid/duplikat")
            seen.add(model.model_id)
            indices.add(idx)
        return self


def today():
    return datetime.now(ZoneInfo("Asia/Jakarta")).date()


class GmvCreate(Input):
    daily_budget: Decimal = Field(gt=0, le=100000000, allow_inf_nan=False)
    start_date: date
    end_date: date | None = None
    roas_target: Decimal | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    reference_id: UUID

    @model_validator(mode="after")
    def dates(self):
        if self.start_date < today() or (self.end_date is not None and self.end_date < self.start_date):
            raise ValueError("Jadwal kampanye tidak valid")
        return self


class GmvEdit(Input):
    campaign_id: int = Field(gt=0)
    edit_action: Literal["change_budget", "change_duration", "pause", "resume", "start", "change_roas_target"]
    daily_budget: Decimal | None = Field(default=None, gt=0, le=100000000, allow_inf_nan=False)
    start_date: date | None = None
    end_date: date | None = None
    roas_target: Decimal | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    reference_id: UUID

    @model_validator(mode="after")
    def action_fields(self):
        allowed = {"change_budget": {"daily_budget"}, "change_duration": {"start_date", "end_date"}, "change_roas_target": {"roas_target"}}.get(self.edit_action, set())
        supplied = {k for k in ("daily_budget", "start_date", "end_date", "roas_target") if getattr(self, k) is not None}
        if supplied - allowed or (allowed and not supplied) or (self.edit_action == "change_duration" and self.start_date is None):
            raise ValueError("Parameter tidak sesuai tindakan GMV Max")
        if self.start_date is not None and (self.start_date < today() or (self.end_date is not None and self.end_date < self.start_date)):
            raise ValueError("Jadwal kampanye tidak valid")
        return self


class GmvItems(Input):
    campaign_id: int = Field(gt=0)
    edit_action: Literal["add", "remove"]
    item_id_list: list[int] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def ids(self):
        if len(set(self.item_id_list)) != len(self.item_id_list) or any(v <= 0 for v in self.item_id_list):
            raise ValueError("ID produk tidak valid/duplikat")
        return self
