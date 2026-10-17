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
        ("GET", "/production-structure"),
        ("PUT", "/production-structure/node"),
        ("GET", "/supplier-catalog/connection"),
        ("POST", "/supplier-catalog/connect"),
        ("GET", "/supplier-catalog"),
        ("DELETE", "/supplier-catalog/connection"),
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
    response = await http.put(
        "/processing/prices/nonwood1",
        json={"version": 0, "kind": "non_kayu", "produk": "Produk manual", "harga": "12345", "sku": "MANUAL-1"},
    )
    assert response.status_code == 200, response.text
    job = await save(http, job, party_id="s1", price_id="nonwood1", catalog_mode="manual", production_price="1")
    assert (
        job["production_price"] == "12345"
        and job["supplier_product_name"] == "Produk manual"
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
