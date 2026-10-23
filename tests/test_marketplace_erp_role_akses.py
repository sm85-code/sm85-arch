from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1.arus_kas_router import router as arus_kas
from tenants.marketplace_erp.adapters.api.v1.operasional_router import router as operasional
from tenants.marketplace_erp.adapters.api.v1.roles_router import router as roles
from tenants.marketplace_erp.modules.marketplace_erp.application import akses
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import (
    akun_ids_diizinkan,
    get_current_user_marketplace_erp,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import (
    MarketplaceErpBase,
    get_db_marketplace_erp,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import UserMarketplaceErp


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


def client_for(role, session):
    app = FastAPI()
    for r in (roles, operasional, arus_kas):
        app.include_router(r)

    async def db():
        yield session

    async def current():
        return SimpleNamespace(id="user", role=role)

    app.dependency_overrides[get_db_marketplace_erp] = db
    app.dependency_overrides[get_current_user_marketplace_erp] = current
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def body(nama="Staf Gudang", akses_toko="semua", halaman=None, keterangan=""):
    return {"nama": nama, "keterangan": keterangan, "akses_toko": akses_toko, "halaman": halaman or {}}


ENTRI = {"kategori": "ongkir", "tanggal": "2026-10-05", "jumlah": "1000", "uraian": "x"}


@pytest.mark.asyncio
async def test_list_has_builtin_roles_and_the_pages(session):
    async with client_for("admin", session) as c:
        data = (await c.get("/roles")).json()
    by_id = {r["id"]: r for r in data["roles"]}
    assert {"admin", "owner", "staff", "staf_keuangan", "staf_operasional", "staf_produksi"} <= set(by_id)
    assert by_id["admin"]["terkunci"] is True
    assert by_id["admin"]["halaman"] == {k: lv[-1] for k, lv in akses.TINGKAT_HALAMAN.items()}  # the highest level of every page
    assert by_id["staf_keuangan"]["halaman"] == {"operasional_umum": "tulis", "operasional_packing": "tulis", "arus_kas": "tulis", "belanja_supplier": "tulis"}
    assert by_id["owner"]["halaman"] == {**akses._OWNER, "belanja_supplier": "baca"}  # given with Operasional Umum
    assert by_id["staff"]["halaman"] == akses._STAFF
    assert by_id["staff"]["akses_toko"] == "ditugaskan" and by_id["owner"]["akses_toko"] == "semua"
    pages = {h["id"]: h for h in data["halaman"]}
    assert {"pesanan", "produk", "iklan", "operasional_umum", "arus_kas", "kelola_pengguna"} <= set(pages)
    assert pages["dashboard"]["tingkat"] == ["baca"] and pages["sinkron_shopee"]["tingkat"] == ["tulis"]
    assert pages["pesanan"]["tingkat"] == ["baca", "tulis"] and pages["pesanan"]["grup"] == "Harian"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "staff", "staf_keuangan", "tidak_ada"])
async def test_only_admin_manages_roles(session, role):
    async with client_for(role, session) as c:
        assert (await c.get("/roles")).status_code == 403
        assert (await c.post("/roles", json=body())).status_code == 403
        assert (await c.put("/roles/owner", json=body("Owner"))).status_code == 403
        assert (await c.delete("/roles/staf_produksi")).status_code == 403


@pytest.mark.asyncio
async def test_create_role_and_it_takes_effect(session):
    async with client_for("admin", session) as c:
        made = await c.post("/roles", json=body("Staf Gudang", halaman={"operasional_packing": "baca"}))
        assert made.status_code == 201 and made.json() == {"id": "staf_gudang"}
        assert (await c.post("/roles", json=body("staf gudang"))).status_code == 409  # same name
    async with client_for("staf_gudang", session) as c:
        assert (await c.get("/operasional/packing?periode=2026-10")).status_code == 200
        assert (await c.post("/operasional/packing", json={**ENTRI, "kategori": "perl_packing"})).status_code == 403
        assert (await c.get("/operasional/umum?periode=2026-10")).status_code == 403


@pytest.mark.asyncio
async def test_validation(session):
    async with client_for("admin", session) as c:
        assert (await c.post("/roles", json=body("A"))).status_code == 422
        assert (await c.post("/roles", json=body("Baru", akses_toko="sebagian"))).status_code == 422
        assert (await c.post("/roles", json=body("Baru", halaman={"halaman_palsu": "tulis"}))).status_code == 422
        assert (await c.post("/roles", json=body("Baru", halaman={"arus_kas": "hapus"}))).status_code == 422
        assert (await c.post("/roles", json=body("Baru", halaman={"dashboard": "tulis"}))).status_code == 422  # only has "baca"
        assert (await c.post("/roles", json=body("Baru", halaman={"sinkron_shopee": "baca"}))).status_code == 422  # only has "tulis"
        assert (await c.post("/roles", json=body("Baru", halaman={"arus_kas": "tidak"}))).status_code == 201


@pytest.mark.asyncio
async def test_admin_can_edit_what_owner_may_do(session):
    async with client_for("owner", session) as c:
        assert (await c.get("/arus-kas/rekening-tukang")).status_code == 403
        assert (await c.post("/operasional/umum", json=ENTRI)).status_code == 403
    async with client_for("admin", session) as c:
        edit = body("Owner", halaman={"arus_kas": "baca", "operasional_umum": "tulis"})
        assert (await c.put("/roles/owner", json=edit)).status_code == 200
    async with client_for("owner", session) as c:
        assert (await c.get("/arus-kas/lembar?periode=2026-10")).status_code == 200
        # reading is not ticking: saving the sheet needs write access
        assert (await c.put("/arus-kas/lembar?periode=2026-10", json={"version": 0, "lembar": {"sel": {}, "baris": [], "hapus": []}})).status_code == 403
        assert (await c.post("/operasional/umum", json=ENTRI)).status_code == 201
        assert (await c.get("/operasional/packing?periode=2026-10")).status_code == 403  # removed from owner
    async with client_for("admin", session) as c:
        owner = next(r for r in (await c.get("/roles")).json()["roles"] if r["id"] == "owner")
        assert owner["nama"] == "Owner" and owner["sistem"] is True  # a built-in role keeps its name
        assert owner["halaman"] == {"arus_kas": "baca", "operasional_umum": "tulis"}


@pytest.mark.asyncio
async def test_admin_role_is_locked_and_built_ins_cannot_be_deleted(session):
    async with client_for("admin", session) as c:
        assert (await c.put("/roles/admin", json=body("Admin"))).status_code == 409
        for rid in ("admin", "owner", "staff"):
            assert (await c.delete(f"/roles/{rid}")).status_code == 409
        assert (await c.put("/roles/tidak_ada", json=body("X"))).status_code == 404
        assert (await c.delete("/roles/tidak_ada")).status_code == 404


@pytest.mark.asyncio
async def test_role_in_use_cannot_be_deleted(session):
    session.add(UserMarketplaceErp(id="u1", nama="Sari", username="sari", password_hash="x", role="staf_produksi"))
    await session.commit()
    async with client_for("admin", session) as c:
        assert (await c.delete("/roles/staf_produksi")).status_code == 409
        assert (await c.delete("/roles/staf_operasional")).status_code == 204
        assert "staf_operasional" not in {r["id"] for r in (await c.get("/roles")).json()["roles"]}


@pytest.mark.asyncio
async def test_shop_access_setting_and_role_must_exist(session):
    user = SimpleNamespace(id="u", role="staf_gudang")
    async with client_for("admin", session) as c:
        await c.post("/roles", json=body("Staf Gudang", akses_toko="ditugaskan"))
        assert await akun_ids_diizinkan(user, session) == []
        await c.put("/roles/staf_gudang", json=body("Staf Gudang", akses_toko="semua"))
    assert await akun_ids_diizinkan(user, session) is None
    await akses.pastikan_role_ada(session, "staf_gudang")
    with pytest.raises(HTTPException) as err:
        await akses.pastikan_role_ada(session, "tidak_ada")
    assert err.value.status_code == 422


@pytest.mark.asyncio
async def test_builtins_are_not_overwritten_once_edited(session):
    async with client_for("admin", session) as c:
        await c.put("/roles/staf_keuangan", json=body("Staf Keuangan", halaman={"arus_kas": "baca"}))
    await akses.pastikan_bawaan(session)  # what startup does again after every deploy
    rows = (await session.execute(select(akses.RoleHalaman).where(akses.RoleHalaman.role_id == "staf_keuangan"))).scalars().all()
    assert {(r.halaman, r.tingkat) for r in rows} == {("arus_kas", "baca")}


@pytest.mark.asyncio
async def test_a_deleted_starter_role_stays_deleted(session):
    async with client_for("admin", session) as c:
        assert (await c.delete("/roles/staf_produksi")).status_code == 204
    await akses.pastikan_bawaan(session)
    assert await session.get(akses.Role, "staf_produksi") is None
    assert await session.get(akses.Role, "owner") is not None


@pytest.mark.asyncio
async def test_akses_pengguna_reports_what_the_app_needs(session):
    assert (await akses.akses_pengguna(session, "admin"))["halaman"]["kelola_pengguna"] == "tulis"
    assert (await akses.akses_pengguna(session, "admin"))["halaman"]["dashboard"] == "baca"
    staff = await akses.akses_pengguna(session, "staff")
    assert staff["akses_toko"] == "ditugaskan" and staff["halaman"]["pesanan"] == "tulis" and "produk" not in staff["halaman"]
    gone = await akses.akses_pengguna(session, "tidak_ada")
    assert gone["halaman"] == {} and gone["akses_toko"] == "ditugaskan"
