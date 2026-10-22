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
    # /processing/import (the one-off import of the old spreadsheet) has been removed; the snapshot is of the old routes.
    assert [s["path"] for s in SNAPSHOT if peta_akses.cari(s["path"], s["method"]) is None] == ["/processing/processing/import"]


def test_every_page_in_the_rules_is_in_the_registry():
    halaman = {h for _, _, hal in peta_akses.ATURAN for h in hal.split("|")} - {"*"}
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


@pytest.mark.asyncio
async def test_pemetaan_order_and_each_data_produksi_menu_are_separate_pages(sesi_peran):
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import Role, RoleHalaman

    for rid, halaman in (("pemetaan", "processing_tukang"), ("tukang", "data_tukang"), ("tarif", "data_tarif_packing"), ("tpl", "data_template")):
        sesi_peran.add(Role(id=rid, nama=rid, akses_toko="semua"))
        sesi_peran.add(RoleHalaman(role_id=rid, halaman=halaman, tingkat="tulis"))
    await sesi_peran.commit()
    async with app_for("pemetaan", sesi_peran, "admin") as c:
        assert (await c.get("/processing/jobs")).status_code == 200
        assert (await c.get("/processing/prices")).status_code == 200  # reads what it needs to map
        assert (await c.get("/processing/parties/tukang")).status_code == 200
        assert (await c.put("/processing/prices/P")).status_code == 403  # but does not change it
        assert (await c.put("/processing/parties/tukang/T")).status_code == 403
        assert (await c.get("/processing/document-templates/INV")).status_code == 403
    async with app_for("tukang", sesi_peran, "admin") as c:  # one menu of Data Produksi: its own data only
        assert (await c.put("/processing/parties/tukang/T")).status_code == 200
        assert (await c.put("/processing/parties/supplier/S")).status_code == 403
        assert (await c.put("/processing/prices/P")).status_code == 403
        assert (await c.put("/processing/document-templates/INV")).status_code == 403
        assert (await c.get("/processing/prices")).status_code == 200
        assert (await c.get("/processing/jobs")).status_code == 403
    async with app_for("tarif", sesi_peran, "admin") as c:
        assert (await c.put("/processing/prices/P")).status_code == 200  # some price page; the kind is checked by the route
        assert (await c.put("/processing/paint-colors/C")).status_code == 403
        assert (await c.post("/processing/supplier-catalog/connect")).status_code == 403
    async with app_for("tpl", sesi_peran, "admin") as c:
        assert (await c.put("/processing/document-templates/INV")).status_code == 200
        assert (await c.put("/processing/prices/P")).status_code == 403


@pytest.mark.asyncio
async def test_a_job_is_changed_only_with_the_pemetaan_tab_it_belongs_to(sesi_peran):
    from fastapi import HTTPException

    from tenants.marketplace_erp.adapters.api.v1.processing_workflow import izin_tab
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import Role, RoleHalaman

    sesi_peran.add(Role(id="pack", nama="Pack", akses_toko="semua"))
    sesi_peran.add(RoleHalaman(role_id="pack", halaman="processing_packing", tingkat="tulis"))
    sesi_peran.add(RoleHalaman(role_id="pack", halaman="processing_tukang", tingkat="baca"))
    await sesi_peran.commit()
    user = SimpleNamespace(id="u", role="pack")
    await izin_tab(sesi_peran, user, "packing")
    for tab in ("tukang", "supplier", "pengecatan"):  # reading a tab is not changing it
        with pytest.raises(HTTPException) as e:
            await izin_tab(sesi_peran, user, tab)
        assert e.value.status_code == 403
    async with app_for("pack", sesi_peran, "admin") as c:
        assert (await c.get("/processing/jobs")).status_code == 200
        assert (await c.post("/processing/jobs/J/transition")).status_code == 200  # the route then checks the tab


@pytest.mark.asyncio
async def test_a_price_is_changed_only_with_the_page_of_its_kind(sesi_peran):
    from fastapi import HTTPException

    from tenants.marketplace_erp.adapters.api.v1.processing_workflow import izin_harga
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import Role, RoleHalaman

    sesi_peran.add(Role(id="tarif", nama="Tarif", akses_toko="semua"))
    sesi_peran.add(RoleHalaman(role_id="tarif", halaman="data_tarif_packing", tingkat="tulis"))
    await sesi_peran.commit()
    user = SimpleNamespace(id="u", role="tarif")
    await izin_harga(sesi_peran, user, "packing")
    for kind in ("produksi", "pengecatan", "non_kayu"):
        with pytest.raises(HTTPException) as e:
            await izin_harga(sesi_peran, user, kind)
        assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_a_role_that_had_processing_keeps_every_data_menu_once(sesi_peran):
    from sqlalchemy import select

    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import PengaturanUi, Role, RoleHalaman

    sesi_peran.add(Role(id="lama", nama="Lama", akses_toko="semua"))
    sesi_peran.add(RoleHalaman(role_id="lama", halaman="processing", tingkat="baca"))
    sesi_peran.add(Role(id="antara", nama="Antara", akses_toko="semua"))
    sesi_peran.add(RoleHalaman(role_id="antara", halaman="data_produksi", tingkat="tulis"))  # the single page of the earlier version
    await sesi_peran.execute(PengaturanUi.__table__.delete().where(PengaturanUi.id.like("migrasi:%")))
    await sesi_peran.commit()
    await akses.pastikan_bawaan(sesi_peran)
    await sesi_peran.commit()
    menu = [k for k, _, g, _ in akses.HALAMAN if g == "Data Produksi"]
    assert len(menu) == 8
    for rid, tingkat in (("lama", "baca"), ("antara", "tulis")):
        assert [(await sesi_peran.get(RoleHalaman, (rid, k))).tingkat for k in menu] == [tingkat] * 8
    assert await sesi_peran.get(RoleHalaman, ("antara", "data_produksi")) is None
    tab = [k for k, _, g, _ in akses.HALAMAN if k.startswith("processing_")]
    assert len(tab) == 4 and [(await sesi_peran.get(RoleHalaman, ("lama", k))).tingkat for k in tab] == ["baca"] * 4
    assert await sesi_peran.get(RoleHalaman, ("lama", "processing")) is None
    await sesi_peran.delete(await sesi_peran.get(RoleHalaman, ("lama", "data_tukang")))
    await sesi_peran.commit()
    await akses.pastikan_bawaan(sesi_peran)  # the admin took one away on purpose: not given back
    await sesi_peran.commit()
    assert await sesi_peran.get(RoleHalaman, ("lama", "data_tukang")) is None
    assert (await sesi_peran.scalars(select(PengaturanUi).where(PengaturanUi.id == akses.PENANDA_MENU))).first()


@pytest.mark.asyncio
async def test_a_pemetaan_tab_lists_only_the_jobs_that_belong_to_it(sesi_peran):
    from tenants.marketplace_erp.adapters.api.v1.processing_workflow import milik_tab, tab_terbaca
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import Role, RoleHalaman

    sesi_peran.add(Role(id="pack", nama="Pack", akses_toko="semua"))
    sesi_peran.add(RoleHalaman(role_id="pack", halaman="processing_packing", tingkat="baca"))
    await sesi_peran.commit()
    assert await tab_terbaca(sesi_peran, SimpleNamespace(id="u", role="pack")) == {"packing"}
    assert await tab_terbaca(sesi_peran, SimpleNamespace(id="u", role="admin")) == {"tukang", "supplier", "pengecatan", "packing"}
    polos = "Polos / Tanpa Cat"
    baru = {"route": "tukang", "color": "Putih"}
    assert milik_tab(baru, "tukang") and not milik_tab(baru, "packing") and not milik_tab(baru, "pengecatan")
    sudah_produksi = {"route": "tukang", "color": "Putih", "production_done_at": "x"}
    assert milik_tab(sudah_produksi, "pengecatan") and not milik_tab(sudah_produksi, "packing")
    assert milik_tab({"route": "tukang", "color": polos, "production_done_at": "x"}, "packing")
    assert milik_tab({"route": "supplier", "color": "", "received_at": "x"}, "packing")
