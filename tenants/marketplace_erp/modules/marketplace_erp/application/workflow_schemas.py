"""Explicit write contracts for listing publication and return disputes."""

from decimal import Decimal
from itertools import product
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Dimension(Input):
    package_length: int = Field(gt=0)
    package_width: int = Field(gt=0)
    package_height: int = Field(gt=0)


class PreOrder(Input):
    is_pre_order: bool = False
    days_to_ship: int = Field(default=2, ge=1, le=180)


class AttributeValue(Input):
    value_id: int = Field(ge=0)
    original_value_name: str = Field(min_length=1, max_length=255)
    value_unit: str | None = None


class Attribute(Input):
    attribute_id: int = Field(gt=0)
    attribute_value_list: list[AttributeValue] = Field(min_length=1, max_length=50)


class Logistic(Input):
    logistic_id: int = Field(gt=0)
    enabled: bool = True
    is_free: bool = False
    shipping_fee: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False, le=Decimal("1000000000000000"))
    size_id: int | None = Field(default=None, ge=0)


class TierOption(Input):
    option: str = Field(min_length=1, max_length=100)
    image_id: str | None = None


class Tier(Input):
    name: str = Field(min_length=1, max_length=100)
    options: list[TierOption] = Field(min_length=1, max_length=50)


class Model(Input):
    tier_index: list[int] = Field(min_length=1, max_length=2)
    sku: str = Field(default="", max_length=100)
    price: Decimal = Field(gt=0, allow_inf_nan=False, le=Decimal("1000000000000000"))
    stock: int = Field(default=0, ge=0, le=2147483647)
    weight: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False, le=Decimal("1000000000000000"))
    dimension: Dimension | None = None
    pre_order: PreOrder | None = None
    gtin_code: str | None = Field(default=None, pattern=r"^(00|[0-9]{8,14})$")

    @model_validator(mode="after")
    def physical(self):
        if self.dimension is not None and self.weight is None:
            raise ValueError("Berat varian wajib diisi jika dimensi varian diatur")
        return self


class PublishIn(Input):
    operation_id: UUID
    nama: str = Field(min_length=1, max_length=255)
    deskripsi: str = Field(min_length=1, max_length=12000)
    sku: str = Field(default="", max_length=100)
    category_id: int = Field(gt=0)
    price: Decimal = Field(gt=0, allow_inf_nan=False, le=Decimal("1000000000000000"))
    stock: int = Field(default=0, ge=0, le=2147483647)
    location_id: str | None = Field(default=None, max_length=64)
    weight: Decimal = Field(gt=0, allow_inf_nan=False, le=Decimal("1000000000000000"))
    dimension: Dimension
    pre_order: PreOrder = Field(default_factory=PreOrder)
    condition: Literal["NEW", "USED"] = "NEW"
    image_ids: list[str] = Field(min_length=1, max_length=9)
    attribute_list: list[Attribute] = Field(default_factory=list, max_length=100)
    logistic_info: list[Logistic] = Field(min_length=1, max_length=50)
    brand_id: int = Field(default=0, ge=0)
    brand_name: str = Field(default="No Brand", min_length=1, max_length=255)
    gtin_code: str | None = Field(default=None, pattern=r"^(00|[0-9]{8,14})$")
    tiers: list[Tier] = Field(default_factory=list, max_length=2)
    models: list[Model] = Field(default_factory=list, max_length=50)
    size_chart: str | None = Field(default=None, min_length=1, max_length=255)
    size_chart_id: int | None = Field(default=None, gt=0)
    item_dangerous: Literal[0, 1] = 0
    aktif: bool = False

    @model_validator(mode="after")
    def variants(self):
        if (
            not self.nama.strip()
            or not self.deskripsi.strip()
            or not any(logistic.enabled for logistic in self.logistic_info)
        ):
            raise ValueError("Nama, deskripsi, dan minimal satu jasa kirim wajib diisi")
        if len(set(self.image_ids)) != len(self.image_ids) or any(not i.strip() for i in self.image_ids):
            raise ValueError("ID foto tidak boleh kosong atau duplikat")
        if len({a.attribute_id for a in self.attribute_list}) != len(self.attribute_list):
            raise ValueError("Atribut tidak boleh duplikat")
        for tier in self.tiers:
            values = [o.option.strip().casefold() for o in tier.options]
            if not tier.name.strip() or any(not v for v in values) or len(set(values)) != len(values):
                raise ValueError("Nama tier/pilihan tidak boleh kosong atau duplikat")
        expected = set(product(*[range(len(t.options)) for t in self.tiers])) if self.tiers else set()
        actual = [tuple(m.tier_index) for m in self.models]
        if len(expected) > 50 or set(actual) != expected or len(set(actual)) != len(actual):
            raise ValueError("Semua kombinasi varian harus terpetakan tepat satu kali; maksimal 50 model")
        return self


class Evidence(Input):
    module_index: int = Field(ge=0)
    urls: list[str] = Field(default_factory=list, max_length=9)


class DisputeIn(Input):
    email: EmailStr
    reason_id: int = Field(gt=0)
    text: str = Field(default="", max_length=2000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=20)
