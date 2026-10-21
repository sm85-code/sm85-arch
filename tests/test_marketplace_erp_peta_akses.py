"""The role settings must give admin, owner and staff exactly what the old fixed rules gave them.

tests/fixtures/marketplace_erp_rute_lama.json is a snapshot (taken when roles became editable) of every endpoint
that was guarded by a fixed list of roles, with that list. Each one is called through the real guard.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI

from tenants.marketplace_erp.modules.marketplace_erp.application import akses, peta_akses
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import (
    get_current_user_marketplace_erp,
    require_roles_marketplace_erp,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp

SNAPSHOT = json.loads((Path(__file__).parent / "fixtures" / "marketplace_erp_rute_lama.json").read_text())
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


def app_for(role, session, *fixed):
    app = FastAPI()

    @app.api_route("/{path:path}", methods=METHODS, dependencies=[Depends(require_roles_marketplace_erp(*fixed))])
    async def anything(path: str):
        return {"ok": True}

    async def db():
        yield session

    async def current():
        return SimpleNamespace(id="u", role=role)

    app.dependency_overrides[get_db_marketplace_erp] = db
    app.dependency_overrides[get_current_user_marketplace_erp] = current
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_snapshot_is_complete_and_every_route_has_a_page():
    assert len(SNAPSHOT) > 200
    assert [s for s in SNAPSHOT if peta_akses.cari(s["path"], s["method"]) is None] == []


def test_every_page_in_the_rules_is_in_the_registry():
    halaman = {h for _, _, h in (a for a in peta_akses.ATURAN)} - {"*"}
    assert halaman <= set(akses.TINGKAT_HALAMAN)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "owner", "staff"])
async def test_builtin_roles_keep_exactly_their_old_access(sesi_peran, role):
    async with app_for(role, sesi_peran, "admin") as c:  # the fixed list is ignored for every mapped route
        wrong = []
        for s in SNAPSHOT:
            got = (await c.request(s["method"], s["path"])).status_code == 200
            if got != (role in s["legacy"]):
                wrong.append((s["method"], s["path"], "allowed" if got else "denied"))
    assert wrong == []


@pytest.mark.asyncio
async def test_unmapped_routes_keep_their_fixed_list(sesi_peran):
    async with app_for("owner", sesi_peran, "admin") as c:
        assert (await c.get("/belum-ada-aturan")).status_code == 403
    async with app_for("owner", sesi_peran, "admin", "owner") as c:
        assert (await c.get("/belum-ada-aturan")).status_code == 200
    async with app_for("tidak_ada", sesi_peran, "admin", "owner") as c:
        assert (await c.get("/belum-ada-aturan")).status_code == 403


@pytest.mark.asyncio
async def test_unticking_and_ticking_changes_what_a_role_reaches(sesi_peran):
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import RoleHalaman

    async with app_for("owner", sesi_peran, "admin") as c:
        assert (await c.get("/iklan")).status_code == 403
        assert (await c.post("/pesanan/ID/status")).status_code == 200
        sesi_peran.add(RoleHalaman(role_id="owner", halaman="iklan", tingkat="baca"))
        row = await sesi_peran.get(RoleHalaman, ("owner", "pesanan"))
        row.tingkat = "baca"
        await sesi_peran.commit()
        assert (await c.get("/iklan")).status_code == 200  # ticked: may look
        assert (await c.post("/iklan/ID/aksi")).status_code == 403  # but not change
        assert (await c.get("/pesanan/ID")).status_code == 200  # still reads orders
        assert (await c.post("/pesanan/ID/status")).status_code == 403  # no longer changes them


@pytest.mark.asyncio
async def test_a_role_with_no_ticks_still_gets_the_shop_list_but_nothing_else(sesi_peran):
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import Role

    sesi_peran.add(Role(id="kosong", nama="Kosong", akses_toko="semua"))
    await sesi_peran.commit()
    async with app_for("kosong", sesi_peran, "admin") as c:
        assert (await c.get("/akun")).status_code == 200  # any existing role
        assert (await c.get("/pesanan")).status_code == 403
        assert (await c.post("/akun")).status_code == 403
