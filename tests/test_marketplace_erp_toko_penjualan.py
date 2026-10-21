from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1.toko_penjualan_router import router
from tenants.marketplace_erp.modules.marketplace_erp.application import akses, peta_akses
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


def toko(**kw):
    return {"nama": "Toko Ampel", "jenis": "internal", "platform": "webapp", "kode": "AK", "keterangan": "", **kw}


@pytest.mark.asyncio
async def test_buat_ubah_dan_nonaktifkan(session):
    async with client_for(session) as c:
        d = (await c.get("/toko-penjualan")).json()
        assert d["items"] == [] and [p["id"] for p in d["platform"]] == ["shopee", "tiktokshop", "lazada", "blibli", "webapp"]
        r = await c.post("/toko-penjualan", json=toko(nama="  Ampel   Kuning Web ", kode="ak"))
        assert r.status_code == 201 and r.json()["nama"] == "Ampel Kuning Web" and r.json()["kode"] == "AK" and r.json()["aktif"] is True
        id_ = r.json()["id"]
        r = await c.put(f"/toko-penjualan/{id_}", json={**toko(nama="Ampel Kuning Web", kode=""), "aktif": False})
        assert r.status_code == 200 and r.json()["kode"] == "" and r.json()["aktif"] is False
        assert (await c.put("/toko-penjualan/tidak-ada", json=toko())).status_code == 404


@pytest.mark.asyncio
async def test_internal_boleh_tanpa_kode_reseller_wajib(session):
    async with client_for(session) as c:
        assert (await c.post("/toko-penjualan", json=toko(kode=None))).status_code == 201
        r = await c.post("/toko-penjualan", json=toko(nama="Reseller A", jenis="reseller", kode=None))
        assert r.status_code == 422
        assert (await c.post("/toko-penjualan", json=toko(nama="Reseller A", jenis="reseller", kode="AB"))).status_code == 201


@pytest.mark.asyncio
async def test_validasi_dan_duplikat(session):
    async with client_for(session) as c:
        assert (await c.post("/toko-penjualan", json=toko(platform="tokopedia"))).status_code == 422
        assert (await c.post("/toko-penjualan", json=toko(jenis="grosir"))).status_code == 422
        assert (await c.post("/toko-penjualan", json=toko(kode="A1"))).status_code == 422
        assert (await c.post("/toko-penjualan", json=toko(nama="   "))).status_code == 422
        assert (await c.post("/toko-penjualan", json=toko())).status_code == 201
        # same code on another shop, and the same name on the same platform, are refused
        assert (await c.post("/toko-penjualan", json=toko(nama="Lain", kode="ak"))).status_code == 409
        assert (await c.post("/toko-penjualan", json=toko(nama="toko ampel", kode="ZZ"))).status_code == 409
        # the same name on another platform is a different shop
        assert (await c.post("/toko-penjualan", json=toko(platform="shopee", kode="ZZ"))).status_code == 201


@pytest.mark.asyncio
async def test_hak_akses(session):
    assert peta_akses.cari("/api/marketplace-erp/toko-penjualan", "GET")[0] == "kelola_toko"
    assert peta_akses.cari("/api/marketplace-erp/toko-penjualan/x", "PUT")[0] == "kelola_toko"
    assert (await akses.akses_pengguna(session, "staff"))["halaman"].get("kelola_toko", "tidak") == "tidak"
