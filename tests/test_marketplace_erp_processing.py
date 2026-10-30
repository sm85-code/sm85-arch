import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1.processing_router import catalog, router
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase, get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import ProcessingCatalog


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "staff"])
async def test_processing_reads_deny_builtin_roles_without_relevant_page_permissions(role, sesi_peran):
    app = FastAPI()
    app.include_router(router)

    async def current():
        return SimpleNamespace(id="other", role=role)

    async def forbidden_db():
        yield sesi_peran  # only the role tables hold anything; see tests/conftest.py

    app.dependency_overrides[get_current_user_marketplace_erp] = current
    app.dependency_overrides[get_db_marketplace_erp] = forbidden_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/processing/data")).status_code == 403
        assert (await client.post("/processing/import", json={})).status_code in (403, 404, 405)  # the old import is gone


@pytest.mark.asyncio
async def test_processing_data_keeps_what_was_imported_from_the_old_spreadsheet():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    harga = [
        {"id": f"h{i}", "nomor": i + 1, "jenis": "Kayu", "produk": "Produk contoh", "kode": "P", "ukuran": "100x20",
         "sku": "P.SAMA", "harga": amount, "catatan": "", "sumber": f"Data!A{i + 2}:H{i + 2}"}
        for i, amount in enumerate(["0", "100"])
    ]
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        assert (await catalog(session))["data"] is None
        session.add(ProcessingCatalog(id="global", checksum="x", imported_by="admin", payload_json=json.dumps({
            "source_url": "", "source_title": "Fixture", "read_at": "2026-10-15T00:00:00Z",
            "tukang": [{"id": "t1", "nama": "Tukang contoh", "sumber": "PENGELUARAN!C10"}],
            "supplier": [{"id": "s1", "nama": "Supplier contoh", "sumber": "PENGELUARAN!C25"}],
            "harga_produksi": harga,
        })))
        await session.commit()
        result = await catalog(session)
        assert [h["harga"] for h in result["data"]["harga_produksi"]] == ["0", "100"]  # a zero price stays zero
        assert result["imported_by"] == "admin" and [p["nama"] for p in result["data"]["tukang"]] == ["Tukang contoh"]
    await engine.dispose()
