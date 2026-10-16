"""Processing reference data and every descendant are admin-only."""

import hashlib
import json
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import require_roles_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import ProcessingCatalog, UserMarketplaceErp

admin = require_roles_marketplace_erp("admin")
router = APIRouter(prefix="/processing", tags=["Processing"], dependencies=[Depends(admin)])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Party(Strict):
    id: str = Field(min_length=1, max_length=80)
    nama: str = Field(min_length=1, max_length=200)
    catatan: str = Field(default="", max_length=2000)
    sumber: str = Field(min_length=1, max_length=200)


class ProductionPrice(Strict):
    id: str = Field(min_length=1, max_length=80)
    nomor: int = Field(ge=1)
    jenis: str = Field(max_length=200)
    produk: str = Field(min_length=1, max_length=300)
    kode: str = Field(max_length=100)
    ukuran: str = Field(max_length=100)
    sku: str = Field(min_length=1, max_length=200)
    harga: Decimal = Field(ge=0, max_digits=20, decimal_places=2)
    catatan: str = Field(default="", max_length=2000)
    sumber: str = Field(min_length=1, max_length=200)


class CatalogImport(Strict):
    source_url: str = Field(max_length=500)
    source_title: str = Field(min_length=1, max_length=200)
    read_at: datetime
    tukang: list[Party] = Field(min_length=1, max_length=1000)
    supplier: list[Party] = Field(min_length=1, max_length=1000)
    harga_produksi: list[ProductionPrice] = Field(min_length=1, max_length=2000)

    @field_validator("source_url")
    @classmethod
    def native_sheet(cls, value):
        if not value.startswith("https://docs.google.com/spreadsheets/d/"):
            raise ValueError("Sumber harus berupa tautan Google Sheets")
        return value

    @model_validator(mode="after")
    def unique_ids(self):
        for entries in (self.tukang, self.supplier, self.harga_produksi):
            if len({row.id for row in entries}) != len(entries):
                raise ValueError("ID baris sumber harus unik")
        return self


@router.get("/data")
async def catalog(session: AsyncSession = Depends(get_db_marketplace_erp)):
    from .processing_workflow import party_list

    row = await session.get(ProcessingCatalog, "global")
    tukang = await party_list(session, "tukang")
    supplier = await party_list(session, "supplier")
    if row is None:
        if not tukang and not supplier:
            return {"data": None}
        return {
            "data": {
                "source_url": "",
                "source_title": "Data internal ERP",
                "read_at": max(p["updated_at"] for p in tukang + supplier),
                "tukang": tukang,
                "supplier": supplier,
                "harga_produksi": [],
            }
        }
    data = json.loads(row.payload_json)
    data["tukang"] = tukang
    data["supplier"] = supplier
    return {"data": data, "imported_at": row.imported_at, "imported_by": row.imported_by}


@router.post("/import")
async def import_catalog(
    payload: CatalogImport,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    actor: UserMarketplaceErp = Depends(admin),
):
    serialized = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    checksum = hashlib.sha256(serialized.encode()).hexdigest()
    row = await session.get(ProcessingCatalog, "global")
    if row is not None:
        if row.checksum != checksum:
            raise HTTPException(409, "Data Processing sudah ada; impor tidak menimpa data yang tersimpan")
        return {"tersimpan": True, "sudah_ada": True}
    session.add(ProcessingCatalog(id="global", checksum=checksum, payload_json=serialized, imported_by=actor.id))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.get(ProcessingCatalog, "global")
        if existing and existing.checksum == checksum:
            return {"tersimpan": True, "sudah_ada": True}
        raise HTTPException(409, "Data Processing baru saja diimpor admin lain; muat ulang")
    return {"tersimpan": True, "sudah_ada": False}


# All descendants inherit the admin dependency above, in addition to their own guard.
from .processing_workflow import router as workflow_router  # noqa: E402

router.include_router(workflow_router)

from .processing_supplier_catalog import router as supplier_catalog_router  # noqa: E402

router.include_router(supplier_catalog_router)
