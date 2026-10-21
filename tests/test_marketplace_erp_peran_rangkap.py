"""One account holding several roles: access is the union of all of them."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import akses, services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import UserCreateIn, UserUpdateIn
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import akun_ids_diizinkan
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase


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


async def buat(session, role="staf_operasional", tambahan=()):
    payload = UserCreateIn(nama="Rina", username="rina", password="Sandi-Kuat-123", role=role, peran_tambahan=list(tambahan))
    return await services.create_user(session, payload)


@pytest.mark.asyncio
async def test_pages_of_every_role_are_open(session):
    user = await buat(session, tambahan=["staf_produksi"])
    roles = await akses.peran_pengguna(session, user)
    assert roles == ["staf_operasional", "staf_produksi"]
    assert await akses.tingkat_halaman(session, roles, "operasional_umum") == "tulis"
    assert await akses.tingkat_halaman(session, roles, "operasional_packing") == "tulis"
    assert await akses.tingkat_halaman(session, roles, "laporan_keuangan") is None
    assert await akses.tingkat_halaman(session, "staf_operasional", "operasional_packing") is None


@pytest.mark.asyncio
async def test_write_beats_read_and_all_shops_beats_assigned(session):
    # staff: Arus Kas write, shops assigned; owner: Operasional Umum read, all shops
    assert await akses.tingkat_halaman(session, ["owner", "staff"], "arus_kas") == "tulis"
    assert await akses.tingkat_halaman(session, ["owner", "staff"], "operasional_umum") == "baca"
    assert await akses.akses_toko(session, ["staff", "staf_keuangan"]) == "semua"
    assert await akses.akses_toko(session, ["staff"]) == "ditugaskan"


@pytest.mark.asyncio
async def test_access_summary_merges_roles(session):
    user = await buat(session, role="staff", tambahan=["staf_produksi"])
    ringkas = await akses.akses_pengguna(session, await akses.peran_pengguna(session, user))
    assert ringkas["role"] == "staff" and ringkas["roles"] == ["staff", "staf_produksi"]
    assert ringkas["nama"] == "Staff + Staf Produksi"
    assert ringkas["halaman"]["arus_kas"] == "tulis" and ringkas["halaman"]["operasional_packing"] == "tulis"
    assert ringkas["akses_toko"] == "semua"
    assert await akun_ids_diizinkan(user, session) is None


@pytest.mark.asyncio
async def test_extra_roles_are_validated(session):
    with pytest.raises(HTTPException) as e:
        await buat(session, tambahan=["tidak_ada"])
    assert e.value.status_code == 422
    with pytest.raises(HTTPException):
        await buat(session, tambahan=["admin"])


@pytest.mark.asyncio
async def test_update_replaces_extras_and_keeps_them_when_not_sent(session):
    user = await buat(session, tambahan=["staf_produksi"])
    await services.update_user(session, user.id, UserUpdateIn(nama="Rina S"))
    assert user.peran_tambahan == ["staf_produksi"]
    await services.update_user(session, user.id, UserUpdateIn(peran_tambahan=["staf_keuangan", "staf_operasional"]))
    assert user.peran_tambahan == ["staf_keuangan"]  # the main role is not repeated
    await services.update_user(session, user.id, UserUpdateIn(role="staf_produksi"))
    assert await akses.peran_pengguna(session, user) == ["staf_produksi", "staf_keuangan"]
    await services.update_user(session, user.id, UserUpdateIn(peran_tambahan=[]))
    assert await akses.peran_pengguna(session, user) == ["staf_produksi"]


@pytest.mark.asyncio
async def test_a_role_held_as_extra_cannot_be_deleted_and_is_counted(session):
    user = await buat(session, tambahan=["staf_produksi"])
    with pytest.raises(HTTPException) as e:
        await akses.hapus(session, "staf_produksi")
    assert e.value.status_code == 409
    jumlah = {r["id"]: r["jumlah_pengguna"] for r in (await akses.daftar(session))["roles"]}
    assert jumlah["staf_produksi"] == 1 and jumlah["staf_operasional"] == 1
    await services.update_user(session, user.id, UserUpdateIn(peran_tambahan=[]))
    await akses.hapus(session, "staf_produksi")


@pytest.mark.asyncio
async def test_admin_takes_no_extras(session):
    admin = SimpleNamespace(id="a1", role="admin")
    with pytest.raises(HTTPException):
        await akses.atur_peran_tambahan(session, admin, ["staf_produksi"])


@pytest.mark.asyncio
async def test_endpoints_follow_every_role(session):
    import httpx
    from fastapi import FastAPI

    from tenants.marketplace_erp.adapters.api.v1.operasional_router import router as operasional
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp

    user = await buat(session, tambahan=["staf_produksi"])
    app = FastAPI()
    app.include_router(operasional)

    async def db():
        yield session

    async def current():
        return SimpleNamespace(id=user.id, role=user.role)

    app.dependency_overrides[get_db_marketplace_erp] = db
    app.dependency_overrides[get_current_user_marketplace_erp] = current
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/operasional/umum", params={"periode": "2026-10"})).status_code == 200
        assert (await c.get("/operasional/packing", params={"periode": "2026-10"})).status_code == 200
    await services.update_user(session, user.id, UserUpdateIn(peran_tambahan=[]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/operasional/packing", params={"periode": "2026-10"})).status_code == 403
