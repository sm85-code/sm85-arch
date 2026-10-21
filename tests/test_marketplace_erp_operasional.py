from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1.operasional_router import router
from tenants.marketplace_erp.modules.marketplace_erp.application import akses
from tenants.marketplace_erp.modules.marketplace_erp.application import operasional as ops
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import (
    akun_ids_diizinkan,
    get_current_user_marketplace_erp,
)
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


def client_for(role, session):
    app = FastAPI()
    app.include_router(router)

    async def db():
        yield session

    async def current():
        return SimpleNamespace(id=None, role=role)

    app.dependency_overrides[get_db_marketplace_erp] = db
    app.dependency_overrides[get_current_user_marketplace_erp] = current
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def isian(kategori="gaji", tanggal="2026-10-05", jumlah="1000000", uraian="Gaji admin"):
    return {"kategori": kategori, "tanggal": tanggal, "jumlah": jumlah, "uraian": uraian}


# (role, jenis) -> (may read, may write)
AKSES = {
    ("admin", "umum"): (True, True), ("admin", "packing"): (True, True),
    ("staf_keuangan", "umum"): (True, True), ("staf_keuangan", "packing"): (True, True),
    ("staf_operasional", "umum"): (True, True), ("staf_operasional", "packing"): (False, False),
    ("staf_produksi", "umum"): (False, False), ("staf_produksi", "packing"): (True, True),
    ("owner", "umum"): (True, False), ("owner", "packing"): (True, False),
    ("staff", "umum"): (False, False), ("staff", "packing"): (False, False),
    ("tidak_ada", "umum"): (False, False),  # a role that does not exist reaches nothing
}


@pytest.mark.asyncio
@pytest.mark.parametrize("role,jenis", list(AKSES))
async def test_access_matrix(session, role, jenis):
    baca, tulis = AKSES[(role, jenis)]
    kategori = "gaji" if jenis == "umum" else "perl_packing"
    async with client_for(role, session) as c:
        assert (await c.get(f"/operasional/{jenis}?periode=2026-10")).status_code == (200 if baca else 403)
        assert (await c.post(f"/operasional/{jenis}", json=isian(kategori))).status_code == (201 if tulis else 403)


@pytest.mark.asyncio
async def test_list_reports_whether_the_caller_may_write(session):
    async with client_for("owner", session) as c:
        assert (await c.get("/operasional/umum?periode=2026-10")).json()["bisa_tulis"] is False
    async with client_for("staf_operasional", session) as c:
        assert (await c.get("/operasional/umum?periode=2026-10")).json()["bisa_tulis"] is True


@pytest.mark.asyncio
async def test_totals_are_exact_and_stay_within_the_month(session):
    async with client_for("admin", session) as c:
        for tanggal, kategori, jumlah in [
            ("2026-10-01", "gaji", "0.10"), ("2026-10-31", "gaji", "0.20"),  # both ends of the month
            ("2026-09-30", "gaji", "500"), ("2026-11-01", "gaji", "700"),  # outside
            ("2026-10-15", "iklan", "1500000.50"),
        ]:
            assert (await c.post("/operasional/umum", json=isian(kategori, tanggal, jumlah))).status_code == 201
        data = (await c.get("/operasional/umum?periode=2026-10")).json()
    assert len(data["items"]) == 3
    assert Decimal(data["ringkasan"]["per_kategori"]["gaji"]) == Decimal("0.30")
    assert Decimal(data["ringkasan"]["per_kategori"]["iklan"]) == Decimal("1500000.50")
    assert Decimal(data["ringkasan"]["per_kategori"]["langganan"]) == 0
    assert Decimal(data["ringkasan"]["total"]) == Decimal("1500000.80")
    assert [i["tanggal"] for i in data["items"]] == ["2026-10-01", "2026-10-15", "2026-10-31"]


@pytest.mark.asyncio
async def test_pages_do_not_mix_and_categories_are_checked(session):
    async with client_for("admin", session) as c:
        assert (await c.post("/operasional/umum", json=isian("perl_packing"))).status_code == 422
        assert (await c.post("/operasional/packing", json=isian("gaji"))).status_code == 422
        assert (await c.post("/operasional/packing", json=isian("perl_packing"))).status_code == 201
        assert (await c.get("/operasional/umum?periode=2026-10")).json()["items"] == []
        assert (await c.get("/operasional/lain?periode=2026-10")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("jumlah", ["0", "-5", "abc", "1.234", "99999999999999"])
async def test_amount_must_be_a_positive_money_value(session, jumlah):
    async with client_for("admin", session) as c:
        assert (await c.post("/operasional/umum", json=isian(jumlah=jumlah))).status_code == 422


@pytest.mark.asyncio
async def test_bad_date_and_period_are_refused(session):
    async with client_for("admin", session) as c:
        assert (await c.post("/operasional/umum", json=isian(tanggal="1999-01-01"))).status_code == 422
        assert (await c.post("/operasional/umum", json=isian(tanggal="besok"))).status_code == 422
        assert (await c.get("/operasional/umum?periode=2026-13")).status_code == 422
        assert (await c.get("/operasional/umum?periode=oktober")).status_code == 422


@pytest.mark.asyncio
async def test_edit_and_delete(session):
    async with client_for("staf_keuangan", session) as c:
        made = (await c.post("/operasional/umum", json=isian())).json()
        edited = (await c.put(f"/operasional/umum/{made['id']}", json=isian("iklan", "2026-10-09", "250000", " Top up "))).json()
        assert edited["kategori"] == "iklan" and edited["uraian"] == "Top up" and Decimal(edited["jumlah"]) == 250000
        # an entry belongs to its page: it cannot be reached (or moved) through the other one
        assert (await c.put(f"/operasional/packing/{made['id']}", json=isian("perl_packing"))).status_code == 404
        assert (await c.delete(f"/operasional/packing/{made['id']}")).status_code == 404
        assert (await c.delete(f"/operasional/umum/{made['id']}")).status_code == 204
        assert (await c.delete(f"/operasional/umum/{made['id']}")).status_code == 404
        assert (await c.get("/operasional/umum?periode=2026-10")).json()["items"] == []


@pytest.mark.asyncio
async def test_read_only_roles_cannot_edit_or_delete(session):
    async with client_for("admin", session) as c:
        made = (await c.post("/operasional/umum", json=isian())).json()
    async with client_for("owner", session) as c:
        assert (await c.put(f"/operasional/umum/{made['id']}", json=isian())).status_code == 403
        assert (await c.delete(f"/operasional/umum/{made['id']}")).status_code == 403


@pytest.mark.asyncio
async def test_total_per_kategori_for_arus_kas(session):
    async with client_for("admin", session) as c:
        await c.post("/operasional/umum", json=isian("gaji", "2026-10-05", "100"))
        await c.post("/operasional/umum", json=isian("gaji", "2026-10-12", "50.5"))
        await c.post("/operasional/umum", json=isian("iklan", "2026-10-13", "7"))
    totals = await ops.total_per_kategori(session, "umum", date(2026, 10, 5), date(2026, 10, 12))
    assert totals == {"gaji": Decimal("150.5")}


@pytest.mark.asyncio
async def test_shop_access_follows_the_role(session):
    for role in ("admin", "owner", "staf_keuangan", "staf_operasional", "staf_produksi"):
        assert await akun_ids_diizinkan(SimpleNamespace(id="u", role=role), session) is None, role
    assert await akun_ids_diizinkan(SimpleNamespace(id="u", role="staff"), session) == []  # nobody assigned yet
    assert await akun_ids_diizinkan(SimpleNamespace(id="u", role="tidak_ada"), session) == []
