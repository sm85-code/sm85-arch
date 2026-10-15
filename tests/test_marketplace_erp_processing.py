from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1.processing_router import CatalogImport, catalog, import_catalog, router
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase, get_db_marketplace_erp


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "staff"])
async def test_processing_reads_and_import_deny_non_admin_before_database_access(role):
    app = FastAPI()
    app.include_router(router)

    async def current():
        return SimpleNamespace(id="other", role=role)

    async def forbidden_db():
        raise AssertionError("Non-admin accessed Processing data")

    app.dependency_overrides[get_current_user_marketplace_erp] = current
    app.dependency_overrides[get_db_marketplace_erp] = forbidden_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/processing/data")).status_code == 403
        assert (await client.post("/processing/import", json={})).status_code == 403


@pytest.mark.asyncio
async def test_processing_import_preserves_source_zero_and_duplicate_sku_and_never_overwrites():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    payload = CatalogImport.model_validate({
        "source_url": "https://docs.google.com/spreadsheets/d/fixture/edit",
        "source_title": "Fixture", "read_at": "2026-10-15T00:00:00Z",
        "tukang": [{"id": "t1", "nama": "Tukang contoh", "sumber": "PENGELUARAN!C10"}],
        "supplier": [{"id": "s1", "nama": "Supplier contoh", "sumber": "PENGELUARAN!C25"}],
        "harga_produksi": [
            {"id": f"h{i}", "nomor": i+1, "jenis": "Kayu", "produk": "Produk contoh", "kode": "P",
             "ukuran": "100x20", "sku": "P.SAMA", "harga": amount, "sumber": f"Data!A{i+2}:H{i+2}"}
            for i, amount in enumerate(["0", "100"])
        ],
    })
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        assert (await catalog(session))["data"] is None
        actor = SimpleNamespace(id="admin", role="admin")
        assert (await import_catalog(payload, session, actor))["sudah_ada"] is False
        assert (await import_catalog(payload, session, actor))["sudah_ada"] is True
        result = await catalog(session)
        assert len(result["data"]["harga_produksi"]) == 2
        assert result["data"]["harga_produksi"][0]["harga"] == "0"
        assert result["imported_by"] == "admin"
        payload.harga_produksi[0].catatan = "Different source revision"
        with pytest.raises(HTTPException) as error:
            await import_catalog(payload, session, actor)
        assert error.value.status_code == 409
        assert (await catalog(session))["data"]["harga_produksi"][0]["catatan"] == ""
    await engine.dispose()
