"""Exercise the internal workflow, permissions, isolation and price snapshots through HTTP."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from tenants.marketplace_erp.adapters.api.v1 import processing_workflow
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1.processing_router import router
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import (
    MarketplaceErpBase,
    get_db_marketplace_erp,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    AkunMarketplace,
    ItemPesanan,
    Pesanan,
    ProcessingCatalog,
)


@pytest_asyncio.fixture
async def client():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with factory() as session:
        session.add(
            ProcessingCatalog(
                id="global",
                checksum="original",
                imported_by="admin",
                payload_json=json.dumps(
                    {
                        "tukang": [{"id": "t1", "nama": "Tukang 1"}, {"id": "t2", "nama": "Tukang 2"}],
                        "supplier": [{"id": "s1", "nama": "Supplier 1"}],
                        "harga_produksi": [
                            {"id": "p1", "produk": "Rak", "harga": "150000", "sku": "PROD.RAK", "ukuran": "100"}
                        ],
                    }
                ),
            )
        )
        shop = AkunMarketplace(platform="shopee", nama_toko="Toko contoh")
        session.add(shop)
        await session.flush()
        order = Pesanan(
            id="order1",
            platform="shopee",
            id_eksternal="SN1",
            akun_id=shop.id,
            status="to_ship",
            status_marketplace="PROCESSED",
            total=500000,
            detail_json='{"ship_by_date":1792238400}',
        )
        session.add(order)
        await session.flush()
        session.add_all(
            [
                ItemPesanan(
                    id=f"i{i}",
                    pesanan_id=order.id,
                    nama_produk=f"Produk {i}",
                    qty=i,
                    harga_satuan=100000,
                    subtotal=100000 * i,
                )
                for i in [1, 2, 3]
            ]
        )
        await session.commit()
    app = FastAPI()
    app.include_router(router)

    async def db():
        async with factory() as session:
            yield session

    async def user():
        return SimpleNamespace(id="admin", role="admin")

    app.dependency_overrides[get_db_marketplace_erp] = db
    app.dependency_overrides[get_current_user_marketplace_erp] = user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http, app, factory
    await engine.dispose()


async def rows(http):
    result = await http.get("/processing/jobs")
    assert result.status_code == 200, result.text
    return result.json()["items"]


async def save(http, job, **kwargs):
    payload = dict(
        version=job["version"], party_id="t1", price_id="p1", color="Putih", packing_type="biasa", packing_price="5000"
    )
    payload.update(kwargs)
    response = await http.put("/processing/jobs/" + job["id"], json=payload)
    assert response.status_code == 200, response.text
    return next(r for r in await rows(http) if r["id"] == job["id"])


async def advance(http, job, stage, reopen=False):
    response = await http.post(
        "/processing/jobs/" + job["id"] + "/transition",
        json={"version": job["version"], "stage": stage, "reopen": reopen},
    )
    assert response.status_code == 200, response.text
    return next(r for r in await rows(http) if r["id"] == job["id"])


@pytest.mark.asyncio
async def test_reseller_multiple_products_price_history_fees_and_stage_times(client):
    http, _, factory = client
    response = await http.post(
        "/processing/jobs/manual",
        json={
            "customer": "Reseller A",
            "ordered_at": "2026-10-16T09:00:00+07:00",
            "deadline": "2026-10-20T17:00:00+07:00",
            "items": [{"produk": "Rak", "qty": 2}, {"produk": "Meja custom", "qty": 1}],
        },
    )
    assert response.status_code == 200, response.text
    jobs = await rows(http)
    first = next(r for r in jobs if r["produk"] == "Rak")
    first = await save(http, first, paint_price="40000")
    assert first["production_total"] == "300000" and first["process_fee"] == "20000"
    # Painted wood for a reseller: ordinary packing is inside the paint price, so one product price plus the process fee.
    assert first["packing_termasuk_cat"] and first["packing_total"] == "0"
    assert first["harga_produk_total"] == "300000" and first["cat_packing_total"] == "80000"
    assert first["packing_kayu_total"] == "0" and first["process_fee"] == "20000"
    assert first["reseller_total"] == "400000"
    kayu = await save(http, first, paint_price="40000", packing_type="kayu")
    assert not kayu["packing_termasuk_cat"] and kayu["packing_kayu_total"] == "10000" and kayu["cat_packing_total"] == "80000"
    assert kayu["reseller_total"] == "410000"
    first = await save(http, kayu, paint_price="40000")
    second = await save(
        http,
        next(r for r in jobs if r["produk"] == "Meja custom"),
        party_id="t2",
        price_id="",
        production_price="200000",
        color="Polos / Tanpa Cat",
    )
    assert second["party_name"] == "Tukang 2" and second["paint_total"] == "0"
    # Editing the list must not silently reprice an existing order even if its mapping is saved again.
    response = await http.put(
        "/processing/prices/p1", json={"version": 0, "kind": "produksi", "produk": "Rak", "harga": "175000"}
    )
    assert response.status_code == 200, response.text
    first = await save(http, first, paint_price="40000")
    assert first["production_price"] == "150000"
    before = dict(first)
    first = await advance(http, first, "produksi")
    assert first["stage"] == "pengecatan" and first["production_done_at"]
    # A repeated click using the old version cannot enqueue or finish it twice.
    response = await http.post(
        "/processing/jobs/" + first["id"] + "/transition", json={"version": before["version"], "stage": "produksi"}
    )
    assert response.status_code == 409
    first = await advance(http, first, "pengecatan")
    assert first["stage"] == "packing" and first["painting_done_at"]
    response = await http.post(
        "/processing/jobs/" + first["id"] + "/transition",
        json={"version": first["version"], "stage": "produksi", "reopen": True},
    )
    assert response.status_code == 409  # Must undo completed downstream work first.
    first = await advance(http, first, "packing")
    assert first["stage"] == "siap_kirim" and first["packing_done_at"]
    first = await advance(http, first, "packing", True)
    assert first["stage"] == "packing" and first["packing_done_at"] is None
    assert any(e["action"] == "selesai_packing" for e in first["events"])
    second = await advance(http, second, "produksi")
    assert second["stage"] == "packing" and second["painting_done_at"] is None
    # Packing done by a supplier (any one) is paid to that supplier; by default it stays internal.
    assert first["packing_by"] == "internal"
    response = await http.put(
        "/processing/jobs/" + first["id"] + "/packing",
        json={"version": first["version"], "packing_type": "kayu", "packing_price": "7000", "packing_by": "supplier"},
    )
    assert response.status_code == 422  # a supplier must be named
    response = await http.put(
        "/processing/jobs/" + first["id"] + "/packing",
        json={"version": first["version"], "packing_type": "kayu", "packing_price": "7000", "packing_by": "supplier",
              "packing_party_id": "s1"},
    )
    assert response.status_code == 200, response.text
    first = next(r for r in await rows(http) if r["id"] == first["id"])
    assert first["packing_party_name"] == "Supplier 1" and first["packing_total"] == "14000"
    first = await advance(http, first, "packing")
    # Arus Kas gets the cost of the finished stages as a reference only (the reopened packing counts for nothing).
    hari = datetime.now(ZoneInfo("Asia/Jakarta")).date()
    async with factory() as session:
        biaya = await processing_workflow.biaya_produksi(session, hari, hari)
    jumlah = {(i["tahap"], i["pihak"]): i["jumlah"] for i in biaya["pembayaran"]}
    # A craftsman is paid the product price only (paint and own packing are nobody's bill); a packing supplier is paid too.
    assert jumlah == {("produksi", "Tukang 1"): "300000", ("produksi", "Tukang 2"): "200000", ("packing", "Supplier 1"): "14000"}
    assert [(t["reseller"], t["harga_produk"], t["cat_packing"], t["biaya_proses"], t["packing_kayu"]) for t in biaya["tagihan"]] == [
        ("Reseller A", "300000", "80000", "20000", "14000")
    ]
    async with factory() as session:
        assert await processing_workflow.biaya_produksi(session, hari.replace(year=hari.year - 1), hari.replace(year=hari.year - 1)) == {"pembayaran": [], "tagihan": []}
    async with factory() as session:
        assert (
            json.loads((await session.get(ProcessingCatalog, "global")).payload_json)["harga_produksi"][0]["harga"]
            == "150000"
        )
        assert len((await session.scalars(select(Pesanan))).all()) == 1  # Manual stays out of the OMS.


@pytest.mark.asyncio
async def test_item_selection_supplier_packing_cancelled_order_and_isolation(client):
    http, _, factory = client
    result = await http.get("/processing/candidates")
    assert result.json()["total"] == 3
    assert (
        await http.post("/processing/jobs/select", json={"route": "supplier", "item_ids": ["i1"]})
    ).status_code == 200
    assert (await http.post("/processing/jobs/select", json={"route": "tukang", "item_ids": ["i1"]})).status_code == 409
    assert (await http.get("/processing/candidates")).json()["total"] == 2
    job = await save(
        http,
        (await rows(http))[0],
        party_id="s1",
        price_id="",
        production_price="12345",
        paint_price="999",
        color="Putih",
    )
    assert job["packing_type"] == "biasa" and job["paint_price"] == "0" and job["process_fee"] == "0"
    job = await advance(http, job, "supplier")
    assert job["stage"] == "packing" and job["received_at"] and not job["painting_done_at"]
    response = await http.put(
        "/processing/jobs/" + job["id"] + "/packing",
        json={"version": job["version"], "packing_type": "kayu", "packing_price": "25000"},
    )
    assert response.status_code == 200, response.text
    job = (await rows(http))[0]
    assert job["packing_type"] == "kayu"
    async with factory() as session:
        order = await session.get(Pesanan, "order1")
        assert order.status == "to_ship" and order.status_marketplace == "PROCESSED"
        order.status = "cancelled"
        await session.commit()
    job = (await rows(http))[0]
    assert job["order_cancelled"]
    response = await http.post(
        "/processing/jobs/" + job["id"] + "/transition", json={"version": job["version"], "stage": "packing"}
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_candidate_status_filters_and_releasing_a_mapping_at_any_stage(client):
    http, _, factory = client
    count = lambda status: http.get(f"/processing/candidates?status={status}")  # noqa: E731
    assert [(await count(s)).json()["total"] for s in ("menunggu", "dikirim", "selesai", "semua", "dipetakan")] == [3, 0, 0, 3, 0]
    assert (await http.get("/processing/candidates")).json()["items"][0]["status"] == "to_ship"
    assert (await http.post("/processing/jobs/select", json={"route": "tukang", "item_ids": ["i1", "i2"]})).status_code == 200
    rekap = (await http.get("/processing/candidates/rekap")).json()
    assert (rekap["belum"], rekap["sudah"]) == (1, 2) and sum(r["belum"] + r["sudah"] for r in rekap["items"]) == 3
    mapped = (await count("dipetakan")).json()
    assert mapped["total"] == 2 and all(i["job"]["route"] == "tukang" and i["job"]["stage"] == "draft" for i in mapped["items"])
    assert (await count("menunggu")).json()["total"] == 1
    job = await save(http, (await rows(http))[0], party_id="t1", price_id="p1")
    job = await advance(http, job, "produksi")
    async with factory() as session:
        order = await session.get(Pesanan, "order1")
        order.status, order.status_marketplace = "cancelled", "CANCELLED"
        await session.commit()
    cancelled = (await count("dipetakan")).json()
    assert cancelled["total"] == 2 and all(i["job"]["cancelled"] for i in cancelled["items"])
    assert (await http.post(f"/processing/jobs/{job['id']}/release?version=1")).status_code == 409  # stale version
    fresh = next(r for r in await rows(http) if r["id"] == job["id"])
    assert (await http.post(f"/processing/jobs/{job['id']}/release?version={fresh['version']}")).status_code == 200
    assert len(await rows(http)) == 1
    async with factory() as session:
        order = await session.get(Pesanan, "order1")
        order.status, order.status_marketplace = "shipped", "SHIPPED"
        await session.commit()
    assert [(await count(s)).json()["total"] for s in ("menunggu", "dikirim", "dipetakan")] == [0, 2, 1]
    manual = await http.post("/processing/jobs/manual", json={"source": "internal", "order_code": "INT-1", "route": "tukang", "customer": "X", "ordered_at": "2026-10-01T00:00:00Z", "deadline": "2026-10-09T00:00:00Z", "items": [{"produk": "Rak", "qty": 1}]})
    assert manual.status_code == 200, manual.text
    manual_job = next(r for r in await rows(http) if r["source"] == "internal")
    assert (await http.post(f"/processing/jobs/{manual_job['id']}/release?version={manual_job['version']}")).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "staff"])
async def test_all_workflow_endpoints_are_admin_only(client, role, sesi_peran):
    http, app, _ = client

    async def user():
        return SimpleNamespace(id="other", role=role)

    async def forbidden_db():
        yield sesi_peran  # only the role tables hold anything; see tests/conftest.py

    app.dependency_overrides[get_current_user_marketplace_erp] = user
    app.dependency_overrides[get_db_marketplace_erp] = forbidden_db
    for method, path in [
        ("GET", "/prices"),
        ("POST", "/prices/bulk-structure"),
        ("DELETE", "/prices/p1?version=0"),
        ("POST", "/prices/p1/activate?version=1"),
        ("PUT", "/parties/tukang/t1"),
        ("PUT", "/prices/p1"),
        ("GET", "/jobs"),
        ("GET", "/candidates"),
        ("GET", "/candidates/rekap"),
        ("POST", "/jobs/select"),
        ("POST", "/jobs/manual"),
        ("GET", "/production-structure"),
        ("PUT", "/production-structure/node"),
        ("GET", "/supplier-catalog/connection"),
        ("POST", "/supplier-catalog/connect"),
        ("GET", "/supplier-catalog"),
        ("DELETE", "/supplier-catalog/connection"),
        ("PUT", "/jobs/job1"),
        ("POST", "/jobs/job1/transition"),
        ("DELETE", "/jobs/job1?version=1"),
        ("POST", "/jobs/job1/release?version=1"),
        ("POST", "/jobs/job1/reset?version=1"),
        ("PUT", "/jobs/job1/packing"),
    ]:
        response = await http.request(method, "/processing" + path, json={} if method in ["PUT", "POST"] else None)
        assert response.status_code == 403, (method, path, response.text)


@pytest.mark.asyncio
async def test_directory_edit_create_versions_and_mapping_use_saved_party(client):
    http, _, factory = client
    response = await http.put(
        "/processing/parties/tukang/t1", json={"version": 0, "nama": "Tukang Diperbarui", "catatan": "Catatan baru"}
    )
    assert response.status_code == 200, response.text
    response = await http.put(
        "/processing/parties/supplier/new", json={"version": 0, "nama": "Supplier Baru", "catatan": ""}
    )
    assert response.status_code == 200, response.text
    data = (await http.get("/processing/data")).json()["data"]
    assert next(p for p in data["tukang"] if p["id"] == "t1")["nama"] == "Tukang Diperbarui"
    assert any(p["id"] == "new" for p in data["supplier"])
    assert (await http.put("/processing/parties/tukang/t1", json={"version": 0, "nama": "Stale"})).status_code == 409
    assert (
        await http.put("/processing/parties/supplier/duplicate", json={"version": 0, "nama": "supplier baru"})
    ).status_code == 409
    await http.post("/processing/jobs/select", json={"route": "tukang", "item_ids": ["i1"]})
    job = await save(http, (await rows(http))[0])
    assert job["party_name"] == "Tukang Diperbarui"
    async with factory() as session:
        original = json.loads((await session.get(ProcessingCatalog, "global")).payload_json)
        assert original["tukang"][0]["nama"] == "Tukang 1"


@pytest.mark.asyncio
async def test_price_category_is_editable_and_omitted_category_preserves_existing(client):
    http, _, _ = client
    response = await http.put(
        "/processing/prices/p1",
        json={"version": 0, "kind": "produksi", "produk": "Rak", "harga": "150000", "jenis": "Rak Dinding"},
    )
    assert response.status_code == 200, response.text
    price = next(p for p in (await http.get("/processing/prices")).json()["items"] if p["id"] == "p1")
    assert price["jenis"] == "Rak Dinding"
    # Older clients do not send category; their price edits must not erase it.
    response = await http.put(
        "/processing/prices/p1",
        json={"version": price["version"], "kind": "produksi", "produk": "Rak", "harga": "160000"},
    )
    assert response.status_code == 200, response.text
    price = next(p for p in (await http.get("/processing/prices")).json()["items"] if p["id"] == "p1")
    assert price["jenis"] == "Rak Dinding" and price["harga"] == "160000"


@pytest.mark.asyncio
async def test_paint_color_saved_and_legacy_edits_preserve_it(client):
    http, _, _ = client
    payload = {"version": 0, "kind": "pengecatan", "produk": "Rak", "harga": "30000", "warna": "Putih"}
    response = await http.put("/processing/prices/cat-putih", json=payload)
    assert response.status_code == 200, response.text
    price = next(p for p in (await http.get("/processing/prices")).json()["items"] if p["id"] == "cat-putih")
    assert price["warna"] == "Putih"
    payload.pop("warna")
    payload.update(version=price["version"], harga="35000")
    assert (await http.put("/processing/prices/cat-putih", json=payload)).status_code == 200
    price = next(p for p in (await http.get("/processing/prices")).json()["items"] if p["id"] == "cat-putih")
    assert price["warna"] == "Putih" and price["harga"] == "35000"
    payload.update(version=price["version"], warna=" ")
    assert (await http.put("/processing/prices/cat-putih", json=payload)).status_code == 422
    response = await http.post(
        "/processing/jobs/manual",
        json={
            "customer": "Reseller",
            "ordered_at": "2026-10-16T09:00:00+07:00",
            "deadline": "2026-10-20T17:00:00+07:00",
            "items": [{"produk": "Rak", "qty": 1}],
        },
    )
    assert response.status_code == 200, response.text
    job = await save(http, (await rows(http))[0], paint_price_id="cat-putih")
    assert job["color"] == "Putih" and job["paint_price"] == "35000"
    assert job["painting_reference"]["warna"] == "Putih"
    # Catalog edits must not change the color/price snapshot of an existing order.
    payload.update(version=price["version"], warna="Walnut", harga="50000")
    assert (await http.put("/processing/prices/cat-putih", json=payload)).status_code == 200
    job = await save(http, job, paint_price_id="cat-putih")
    assert job["paint_price"] == "35000" and job["painting_reference"]["warna"] == "Putih"
    response = await http.put(
        "/processing/jobs/" + job["id"],
        json={
            "version": job["version"],
            "party_id": "t1",
            "price_id": "p1",
            "color": "Walnut",
            "paint_price_id": "cat-putih",
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_manual_internal_orders_have_no_reseller_fees_and_can_use_supplier(client):
    http, _, _ = client
    payload = {
        "source": "internal",
        "customer": "Pesanan langsung",
        "ordered_at": "2026-10-16T09:00:00+07:00",
        "deadline": "2026-10-20T17:00:00+07:00",
        "items": [{"produk": "Custom internal", "qty": 2}],
    }
    response = await http.post("/processing/jobs/manual", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["order_number"].startswith("INT-")
    job = await save(http, (await rows(http))[0], price_id="", production_price="123000", paint_price="40000")
    assert job["source"] == "internal" and job["production_total"] == "246000"
    assert job["process_fee"] == "0" and job["paint_price"] == "0" and job["reseller_total"] is None
    payload["route"] = "supplier"
    response = await http.post("/processing/jobs/manual", json=payload)
    assert response.status_code == 200, response.text
    supplier = next(j for j in await rows(http) if j["route"] == "supplier")
    supplier = await save(http, supplier, party_id="s1", price_id="", production_price="50000")
    supplier = await advance(http, supplier, "supplier")
    assert supplier["stage"] == "packing" and supplier["process_fee"] == "0"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,party_id", [("tukang", "t1"), ("supplier", "s1")])
async def test_party_contact_fields_round_trip_and_preserve_on_legacy_edits(client, kind, party_id):
    http, _, _ = client
    contacts = {
        "alamat": "Alamat contoh",
        "no_wa": "081234567890",
        "no_rekening": "001234567890",
        "nama_bank": "Bank contoh",
    }
    if kind == "tukang":
        contacts["keahlian"] = "Meja dan lemari"
    response = await http.put(
        f"/processing/parties/{kind}/{party_id}", json={"version": 0, "nama": "Nama contoh", **contacts}
    )
    assert response.status_code == 200, response.text
    party = next(p for p in (await http.get("/processing/data")).json()["data"][kind] if p["id"] == party_id)
    assert all(party[key] == value for key, value in contacts.items())
    response = await http.put(
        f"/processing/parties/{kind}/{party_id}", json={"version": party["version"], "nama": "Nama diperbarui"}
    )
    assert response.status_code == 200, response.text
    party = next(p for p in (await http.get("/processing/data")).json()["data"][kind] if p["id"] == party_id)
    assert all(party[key] == value for key, value in contacts.items())
    response = await http.put(
        f"/processing/parties/{kind}/{party_id}", json={"version": party["version"], "nama": party["nama"], "no_wa": ""}
    )
    assert response.status_code == 200, response.text
    party = next(p for p in (await http.get("/processing/data")).json()["data"][kind] if p["id"] == party_id)
    assert party["no_wa"] == "" and party["no_rekening"] == "001234567890"


@pytest.mark.asyncio
async def test_production_hierarchy_reuse_auto_sku_unique_size_and_order_snapshots(client):
    http, _, factory = client
    for node_id, body in [
        ("cat", {"kind": "kategori", "code": "azf", "name": "Furniture"}),
        ("prod", {"kind": "produk", "parent_id": "cat", "code": "1r.10", "name": "Rak"}),
        ("model", {"kind": "model", "parent_id": "prod", "code": "rpl", "name": "RPL"}),
    ]:
        response = await http.put(f"/processing/production-structure/{node_id}", json={"version": 0, **body})
        assert response.status_code == 200, response.text
    response = await http.put(
        "/processing/production-structure/duplicate",
        json={
            "version": 0,
            "kind": "kategori",
            "code": "AZF",
            "name": "Duplicate",
        },
    )
    assert response.status_code == 409
    response = await http.put(
        "/processing/production-structure/wrong-parent",
        json={
            "version": 0,
            "kind": "model",
            "parent_id": "cat",
            "code": "OTHER",
            "name": "Wrong",
        },
    )
    assert response.status_code == 422
    price = {
        "version": 0,
        "kind": "produksi",
        "produk": "Ignored",
        "sku": "USER-SKU",
        "harga": "150000",
        "structure_model_id": "model",
        "ukuran": "150 × 200",
    }
    response = await http.put("/processing/prices/structured", json=price)
    assert response.status_code == 200, response.text
    prices = (await http.get("/processing/prices")).json()["items"]
    first = next(p for p in prices if p["id"] == "structured")
    assert first["sku"] == "AZF.1R.10.RPL.150x200" and first["produk"] == "Rak" and first["jenis"] == "Furniture"
    assert any(p["id"] == "p1" for p in prices)
    assert (await http.put("/processing/prices/duplicate-size", json={**price, "ukuran": "150X200"})).status_code == 409
    assert (
        await http.put("/processing/prices/another-size", json={**price, "ukuran": "180x200", "harga": "175000"})
    ).status_code == 200
    await http.post("/processing/jobs/select", json={"route": "tukang", "item_ids": ["i1"]})
    job = await save(http, (await rows(http))[0], price_id="structured")
    assert job["production_reference"]["sku"] == "AZF.1R.10.RPL.150x200"
    # Master edits update future catalog labels without rewriting existing order snapshots.
    response = await http.put(
        "/processing/production-structure/model",
        json={
            "version": 1,
            "kind": "model",
            "parent_id": "prod",
            "code": "RPL2",
            "name": "Rak polos baru",
        },
    )
    assert response.status_code == 200, response.text
    first = next(p for p in (await http.get("/processing/prices")).json()["items"] if p["id"] == "structured")
    assert first["sku"] == "AZF.1R.10.RPL2.150x200"
    job = await save(http, job, price_id="structured")
    assert job["production_reference"]["sku"] == "AZF.1R.10.RPL.150x200"
    assert job["production_price"] == "150000"
    structure = (await http.get("/processing/production-structure")).json()["items"]
    assert len(structure) == 3 and all(n["kind"] != "varian" for n in structure)
    async with factory() as session:
        original = json.loads((await session.get(ProcessingCatalog, "global")).payload_json)
        assert original["harga_produksi"][0]["sku"] == "PROD.RAK"


@pytest.mark.asyncio
async def test_manual_order_codes_reseller_per_product_and_internal_typed(client):
    http, _, _ = client
    body = {
        "source": "reseller",
        "seller_code": "ab",
        "customer": "Penjual A",
        "ordered_at": "2026-10-16T09:00:00+07:00",
        "deadline": "2026-10-20T18:30:00+00:00",
        "items": [{"produk": "Rak", "qty": 1}, {"produk": "Custom", "qty": 2}],
    }
    response = await http.post("/processing/jobs/manual", json=body)
    assert response.status_code == 200, response.text
    jobs = await rows(http)
    assert all(j["order_number"] == "Menunggu SKU" for j in jobs)
    first = await save(http, next(j for j in jobs if j["produk"] == "Rak"))
    assert first["order_number"] == "PROD.RAK-2110-AB-01"  # Cut-off is 21 October in WIB.
    second = await save(
        http,
        next(j for j in jobs if j["produk"] == "Custom"),
        price_id="",
        production_price="120000",
        production_sku="AZF.CUSTOM.RPL.150x200",
    )
    assert second["order_number"] == "AZF.CUSTOM.RPL.150x200-2110-AB-01"
    assert first["order_id"] == second["order_id"] and first["id"] != second["id"]
    again = await save(http, first)
    assert again["order_number"] == first["order_number"]  # Resaving never consumes another number.
    response = await http.post("/processing/jobs/manual", json={**body, "items": [{"produk": "Rak kedua", "qty": 1}]})
    assert response.status_code == 200, response.text
    another = await save(http, next(j for j in await rows(http) if j["produk"] == "Rak kedua"))
    assert another["order_number"] == "PROD.RAK-2110-AB-02"

    response = await http.post("/processing/jobs/manual", json={**body, "seller_code": "A1"})
    assert response.status_code == 422
    internal = {**body, "source": "internal", "order_code": "INTERNAL-001"}
    response = await http.post("/processing/jobs/manual", json=internal)
    assert response.status_code == 200, response.text
    assert response.json()["order_number"] == "INTERNAL-001"
    item = next(j for j in await rows(http) if j["source"] == "internal")
    item = await save(http, item)
    assert item["order_number"] == "INTERNAL-001" and item["process_fee"] == "0"
    response = await http.post("/processing/jobs/manual", json={**internal, "order_code": " "})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_supplier_catalog_connection_prices_stock_snapshots_and_manual_catalog(client, monkeypatch):
    from cryptography.fernet import Fernet
    from sqlalchemy import text
    from tenants.marketplace_erp.adapters.api.v1 import processing_supplier_catalog as supplier

    http, _, factory = client
    monkeypatch.setenv("SHOPEE_TOKEN_ENCRYPTION_KEYS", Fernet.generate_key().decode())
    remote = {
        "unit_id": "u5",
        "unit_name": "BUMDes",
        "read_at": "2026-10-17T10:00:00+00:00",
        "items": [
            {
                "id": "remote1",
                "sku": "KRS.FUR-1",
                "name": "Produk BUMDes",
                "sell_price": "50000",
                "qty_on_hand": 7,
                "unit_of_measure": "pcs",
                "category_name": "Furniture",
            }
        ],
    }
    reads = []

    def transport(request):
        if request.url.path == "/api/auth/login":
            assert json.loads(request.content)["password"] == "one-time-password"
            return httpx.Response(
                200, json={"user": {"role": "admin"}}, headers={"set-cookie": "source=authenticated; Path=/; Secure"}
            )
        if request.method == "POST":
            assert "source=authenticated" in request.headers.get("cookie", "")
            return httpx.Response(
                200,
                json={
                    "token": "remote-read-key",
                    "unit_id": "u5",
                    "unit_name": "BUMDes",
                    "expires_at": "2027-10-17T00:00:00+00:00",
                },
            )
        assert request.headers.get("X-ERP-Catalog-Key") == "remote-read-key"
        reads.append(1)
        return httpx.Response(200, json=remote)

    monkeypatch.setattr(
        supplier, "catalog_http_client", lambda *args: httpx.AsyncClient(transport=httpx.MockTransport(transport))
    )
    response = await http.post(
        "/processing/supplier-catalog/connect", json={"username": "admin", "password": "one-time-password"}
    )
    assert response.status_code == 200, response.text
    assert "token" not in response.json()
    async with factory() as db:
        stored = await db.scalar(text("SELECT token FROM mpe_processing_supplier_connection"))
        assert stored.startswith("fernet:v1:") and "remote-read-key" not in stored and "password" not in stored
    data = (await http.get("/processing/supplier-catalog")).json()
    assert data["items"][0]["qty_on_hand"] == 7 and data["items"][0]["sell_price"] == "50000"
    await http.post("/processing/jobs/select", json={"route": "supplier", "item_ids": ["i1"]})
    job = await save(
        http,
        (await rows(http))[0],
        party_id="s1",
        price_id="",
        catalog_mode="otomatis",
        supplier_product_id="remote1",
        production_price="1",
    )
    assert job["production_price"] == "50000" and job["supplier_reference"]["qty_on_hand"] == 7
    assert job["supplier_product_name"] == "Produk BUMDes" and job["paint_price"] == "0"
    count = len(reads)
    remote["items"][0].update(sell_price="60000", qty_on_hand=3)
    job = await save(
        http,
        job,
        party_id="s1",
        price_id="",
        catalog_mode="otomatis",
        supplier_product_id="remote1",
        production_price="1",
    )
    assert len(reads) == count and job["production_price"] == "50000"  # Unchanged mapping keeps its saved price.
    job = await save(
        http,
        job,
        party_id="s1",
        price_id="",
        catalog_mode="otomatis",
        supplier_product_id="remote1",
        refresh_catalog_price=True,
    )
    assert job["production_price"] == "60000" and job["supplier_reference"]["qty_on_hand"] == 3
    # A product that sold out stays readable and keeps its mapping, but cannot be chosen for a new one.
    remote["items"][0].update(qty_on_hand=0)
    job = await save(
        http, job, party_id="s1", price_id="", catalog_mode="otomatis", supplier_product_id="remote1",
        refresh_catalog_price=True,
    )
    assert job["supplier_reference"]["qty_on_hand"] == 0
    remote["items"].append(dict(remote["items"][0], id="remote2", sku="KRS.FUR-2", qty_on_hand=0))
    response = await http.put(
        "/processing/jobs/" + job["id"],
        json=dict(version=job["version"], party_id="s1", price_id="", color="Putih", packing_type="biasa",
                  packing_price="5000", catalog_mode="otomatis", supplier_product_id="remote2"),
    )
    assert response.status_code == 409 and "habis" in response.text
    response = await http.put(
        "/processing/prices/nonwood1",
        json={"version": 0, "kind": "non_kayu", "produk": "Produk manual", "harga": "12345", "sku": "MANUAL-1"},
    )
    assert response.status_code == 200, response.text
    job = await save(http, job, party_id="s1", price_id="nonwood1", catalog_mode="manual", production_price="1")
    assert (
        job["production_price"] == "12345"
        and job["supplier_product_name"] == "PRODUK MANUAL"
        and job["supplier_reference"] is None
    )
    response = await http.put(
        "/processing/prices/packing1",
        json={"version": 0, "kind": "packing", "packing_type": "kayu", "produk": "Kemasan besar", "harga": "25000"},
    )
    assert response.status_code == 200, response.text
    job = await advance(http, job, "supplier")
    response = await http.put(
        "/processing/jobs/" + job["id"] + "/packing",
        json={"version": job["version"], "packing_type": "kayu", "packing_price": "27500"},
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_paint_color_master_and_structured_tariff(client):
    http, app, _ = client
    color = {"version": 0, "name": "  Biru   Laut "}
    assert (await http.put("/processing/paint-colors/c1", json=color)).status_code == 200
    assert (await http.get("/processing/paint-colors")).json()["items"] == [
        {"id": "c1", "name": "Biru Laut", "version": 1}
    ]
    assert (await http.put("/processing/paint-colors/c2", json={"version": 0, "name": "biru laut"})).status_code == 409
    assert (await http.put("/processing/paint-colors/c1", json=color)).status_code == 409
    for node_id, kind, parent, code in [("cat", "kategori", "", "AZF"), ("product", "produk", "cat", "1R.10"), ("model", "model", "product", "RPL")]:
        assert (await http.put(f"/processing/production-structure/{node_id}", json={
            "version": 0, "kind": kind, "parent_id": parent, "code": code, "name": node_id
        })).status_code == 200
    source = {"version": 0, "kind": "produksi", "produk": "product", "structure_model_id": "model", "ukuran": "150x200", "harga": "100000"}
    assert (await http.put("/processing/prices/structured", json=source)).status_code == 200
    paint = {"version": 0, "kind": "pengecatan", "produk": "Forged", "sku": "wrong", "ukuran": "wrong", "warna": "wrong", "color_id": "c1", "production_price_id": "structured", "harga": "30000"}
    assert (await http.put("/processing/prices/paint", json=paint)).status_code == 200
    saved = next(p for p in (await http.get("/processing/prices")).json()["items"] if p["id"] == "paint")
    assert (saved["produk"], saved["ukuran"], saved["sku"], saved["warna"]) == ("product", "150x200", "AZF.1R.10.RPL.150x200", "Biru Laut")
    assert (await http.put("/processing/prices/bad", json={**paint, "production_price_id": "p1"})).status_code == 422
    assert (await http.put("/processing/prices/bad", json={**paint, "color_id": "missing"})).status_code == 422
    assert (await http.put("/processing/paint-colors/c1", json={"version": 1, "name": "Biru Tua"})).status_code == 200
    saved = next(p for p in (await http.get("/processing/prices")).json()["items"] if p["id"] == "paint")
    assert saved["warna"] == "Biru Laut"
    for role in ("owner", "staff"):
        async def actor():
            return SimpleNamespace(id="other", role=role)
        app.dependency_overrides[get_current_user_marketplace_erp] = actor
        assert (await http.get("/processing/paint-colors")).status_code == 403
        assert (await http.put("/processing/paint-colors/no", json=color)).status_code == 403


@pytest.mark.asyncio
async def test_manual_paint_tariff_and_switching_from_a_production_product(client):
    http, _, _ = client
    for node_id, kind, parent, code in [("cat", "kategori", "", "AZF"), ("product", "produk", "cat", "1R.10"), ("model", "model", "product", "RPL")]:
        assert (await http.put(f"/processing/production-structure/{node_id}", json={
            "version": 0, "kind": kind, "parent_id": parent, "code": code, "name": node_id
        })).status_code == 200
    source = {"version": 0, "kind": "produksi", "produk": "product", "structure_model_id": "model", "ukuran": "150x200", "harga": "100000"}
    assert (await http.put("/processing/prices/structured", json=source)).status_code == 200
    manual = {"version": 0, "kind": "pengecatan", "produk": "Rak Custom", "ukuran": "40x60", "sku": "", "warna": "Putih", "production_price_id": "", "harga": "12000"}
    assert (await http.put("/processing/prices/manual", json=manual)).status_code == 200
    item = lambda items, pid: next(p for p in items if p["id"] == pid)  # noqa: E731
    saved = item((await http.get("/processing/prices")).json()["items"], "manual")
    assert (saved["produk"], saved["ukuran"], saved["warna"], saved["harga"]) == ("Rak Custom", "40x60", "Putih", "12000")
    assert not saved.get("production_price_id") and not saved.get("model")
    auto = {"version": 0, "kind": "pengecatan", "produk": "x", "warna": "Biru", "production_price_id": "structured", "harga": "30000"}
    assert (await http.put("/processing/prices/auto", json=auto)).status_code == 200
    saved = item((await http.get("/processing/prices")).json()["items"], "auto")
    assert saved["model"] == "model" and saved["production_price_id"] == "structured"
    switched = {"version": saved["version"], "kind": "pengecatan", "produk": "Model Sendiri", "ukuran": "30x30", "warna": "Biru", "production_price_id": "", "harga": "30000"}
    assert (await http.put("/processing/prices/auto", json=switched)).status_code == 200
    saved = item((await http.get("/processing/prices")).json()["items"], "auto")
    assert (saved["produk"], saved["ukuran"]) == ("Model Sendiri", "30x30")
    assert not saved.get("production_price_id") and not saved.get("model") and not saved.get("model_kode")


@pytest.mark.asyncio
async def test_document_templates_version_scope_and_validation(client):
    http, app, factory = client
    assert (await http.get('/processing/document-templates/INV')).json() == {'kind': 'INV', 'version': 0, 'orientation': 'portrait', 'blocks': None}
    block = {'id': 'title', 'kind': 'text', 'x': 40, 'y': 40, 'width': 300, 'height': 60,
             'text': 'INVOICE {{nomor_dokumen}}', 'font_size': 24}
    body = {'version': 0, 'blocks': [block]}
    assert (await http.put('/processing/document-templates/INV', json=body)).status_code == 200
    assert (await http.get('/processing/document-templates/PO')).json()['blocks'] is None
    assert (await http.put('/processing/document-templates/INV', json=body)).status_code == 409
    saved = (await http.get('/processing/document-templates/INV')).json()
    assert saved['version'] == 1 and saved['blocks'][0]['text'] == block['text']
    assert (await http.put('/processing/document-templates/INV', json={'version': 1, 'blocks': [{**block, 'x': 80}]})).status_code == 200
    assert (await http.get('/processing/document-templates/INV')).json()['blocks'][0]['x'] == 80
    for blocks in [[block, block], [{**block, 'x': 790}], [{**block, 'image_url': 'javascript:alert(1)'}]]:
        assert (await http.put('/processing/document-templates/PO', json={'version': 0, 'blocks': blocks})).status_code == 422
    assert (await http.get('/processing/document-templates/OTHER')).status_code == 422
    async with factory() as session:
        assert (await session.get(Pesanan, 'order1')).status == 'to_ship'
    for role in ('owner', 'staff'):
        async def actor():
            return SimpleNamespace(id='other', role=role)
        app.dependency_overrides[get_current_user_marketplace_erp] = actor
        assert (await http.get('/processing/document-templates/INV')).status_code == 403
        assert (await http.put('/processing/document-templates/INV', json=body)).status_code == 403


@pytest.mark.asyncio
async def test_template_bootstrap_when_shared_startup_did_not_create_table(client):
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import ProcessingDocumentTemplate

    http, _, factory = client
    async with factory() as session:
        async with session.bind.begin() as connection:
            await connection.run_sync(ProcessingDocumentTemplate.__table__.drop)
    response = await http.get("/processing/document-templates/INV")
    assert response.status_code == 200, response.text
    assert response.json() == {"kind": "INV", "version": 0, "orientation": "portrait", "blocks": None}
    assert (await http.put("/processing/document-templates/PO", json={"version": 0, "blocks": []})).status_code == 200
    assert (await http.get("/processing/document-templates/PO")).json()["version"] == 1
    assert (await http.get("/processing/document-templates/INV")).json()["blocks"] is None
    async with factory() as session:
        assert (await session.get(Pesanan, "order1")).status == "to_ship"


@pytest.mark.asyncio
async def test_landscape_template_columns_and_page_bounds(client):
    http, _, _ = client
    block = {"id": "table", "kind": "table", "x": 30, "y": 250, "width": 1060, "height": 240,
             "row_count": 4, "show_totals": True,
             "columns": [{"field": "harga_barang", "label": "Harga barang", "total": True},
                         {"field": "biaya_pengecatan", "label": "Jasa pengecatan", "total": True}]}
    body = {"version": 0, "orientation": "landscape", "blocks": [block]}
    assert (await http.put("/processing/document-templates/INV", json=body)).status_code == 200
    saved = (await http.get("/processing/document-templates/INV")).json()
    assert saved["orientation"] == "landscape"
    assert saved["blocks"][0]["columns"][1]["field"] == "biaya_pengecatan"
    assert saved["blocks"][0]["row_count"] == 4
    assert (await http.put("/processing/document-templates/PO", json={**body, "orientation": "portrait"})).status_code == 422
    assert (await http.put("/processing/document-templates/PO", json={**body, "blocks": [{**block, "y": 790}]})).status_code == 422


@pytest.mark.asyncio
async def test_existing_portrait_template_payload_is_read_without_rewrite(client):
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import ProcessingDocumentTemplate

    http, _, factory = client
    old = [{"id": "old", "kind": "text", "x": 40, "y": 40, "width": 200, "height": 40, "text": "Existing"}]
    async with factory() as session:
        session.add(ProcessingDocumentTemplate(id="INV", payload_json=json.dumps(old), version=2))
        await session.commit()
    saved = (await http.get("/processing/document-templates/INV")).json()
    assert saved == {"kind": "INV", "orientation": "portrait", "version": 2, "blocks": old}
    async with factory() as session:
        assert json.loads((await session.get(ProcessingDocumentTemplate, "INV")).payload_json) == old


@pytest.mark.asyncio
async def test_canvas_layer_metadata_and_embedded_logo(client):
    import base64
    http, _, _ = client
    image = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"logo").decode()
    block = {"id": "logo", "kind": "logo", "x": 40, "y": 40, "width": 140, "height": 80,
             "name": "Logo perusahaan", "locked": True, "hidden": False, "image_url": image}
    response = await http.put("/processing/document-templates/INV", json={"version": 0, "blocks": [block]})
    assert response.status_code == 200, response.text
    saved = (await http.get("/processing/document-templates/INV")).json()["blocks"][0]
    assert saved["name"] == "Logo perusahaan" and saved["locked"] is True
    assert saved["image_url"] == image
    for invalid in ["data:image/svg+xml;base64,PHN2Zz4=", "data:image/png;base64,PGh0bWw+", "data:image/png;base64,???"]:
        assert (await http.put("/processing/document-templates/PO", json={"version": 0, "blocks": [{**block, "image_url": invalid}]})).status_code == 422


@pytest.mark.asyncio
async def test_catalog_removal_does_not_revive_import_and_checks_version(client):
    http, _, factory = client
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import ProcessingPrice

    response = await http.delete('/processing/prices/p1?version=0')
    assert response.status_code == 200, response.text
    assert response.json()['action'] == 'deleted'
    assert (await http.get('/processing/prices?include_inactive=true')).json()['items'] == []
    # The immutable source remains, while its override prevents accidental resurrection.
    async with factory() as session:
        original = await session.get(ProcessingCatalog, 'global')
        assert json.loads(original.payload_json)['harga_produksi'][0]['id'] == 'p1'
        assert json.loads((await session.get(ProcessingPrice, 'p1')).payload_json)['deleted']
    assert (await http.put('/processing/prices/p1', json={
        'version': 0, 'kind': 'produksi', 'produk': 'Rak', 'harga': '1'
    })).status_code == 409
    assert (await http.post('/processing/prices/p1/activate?version=1')).status_code == 404
    payload = {'version': 0, 'kind': 'non_kayu', 'produk': 'Produk supplier', 'harga': '50000'}
    assert (await http.put('/processing/prices/manual', json=payload)).status_code == 200
    assert (await http.delete('/processing/prices/manual?version=0')).status_code == 409
    assert (await http.delete('/processing/prices/manual?version=1')).json()['action'] == 'deleted'


@pytest.mark.asyncio
async def test_used_catalog_deactivates_and_preserves_history_but_blocks_new_mapping(client):
    http, _, _ = client
    assert (await http.post('/processing/jobs/manual', json={
        'customer': 'Reseller A', 'ordered_at': '2026-10-16T09:00:00+07:00',
        'deadline': '2026-10-20T17:00:00+07:00', 'items': [{'produk': 'Rak', 'qty': 2}]
    })).status_code == 200
    job = await save(http, (await rows(http))[0], color='Polos / Tanpa Cat')
    response = await http.delete('/processing/prices/p1?version=0')
    assert response.json()['action'] == 'deactivated'
    assert (await http.get('/processing/prices')).json()['items'] == []
    inactive = (await http.get('/processing/prices?include_inactive=true')).json()['items'][0]
    assert not inactive['active'] and inactive['in_use']
    assert (await rows(http))[0] == job
    # Editing the old assignment retains both its selected catalog ID and original cost.
    updated = await save(http, job, color='Polos / Tanpa Cat')
    assert updated['production_price'] == job['production_price']
    assert updated['production_reference'] == job['production_reference']
    assert (await http.post('/processing/jobs/manual', json={
        'customer': 'Reseller B', 'ordered_at': '2026-10-16T09:00:00+07:00',
        'deadline': '2026-10-20T17:00:00+07:00', 'items': [{'produk': 'Baru', 'qty': 1}]
    })).status_code == 200
    new = next(r for r in await rows(http) if r['produk'] == 'Baru')
    response = await http.put('/processing/jobs/' + new['id'], json={
        'version': new['version'], 'party_id': 't1', 'price_id': 'p1', 'color': 'Polos / Tanpa Cat'
    })
    assert response.status_code == 422
    assert (await http.post('/processing/prices/p1/activate?version=0')).status_code == 409
    assert (await http.post('/processing/prices/p1/activate?version=1')).json()['action'] == 'activated'
    assert (await http.get('/processing/prices')).json()['items'][0]['active']


@pytest.mark.asyncio
async def test_deleted_variant_releases_size_and_related_tariff_prevents_deletion(client):
    http, _, _ = client
    for id_, kind, parent, code in [('c', 'kategori', '', 'AZF'), ('p', 'produk', 'c', 'RK'), ('m', 'model', 'p', 'ORI')]:
        response = await http.put('/processing/production-structure/' + id_, json={
            'version': 0, 'kind': kind, 'parent_id': parent, 'code': code, 'name': code
        })
        assert response.status_code == 200, response.text
    product = {'version': 0, 'kind': 'produksi', 'produk': 'Rak', 'structure_model_id': 'm', 'ukuran': '100x200', 'harga': '150000'}
    assert (await http.put('/processing/prices/old', json=product)).status_code == 200
    assert (await http.delete('/processing/prices/old?version=1')).json()['action'] == 'deleted'
    assert (await http.put('/processing/prices/new', json=product)).status_code == 200
    paint = {'version': 0, 'kind': 'pengecatan', 'produk': 'Rak', 'production_price_id': 'new', 'warna': 'Putih', 'harga': '20000'}
    assert (await http.put('/processing/prices/paint', json=paint)).status_code == 200
    assert (await http.delete('/processing/prices/new?version=1')).json()['action'] == 'deactivated'
    assert (await http.put('/processing/prices/paint', json={**paint, 'version': 1})).status_code == 200
    assert (await http.put('/processing/prices/another-paint', json=paint)).status_code == 422


async def bulk_fixture(http):
    for id_, kind, parent, code in [('bc', 'kategori', '', 'AZF'), ('bp', 'produk', 'bc', 'PL20'), ('bm', 'model', 'bp', 'RBK')]:
        result = await http.put('/processing/production-structure/' + id_, json={
            'version': 0, 'kind': kind, 'parent_id': parent, 'code': code, 'name': code
        })
        assert result.status_code == 200, result.text
    result = await http.put('/processing/prices/p2', json={
        'version': 0, 'kind': 'produksi', 'produk': 'Produk lama 2', 'ukuran': '80x200', 'harga': '250000', 'catatan': 'Tetap utuh'
    })
    assert result.status_code == 200
    return {'structure_model_id': 'bm', 'items': [
        {'id': 'p1', 'version': 0, 'ukuran': '60 × 20 × 200', 'harga': '150000'},
        {'id': 'p2', 'version': 1, 'ukuran': '80x20x200', 'harga': '250000'},
    ]}


@pytest.mark.asyncio
async def test_bulk_structure_preserves_order_snapshots_and_allows_size_swap(client):
    http, _, _ = client
    payload = await bulk_fixture(http)
    await http.post('/processing/jobs/manual', json={
        'customer': 'Reseller', 'ordered_at': '2026-10-16T09:00:00+07:00',
        'deadline': '2026-10-20T17:00:00+07:00', 'items': [{'produk': 'Rak', 'qty': 1}]
    })
    job = await save(http, (await rows(http))[0], color='Polos / Tanpa Cat')
    result = await http.post('/processing/prices/bulk-structure', json=payload)
    assert result.status_code == 200, result.text
    assert result.json()['count'] == 2
    prices = {p['id']: p for p in (await http.get('/processing/prices')).json()['items']}
    assert prices['p1']['sku'] == 'AZF.PL20.RBK.60x20x200'
    assert prices['p2']['sku'] == 'AZF.PL20.RBK.80x20x200'
    assert prices['p1']['harga'] == '150000' and prices['p2']['harga'] == '250000'
    assert prices['p2']['catatan'] == 'Tetap utuh'
    assert (await rows(http))[0] == job
    # Swap existing sizes atomically instead of colliding with the other selected row.
    swapped = {'structure_model_id': 'bm', 'items': [
        {'id': 'p1', 'version': 1, 'ukuran': '80x20x200', 'harga': '175000'},
        {'id': 'p2', 'version': 2, 'ukuran': '60x20x200', 'harga': '250000'},
    ]}
    result = await http.post('/processing/prices/bulk-structure', json=swapped)
    assert result.status_code == 200, result.text
    prices = {p['id']: p for p in (await http.get('/processing/prices')).json()['items']}
    assert prices['p1']['ukuran'] == '80x20x200' and prices['p1']['harga'] == '175000'
    assert prices['p2']['ukuran'] == '60x20x200'
    assert (await rows(http))[0] == job


@pytest.mark.asyncio
async def test_bulk_structure_rejects_conflicts_stale_invalid_and_inactive_without_partial_save(client):
    http, _, _ = client
    payload = await bulk_fixture(http)
    before = (await http.get('/processing/prices?include_inactive=true')).json()
    duplicate = {**payload, 'items': [payload['items'][0], {**payload['items'][1], 'ukuran': '60X20X200'}]}
    assert (await http.post('/processing/prices/bulk-structure', json=duplicate)).status_code == 409
    stale = {**payload, 'items': [payload['items'][0], {**payload['items'][1], 'version': 99}]}
    assert (await http.post('/processing/prices/bulk-structure', json=stale)).status_code == 409
    invalid = {**payload, 'items': [payload['items'][0], {**payload['items'][1], 'ukuran': '???'}]}
    assert (await http.post('/processing/prices/bulk-structure', json=invalid)).status_code == 422
    same_id = {**payload, 'items': [payload['items'][0], payload['items'][0]]}
    assert (await http.post('/processing/prices/bulk-structure', json=same_id)).status_code == 422
    assert (await http.get('/processing/prices?include_inactive=true')).json() == before
    assert (await http.put('/processing/prices/occupied', json={
        'version': 0, 'kind': 'produksi', 'produk': 'Bentrok', 'harga': '1', 'structure_model_id': 'bm', 'ukuran': '60x20x200'
    })).status_code == 200
    assert (await http.post('/processing/prices/bulk-structure', json=payload)).status_code == 409
    # Deleted catalog products cannot be silently recreated by a stale bulk form.
    assert (await http.delete('/processing/prices/p2?version=1')).status_code == 200
    assert (await http.post('/processing/prices/bulk-structure', json={**payload, 'items': [payload['items'][1]]})).status_code == 409


@pytest.mark.asyncio
async def test_bulk_structure_rolls_back_database_conflict(client):
    http, _, _ = client
    payload = await bulk_fixture(http)
    # Imported IDs may collide with master structure IDs created independently.
    assert (await http.put('/processing/production-structure/p1', json={
        'version': 0, 'kind': 'kategori', 'code': 'OTHER', 'name': 'Other'
    })).status_code == 200
    before = (await http.get('/processing/prices')).json()
    result = await http.post('/processing/prices/bulk-structure', json=payload)
    assert result.status_code == 409, result.text
    assert (await http.get('/processing/prices')).json() == before


@pytest.mark.asyncio
async def test_manual_product_takes_any_variants_and_keeps_a_one_line_summary(client):
    http, _, _ = client
    body = {
        "version": 0, "kind": "non_kayu", "produk": "Pupuk", "harga": "15000", "sku": "PPK-1",
        "varian": [{"nama": "Ukuran", "nilai": " 5  kg "}, {"nama": "Jenis", "nilai": "Basah"}, {"nama": "", "nilai": ""}],
    }
    assert (await http.put("/processing/prices/pupuk", json=body)).status_code == 200
    row = next(p for p in (await http.get("/processing/prices")).json()["items"] if p["id"] == "pupuk")
    assert row["varian"] == [{"nama": "UKURAN", "nilai": "5 KG"}, {"nama": "JENIS", "nilai": "BASAH"}]
    assert row["ukuran"] == "5 KG · BASAH" and row["produk"] == "PUPUK" and row["sku"] == "PPK-1"
    # Without variants the product is valid too, and the summary is cleared.
    body = {**body, "version": row["version"], "varian": []}
    assert (await http.put("/processing/prices/pupuk", json=body)).status_code == 200
    row = next(p for p in (await http.get("/processing/prices")).json()["items"] if p["id"] == "pupuk")
    assert row["varian"] == [] and row["ukuran"] == ""
    # An older client that only sends `ukuran` drops the variant list instead of leaving it stale.
    legacy = {"version": row["version"], "kind": "non_kayu", "produk": "Pupuk", "harga": "15000", "ukuran": "10 kg"}
    assert (await http.put("/processing/prices/pupuk", json=legacy)).status_code == 200
    row = next(p for p in (await http.get("/processing/prices")).json()["items"] if p["id"] == "pupuk")
    assert row.get("varian") is None and row["ukuran"] == "10 KG"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "varian,kind",
    [
        ([{"nama": "Ukuran", "nilai": ""}], "non_kayu"),
        ([{"nama": "Jenis", "nilai": "Basah"}, {"nama": "jenis", "nilai": "basah"}], "non_kayu"),
        ([{"nama": "Jenis", "nilai": "x" * 100}, {"nama": "Ukuran", "nilai": "y"}], "non_kayu"),
        ([{"nama": "Jenis", "nilai": "Basah"}], "packing"),
    ],
)
async def test_manual_product_variants_are_validated(client, varian, kind):
    http, _, _ = client
    body = {"version": 0, "kind": kind, "produk": "Barang", "harga": "1", "varian": varian}
    assert (await http.put("/processing/prices/x1", json=body)).status_code == 422


@pytest.mark.asyncio
async def test_manual_product_takes_one_variant_name_with_several_values(client):
    http, _, _ = client
    body = {"version": 0, "kind": "non_kayu", "produk": "Kain", "harga": "1",
            "varian": [{"nama": "Warna", "nilai": "Merah"}, {"nama": "warna", "nilai": "Hitam"}]}
    assert (await http.put("/processing/prices/kain", json=body)).status_code == 200, "same name, different value"
    row = next(p for p in (await http.get("/processing/prices")).json()["items"] if p["id"] == "kain")
    assert row["ukuran"] == "MERAH · HITAM"


def test_supplier_product_name_carries_the_variants():
    from tenants.marketplace_erp.adapters.api.v1.processing_workflow import product_name

    assert product_name({"produk": "Pupuk"}) == "Pupuk"
    assert product_name({"produk": "Pupuk", "varian": []}) == "Pupuk"
    assert (
        product_name({"produk": "Pupuk", "varian": [{"nama": "Ukuran", "nilai": "5 kg"}, {"nama": "Jenis", "nilai": "Basah"}]})
        == "Pupuk (Ukuran: 5 kg, Jenis: Basah)"
    )


@pytest.mark.asyncio
async def test_manual_product_price_can_follow_later_but_not_be_used_without_one(client):
    http, _, _ = client
    put = lambda pid, **body: http.put(f"/processing/prices/{pid}", json=body)  # noqa: E731
    assert (await put("later", version=0, kind="non_kayu", produk="Menyusul", sku="LT-1")).status_code == 200
    row = next(p for p in (await http.get("/processing/prices")).json()["items"] if p["id"] == "later")
    assert "harga" not in row or row["harga"] is None
    # Other kinds still need a price.
    assert (await put("pack", version=0, kind="packing", packing_type="biasa", produk="Kemasan")).status_code == 422
    # A job cannot pick it until the price is filled.
    await http.post("/processing/jobs/select", json={"route": "supplier", "item_ids": ["i1"]})
    job = next(j for j in await rows(http) if j["route"] == "supplier")
    blocked = await http.put("/processing/jobs/" + job["id"], json=dict(
        version=job["version"], party_id="s1", price_id="later", catalog_mode="manual",
        color="Putih", packing_type="biasa", packing_price="5000"))
    assert blocked.status_code == 422 and "Isi harga" in blocked.text
    # Filling the price later makes it a normal product again.
    assert (await put("later", version=row["version"], kind="non_kayu", produk="Menyusul", harga="9000")).status_code == 200
    row = next(p for p in (await http.get("/processing/prices")).json()["items"] if p["id"] == "later")
    assert row["harga"] == "9000"
    # Clearing it again removes the stored price instead of keeping the old one.
    assert (await put("later", version=row["version"], kind="non_kayu", produk="Menyusul")).status_code == 200
    row = next(p for p in (await http.get("/processing/prices")).json()["items"] if p["id"] == "later")
    assert row.get("harga") is None


@pytest.mark.asyncio
async def test_manual_order_mapping_can_be_cancelled_and_the_order_deleted_at_any_stage(client):
    http, _, _ = client
    body = {
        "source": "reseller", "seller_code": "ab", "customer": "Penjual A", "route": "tukang",
        "ordered_at": "2026-10-16T09:00:00+07:00", "deadline": "2026-10-20T18:30:00+00:00",
        "items": [{"produk": "Rak", "qty": 1}],
    }
    assert (await http.post("/processing/jobs/manual", json=body)).status_code == 200
    job = await save(http, (await rows(http))[0])
    assert job["order_number"] == "PROD.RAK-2110-AB-01" and job["stage"] == "produksi"
    job = next(r for r in await rows(http) if r["id"] == job["id"])
    await advance(http, job, "produksi")
    job = next(r for r in await rows(http) if r["id"] == job["id"])
    assert job["stage"] == "pengecatan"
    # a draft cannot be "cancelled"; a mapped manual order goes back to a draft and keeps its order data
    stale = await http.post(f"/processing/jobs/{job['id']}/reset?version={job['version'] + 5}")
    assert stale.status_code == 409
    done = await http.post(f"/processing/jobs/{job['id']}/reset?version={job['version']}")
    assert done.status_code == 200, done.text
    job = next(r for r in await rows(http) if r["id"] == job["id"])
    assert job["stage"] == "draft" and job["order_number"] == "Menunggu SKU" and job["customer"] == "Penjual A"
    assert not job.get("party_id") and not job.get("price_id") and job["id"]
    assert (await http.post(f"/processing/jobs/{job['id']}/reset?version={job['version']}")).status_code == 409
    # mapped again, the code is formed again (the number used before is not given out twice)
    job = await save(http, job)
    assert job["order_number"] == "PROD.RAK-2110-AB-02"
    # a manual order is deleted at any stage
    advance_job = next(r for r in await rows(http) if r["id"] == job["id"])
    await advance(http, advance_job, "produksi")
    job = next(r for r in await rows(http) if r["id"] == job["id"])
    assert job["stage"] != "draft"
    gone = await http.delete(f"/processing/jobs/{job['id']}?version={job['version']}")
    assert gone.status_code == 200, gone.text
    assert await rows(http) == []


@pytest.mark.asyncio
async def test_shopee_items_are_released_not_cancelled_or_deleted_after_mapping(client):
    http, _, _ = client
    cand = (await http.get("/processing/candidates")).json()["items"]
    assert (await http.post("/processing/jobs/select", json={"route": "tukang", "item_ids": [cand[0]["item_id"]]})).status_code == 200
    job = await save(http, (await rows(http))[0])
    assert (await http.post(f"/processing/jobs/{job['id']}/reset?version={job['version']}")).status_code == 409
    assert (await http.delete(f"/processing/jobs/{job['id']}?version={job['version']}")).status_code == 409


@pytest.mark.asyncio
async def test_manual_orders_from_the_pesanan_menu_are_read_by_pemetaan_order(client):
    from fastapi import HTTPException

    from tenants.marketplace_erp.modules.marketplace_erp.application import pesanan_manual, services
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import TokoPenjualan

    http, _, factory = client
    async with factory() as session:
        reseller = TokoPenjualan(nama="Reseller Budi", nama_key="reseller budi", jenis="reseller", platform="shopee", kode="AB")
        own = TokoPenjualan(nama="Ampel Web", nama_key="ampel web", jenis="internal", platform="webapp", kode=None)
        off = TokoPenjualan(nama="Mati", nama_key="mati", jenis="reseller", platform="lazada", kode="ZZ", aktif=False)
        session.add_all([reseller, own, off])
        await session.commit()
        base = {
            "ordered_at": "2026-10-16T09:00:00+07:00", "deadline": "2026-10-20T18:30:00+00:00", "reference": "REF-1",
            "items": [{"produk": "Rak", "qty": 2, "note": "cat putih"}, {"produk": "Meja", "varian": "Besar", "qty": 1}],
        }
        order = await pesanan_manual.buat(session, pesanan_manual.PesananManualIn(toko_id=reseller.id, pemesan="Budi", **base))
        internal = await pesanan_manual.buat(session, pesanan_manual.PesananManualIn(toko_id=own.id, kode_pesanan="INT-77", **base))
        assert order.platform == "manual" and order.status == "to_ship" and order.id_eksternal.startswith("RSL-")
        order_id, internal_id = order.id, internal.id
        await session.commit()
        # refused: an inactive shop, an own-shop order without its code, the same code twice
        with pytest.raises(HTTPException) as e:
            await pesanan_manual.buat(session, pesanan_manual.PesananManualIn(toko_id=off.id, **base))
        assert e.value.status_code == 422
        with pytest.raises(HTTPException) as e:
            await pesanan_manual.buat(session, pesanan_manual.PesananManualIn(toko_id=own.id, **base))
        assert e.value.status_code == 422
        with pytest.raises(HTTPException) as e:
            await pesanan_manual.buat(session, pesanan_manual.PesananManualIn(toko_id=own.id, kode_pesanan="INT-77", **base))
        assert e.value.status_code == 409
        await session.rollback()

    cand = (await http.get("/processing/candidates?status=semua")).json()["items"]
    manual = [c for c in cand if c["order_id"] in (order_id, internal_id)]
    assert len(manual) == 4
    rak = next(c for c in manual if c["order_id"] == order_id and c["produk"] == "Rak")
    assert (rak["source"], rak["seller_code"], rak["customer"], rak["order_number"], rak["shop"], rak["note"]) == (
        "reseller", "AB", "Budi", "Menunggu SKU", "Reseller Budi", "cat putih",
    )
    assert rak["deadline"].startswith("2026-10-20") and rak["reference"] == "REF-1"
    own_row = next(c for c in manual if c["order_id"] == internal_id and c["produk"] == "Meja")
    assert (own_row["source"], own_row["order_number"], own_row["customer"]) == ("internal", "INT-77", "Ampel Web")

    ids = [c["item_id"] for c in manual if c["order_id"] == order_id]
    assert (await http.post("/processing/jobs/select", json={"route": "tukang", "item_ids": ids})).status_code == 200
    job = next(r for r in await rows(http) if r["produk"] == "Rak")
    assert job["source"] == "reseller" and job["order_number"] == "Menunggu SKU" and job["item_id"]
    mapped = await save(http, job)
    assert mapped["order_number"] == "PROD.RAK-2110-AB-01"  # SKU, ship-by date (WIB), seller code, running number
    # while mapped, the order cannot be deleted; released, it can
    async with factory() as session:
        with pytest.raises(HTTPException) as e:
            await services.delete_pesanan(session, order_id)
        assert e.value.status_code == 409
        await session.rollback()
    for j in [r for r in await rows(http) if r["order_id"] == order_id]:
        assert (await http.post(f"/processing/jobs/{j['id']}/release?version={j['version']}")).status_code == 200
    async with factory() as session:
        await services.delete_pesanan(session, order_id)
        await session.commit()
        # status moves without any stock work
        moved = await services.ubah_status_pesanan(session, internal_id, "shipped")
        assert moved.status == "shipped"
    assert (await http.get("/processing/candidates?status=semua")).json()["total"] >= 2


async def use_processing_permissions(app, factory, halaman):
    """Synthetic role only; exercise real dependencies on the assembled router."""
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import Role, RoleHalaman

    async with factory() as db:
        db.add(Role(id="delegated", nama="Delegated", akses_toko="semua"))
        db.add_all(RoleHalaman(role_id="delegated", halaman=h, tingkat=lv) for h, lv in halaman.items())
        await db.commit()

    async def user():
        return SimpleNamespace(id="delegate", role="delegated")

    app.dependency_overrides[get_current_user_marketplace_erp] = user


@pytest.mark.asyncio
@pytest.mark.parametrize("tab", ["tukang", "supplier", "pengecatan", "packing"])
@pytest.mark.parametrize("level", ["baca", "tulis"])
async def test_delegated_tabs_read_real_processing_routes(client, tab, level):
    http, app, factory = client
    await http.post('/processing/jobs/select', json={"route": "tukang", "item_ids": ["i1"]})
    await http.post('/processing/jobs/select', json={"route": "supplier", "item_ids": ["i2"]})
    await use_processing_permissions(app, factory, {f"processing_{tab}": level})
    for path in ('data', 'jobs', 'prices', 'paint-colors', 'production-structure', 'supplier-catalog/connection'):
        response = await http.get('/processing/' + path)
        assert response.status_code == 200, (path, response.text)
    catalog_response = await http.get('/processing/supplier-catalog')
    assert catalog_response.status_code == 409 and catalog_response.json()['detail'] == 'Hubungkan katalog terlebih dahulu'
    jobs = (await http.get('/processing/jobs')).json()['items']
    assert len(jobs) == (1 if tab in ('tukang', 'supplier') else 0)
    assert all(j['route'] == tab for j in jobs)
    for path in ('candidates', 'candidates/rekap'):
        assert (await http.get('/processing/' + path)).status_code == (200 if tab in ('tukang', 'supplier') else 403)
    # A mapping tab never grants access to edit Data Produksi or read templates.
    assert (await http.get('/processing/document-templates/INV')).status_code == 403
    assert (await http.put('/processing/paint-colors/new', json={'version': 0, 'name': 'Blue'})).status_code == 403
    assert (await http.put('/processing/parties/tukang/t1', json={'version': 0})).status_code == 403
    assert (await http.put('/processing/prices/new', json={'version': 0, 'kind': 'packing', 'produk': 'Box', 'harga': '10'})).status_code == 403
    assert (await http.post('/processing/supplier-catalog/connect', json={})).status_code == 403
    assert (await http.delete('/processing/supplier-catalog/connection')).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('halaman', [{}, {'processing': 'tulis'}, {'data_template': 'baca'}])
async def test_processing_without_relevant_ticks_is_denied(client, halaman):
    http, app, factory = client
    await use_processing_permissions(app, factory, halaman)
    for path in ('data', 'jobs', 'candidates', 'candidates/rekap', 'prices', 'paint-colors', 'production-structure', 'supplier-catalog'):
        assert (await http.get('/processing/' + path)).status_code == 403


@pytest.mark.asyncio
async def test_read_only_tabs_cannot_write_any_processing_job_route(client):
    http, app, factory = client
    await http.post('/processing/jobs/select', json={'route': 'tukang', 'item_ids': ['i1']})
    job = (await rows(http))[0]
    await use_processing_permissions(app, factory, {f'processing_{t}': 'baca' for t in ('tukang', 'supplier', 'pengecatan', 'packing')})
    requests = [
        ('POST', '/jobs/select', {'route': 'tukang', 'item_ids': ['i2']}),
        ('POST', '/jobs/manual', {}),
        ('PUT', '/jobs/' + job['id'], {}),
        ('POST', '/jobs/' + job['id'] + '/transition', {}),
        ('POST', '/jobs/' + job['id'] + '/correct-time', {}),
        ('PUT', '/jobs/' + job['id'] + '/packing', {}),
        ('DELETE', '/jobs/' + job['id'] + '?version=1', None),
        ('POST', '/jobs/' + job['id'] + '/reset?version=1', None),
        ('POST', '/jobs/' + job['id'] + '/release?version=1', None),
    ]
    for method, path, body in requests:
        response = await http.request(method, '/processing' + path, json=body)
        assert response.status_code == 403, (method, path, response.text)
    assert (await rows(http))[0]['version'] == job['version']


@pytest.mark.asyncio
async def test_other_tab_writer_cannot_change_read_only_tukang_jobs(client):
    http, app, factory = client
    await http.post('/processing/jobs/select', json={'route': 'tukang', 'item_ids': ['i1']})
    job = (await rows(http))[0]
    await use_processing_permissions(app, factory, {'processing_tukang': 'baca', 'processing_supplier': 'tulis'})
    assert (await http.post('/processing/jobs/select', json={'route': 'supplier', 'item_ids': ['i2']})).status_code == 200
    assert (await http.post('/processing/jobs/select', json={'route': 'tukang', 'item_ids': ['i3']})).status_code == 403
    url = '/processing/jobs/' + job['id']
    for suffix in ('/release', '/reset', ''):
        method = 'DELETE' if suffix == '' else 'POST'
        assert (await http.request(method, url + suffix + '?version=1')).status_code == 403
    assert (await http.put(url, json={'version': 1, 'party_id': 't1', 'price_id': 'p1', 'color': 'Putih', 'packing_type': 'biasa', 'packing_price': '0'})).status_code == 403
    for stage in ('produksi', 'pengecatan', 'packing'):
        assert (await http.post(url + '/transition', json={'version': 1, 'stage': stage})).status_code == 403
    assert (await http.put(url + '/packing', json={'version': 1, 'packing_type': 'biasa', 'packing_price': '0'})).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('page,path,body', [
    ('data_tarif_pengecatan', 'paint-colors/delegated', {'version': 0, 'name': 'Blue'}),
    ('data_produk_kayu', 'production-structure/delegated', {'version': 0, 'kind': 'kategori', 'code': 'K', 'name': 'Kayu'}),
    ('data_template', 'document-templates/INV', {'version': 0, 'blocks': []}),
    ('data_supplier', 'parties/supplier/new', {'version': 0, 'nama': 'Fixture', 'alamat': 'Fixture'}),
    ('data_tukang', 'parties/tukang/new', {'version': 0, 'nama': 'Fixture', 'alamat': 'Fixture'}),
    ('data_tarif_packing', 'prices/delegated', {'version': 0, 'kind': 'packing', 'produk': 'Box', 'packing_type': 'biasa', 'harga': '10'}),
    ('data_katalog_manual', 'prices/delegated', {'version': 0, 'kind': 'non_kayu', 'produk': 'Box', 'harga': '10'}),
])
@pytest.mark.parametrize('level', ['baca', 'tulis'])
async def test_data_produksi_writes_follow_their_own_page(client, page, path, body, level):
    http, app, factory = client
    await use_processing_permissions(app, factory, {page: level})
    response = await http.put('/processing/' + path, json=body)
    assert response.status_code == (200 if level == 'tulis' else 403), response.text
    assert (await http.get('/processing/jobs')).status_code == 403


@pytest.mark.asyncio
async def test_all_assembled_processing_routes_have_page_rules_and_deny_without_ticks(client):
    import re
    from tenants.marketplace_erp.modules.marketplace_erp.application import peta_akses

    http, app, factory = client
    await use_processing_permissions(app, factory, {})
    paths = app.openapi()['paths']
    assert len(paths) >= 20
    for path, operations in paths.items():
        for method in operations:
            if method.upper() not in {'GET', 'POST', 'PUT', 'DELETE', 'PATCH'}:
                continue
            concrete = re.sub(r"\{[^}]+\}", "fixture", path)
            assert peta_akses.cari(concrete, method) is not None, (method, path)
            response = await http.request(method, concrete, json={})
            assert response.status_code == 403, (method, path, response.text)


@pytest.mark.asyncio
async def test_processing_page_guard_denies_unmapped_routes_even_for_admin(client):
    from fastapi import Depends
    from tenants.marketplace_erp.adapters.api.v1.processing_permissions import processing_access

    http, app, _ = client

    @app.get('/processing/unmapped', dependencies=[Depends(processing_access)])
    async def unmapped():
        return {'unexpected': True}

    assert (await http.get('/processing/unmapped')).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('level', ['baca', 'tulis'])
async def test_supplier_connection_requires_automatic_catalog_write(client, level):
    http, app, factory = client
    await use_processing_permissions(app, factory, {'data_katalog_otomatis': level})
    assert (await http.get('/processing/data')).status_code == 200
    assert (await http.get('/processing/supplier-catalog/connection')).status_code == 200
    assert (await http.delete('/processing/supplier-catalog/connection')).status_code == (200 if level == 'tulis' else 403)
    # Missing credentials stop at validation; no outbound request is made.
    assert (await http.post('/processing/supplier-catalog/connect', json={})).status_code == (422 if level == 'tulis' else 403)


@pytest.mark.asyncio
async def test_packing_writer_can_finish_packing_without_tukang_write(client):
    http, app, factory = client
    await http.post('/processing/jobs/select', json={'route': 'tukang', 'item_ids': ['i1']})
    job = await save(http, (await rows(http))[0], color='Polos / Tanpa Cat')
    job = await advance(http, job, 'produksi')
    await use_processing_permissions(app, factory, {'processing_tukang': 'baca', 'processing_packing': 'tulis'})
    response = await http.put('/processing/jobs/' + job['id'] + '/packing', json={
        'version': job['version'], 'packing_type': 'biasa', 'packing_price': '0',
    })
    assert response.status_code == 200, response.text
    job = (await rows(http))[0]
    job = await advance(http, job, 'packing')
    assert job['stage'] == 'siap_kirim'
