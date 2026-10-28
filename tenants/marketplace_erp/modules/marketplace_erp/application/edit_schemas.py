"""One-screen product edit (Seller Center style): every changed part in one request."""
from decimal import Decimal

from pydantic import Field, model_validator

from .management_schemas import ItemEdit
from .workflow_schemas import Input, Model, PreOrder


class EditOption(Input):
    option: str = Field(min_length=1, max_length=100)
    # Index of this option in the current variation; None = a new option (only appended at the end).
    asal: int | None = Field(default=None, ge=0)


class EditTier(Input):
    name: str = Field(min_length=1, max_length=100)
    options: list[EditOption] = Field(min_length=1, max_length=50)


class ModelChange(Input):
    model_id: int = Field(gt=0)
    model_sku: str | None = Field(default=None, max_length=100)
    weight: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    pre_order: PreOrder | None = None
    price: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False, le=Decimal("1000000000000000"))
    stock: int | None = Field(default=None, ge=0, le=2147483647)


class ProductEdit(Input):
    item: ItemEdit | None = None
    tiers: list[EditTier] | None = Field(default=None, min_length=1, max_length=2)
    delete_models: list[int] = Field(default_factory=list, max_length=50)
    add_models: list[Model] = Field(default_factory=list, max_length=50)
    models: list[ModelChange] = Field(default_factory=list, max_length=100)
    # Products without variants: price and stock of the item itself.
    price: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False, le=Decimal("1000000000000000"))
    stock: int | None = Field(default=None, ge=0, le=2147483647)

    @model_validator(mode="after")
    def check(self):
        if not (self.item or self.tiers or self.delete_models or self.add_models or self.models or self.price is not None or self.stock is not None):
            raise ValueError("Belum ada perubahan")
        if len(set(self.delete_models)) != len(self.delete_models) or len({m.model_id for m in self.models}) != len(self.models):
            raise ValueError("Varian duplikat")
        if set(self.delete_models) & {m.model_id for m in self.models}:
            raise ValueError("Varian yang dihapus tidak bisa diubah sekaligus")
        if len({tuple(m.tier_index) for m in self.add_models}) != len(self.add_models):
            raise ValueError("Kombinasi varian baru tidak boleh duplikat")
        for tier in self.tiers or []:
            names = [o.option.strip().casefold() for o in tier.options]
            if len(set(names)) != len(names):
                raise ValueError(f"Pilihan '{tier.name}' tidak boleh kembar")
        return self
