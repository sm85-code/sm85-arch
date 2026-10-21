from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1.pengaturan_ui_router import router
from tenants.marketplace_erp.modules.marketplace_erp.application import akses
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import (
    MarketplaceErpBase,
    get_db_marketplace_erp,
)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        await akses.pastikan_bawaan(s)
        await s.commit()
        yield s
    await engine.dispose()


def client_for(session):
    app = FastAPI()
    app.include_router(router)

    async def db():
        yield session

    async def current():
        return SimpleNamespace(id=None, role="admin")

    app.dependency_overrides[get_db_marketplace_erp] = db
    app.dependency_overrides[get_current_user_marketplace_erp] = current
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_ubah_dan_kembalikan_teks(session):
    async with client_for(session) as c:
        assert (await c.get("/pengaturan-ui")).json() == {"items": {}}
        r = await c.put("/pengaturan-ui/menu:/arus-kas", json={"teks": "  Arus   Kas Mingguan "})
        assert r.status_code == 200 and r.json()["teks"] == "Arus Kas Mingguan"
        assert (await c.get("/pengaturan-ui")).json()["items"] == {"menu:/arus-kas": "Arus Kas Mingguan"}
        assert (await c.delete("/pengaturan-ui/menu:/arus-kas")).status_code == 204
        assert (await c.get("/pengaturan-ui")).json() == {"items": {}}


@pytest.mark.asyncio
async def test_teks_dan_id_divalidasi(session):
    async with client_for(session) as c:
        assert (await c.put("/pengaturan-ui/x", json={"teks": "   "})).status_code == 422
        assert (await c.put("/pengaturan-ui/x", json={"teks": "a" * 121})).status_code == 422
        assert (await c.put("/pengaturan-ui/ada spasi", json={"teks": "a"})).status_code == 422


@pytest.mark.asyncio
async def test_hak_akses(session):
    assert "kelola_aplikasi" in {h[0] for h in akses.HALAMAN}
    assert (await akses.akses_pengguna(session, "admin"))["halaman"]["kelola_aplikasi"] == "tulis"
    assert (await akses.akses_pengguna(session, "staff"))["halaman"].get("kelola_aplikasi", "tidak") == "tidak"
