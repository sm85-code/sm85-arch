"""Exercise the internal workflow, permissions, isolation and price snapshots through HTTP."""

import json
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
    assert first["reseller_total"] == "410000"  # Includes packing 2 x 5,000.
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
@pytest.mark.parametrize("role", ["owner", "staff"])
async def test_all_workflow_endpoints_are_admin_only(client, role):
    http, app, _ = client

    async def user():
        return SimpleNamespace(id="other", role=role)

    async def forbidden_db():
        raise AssertionError("Non-admin reached DB")

    app.dependency_overrides[get_current_user_marketplace_erp] = user
    app.dependency_overrides[get_db_marketplace_erp] = forbidden_db
    for method, path in [
        ("GET", "/prices"),
        ("PUT", "/parties/tukang/t1"),
        ("PUT", "/prices/p1"),
        ("GET", "/jobs"),
        ("GET", "/candidates"),
        ("POST", "/jobs/select"),
        ("POST", "/jobs/manual"),
        ("PUT", "/jobs/job1"),
        ("POST", "/jobs/job1/transition"),
        ("DELETE", "/jobs/job1?version=1"),
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
