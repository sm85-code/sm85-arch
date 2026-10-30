"""Processing reference data (what was kept from the old spreadsheet, plus the parties added since) and every descendant."""

import json

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import ProcessingCatalog

from .processing_permissions import processing_access

router = APIRouter(prefix="/processing", tags=["Processing"])


@router.get("/data", dependencies=[Depends(processing_access)])
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


# Descendants enforce their mapped page; job and price writes also check their exact tab/kind.
from .processing_workflow import router as workflow_router  # noqa: E402

router.include_router(workflow_router)

from .processing_supplier_catalog import router as supplier_catalog_router  # noqa: E402

router.include_router(supplier_catalog_router)

from .processing_templates import router as templates_router  # noqa: E402

router.include_router(templates_router)
