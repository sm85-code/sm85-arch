"""Only synthetic fixtures + disposable databases. No production network or writes."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1.order_bridge_router import router
from tenants.marketplace_erp.modules.marketplace_erp.application import akses, order_bridge as b, toko_penjualan as tp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import easyboss as eb
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase, get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import BridgeOrder, BridgeRun, ItemPesanan, Pesanan, TokoPenjualan


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


def shop(**kw):
    return eb.Shop(platform="lazada", shop_id="shop-1", name="Synthetic Shop", active=True, **kw)


def order(**kw):
    return eb.Order.model_validate({"platform": "lazada", "shop_id": "shop-1", "order_id": "original-1",
        "easyboss_order_id": "eb-1", "package_ids": ["package-a", "package-b"], "complete": True,
        "status": "ready", "paid": True, "source_status": "fixture_paid_ready", "buyer": "Fixture Buyer",
        "ordered_at": "2026-11-01T12:00:00+08:00", "deadline": "2026-11-03T23:59:00+08:00",
        "buyer_note": "Fixture note", "items": [{"item_id": "line-1", "product": "Chair", "variant": "Blue",
        "product_sku": "CHAIR", "variant_sku": "BLUE", "quantity": 2}, {"item_id": "line-2", "product": "Desk", "quantity": 1}], **kw})


class Fake:
    def __init__(self, shops=None, orders=None):
        self.shop_rows = [shop()] if shops is None else shops
        self.order_rows = [order()] if orders is None else orders

    async def shops(self):
        return self.shop_rows

    async def orders(self, since):
        return self.order_rows


async def sync(s, g=None, links=None):
    g = g or Fake()
    preview = await b.sync_shops(s, g)
    return await b.sync_shops(s, g, preview=False, expected=preview["fingerprint"], links=links)


@pytest.mark.asyncio
async def test_shop_new_repeat_rename_and_similar_names(session):
    await sync(session)
    first = await session.scalar(select(TokoPenjualan))
    id_ = first.id
    await sync(session)
    g = Fake(shops=[shop().model_copy(update={"name": "Renamed"}), shop().model_copy(update={"shop_id": "shop-2", "name": "Renamed 2"})])
    await sync(session, g)
    assert first.id == id_ and first.nama == "Renamed"
    assert await session.scalar(select(func.count()).select_from(TokoPenjualan)) == 2


@pytest.mark.asyncio
async def test_manual_review_link_reseller_webapp_and_shopee_preserved(session):
    old = await tp.buat(session, "Synthetic Shop", "internal", "lazada", None, "")
    reseller = await tp.buat(session, "Reseller", "reseller", "tiktokshop", "ZZ", "")
    web = await tp.buat(session, "Web App", "internal", "webapp", None, "")
    g = Fake(shops=[shop(), shop().model_copy(update={"platform": "shopee", "shop_id": "skip"})])
    plan = await b.sync_shops(session, g)
    assert plan["items"][0]["action"] == "review" and old["id"] in plan["manual_review"]
    await sync(session, g)
    assert (await session.get(TokoPenjualan, old["id"])).sinkron_status == "needs_review"
    await sync(session, g, {"lazada:shop-1": old["id"]})
    assert (await session.get(TokoPenjualan, old["id"])).easyboss_shop_id == "shop-1"
    assert (await session.get(TokoPenjualan, reseller["id"])).sumber == "manual"
    assert (await session.get(TokoPenjualan, web["id"])).platform == "webapp"
    assert await session.scalar(select(func.count()).select_from(TokoPenjualan)) == 3
    await sync(session, Fake(shops=[]))
    assert (await session.get(TokoPenjualan, old["id"])) is not None


@pytest.mark.asyncio
async def test_rename_collision_is_atomic_and_preview_stale(session):
    await sync(session)
    other = await tp.buat(session, "Collision", "internal", "lazada", None, "")
    g = Fake()
    plan = await b.sync_shops(session, g)
    g.shop_rows[0] = g.shop_rows[0].model_copy(update={"name": "Collision"})
    with pytest.raises(HTTPException) as e:
        await b.sync_shops(session, g, preview=False, expected=plan["fingerprint"])
    assert e.value.status_code == 409
    assert await session.get(TokoPenjualan, other["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["lazada", "tiktokshop"])
async def test_import_duplicate_multiitem_packages_restart_and_cancel(session, platform):
    g = Fake(shops=[shop().model_copy(update={"platform": platform})], orders=[order(platform=platform)])
    await sync(session, g)
    result = await b.run_orders(session, g)
    assert result["imported"] == 1
    assert await session.scalar(select(func.count()).select_from(ItemPesanan)) == 2
    p = await session.scalar(select(Pesanan))
    detail = json.loads(p.detail_json)
    assert p.platform == "manual" and detail["manual"]["toko_id"]
    assert detail["easyboss"]["order_id"] == "original-1"
    assert detail["easyboss"]["items"][0]["variant_sku"] == "BLUE"
    assert detail["easyboss"]["package_ids"] == ["package-a", "package-b"]
    g.order_rows *= 2
    assert (await b.run_orders(session, g))["skipped"] == 1
    session.add(BridgeRun(kind="orders", status="running"))
    await session.commit()
    g.order_rows = [order(platform=platform, status="cancelled")]
    await b.run_orders(session, g)
    assert p.status == "to_ship"  # production work untouched
    assert (await session.scalar(select(BridgeOrder))).status == "review"
    assert await session.scalar(select(func.count()).select_from(Pesanan)) == 1
    assert await session.scalar(select(BridgeRun.id).where(BridgeRun.status == "interrupted"))


@pytest.mark.asyncio
@pytest.mark.parametrize("changes,error", [({"deadline": None}, "missing_deadline"),
    ({"ordered_at": None}, "missing_ordered_at"), ({"buyer": ""}, "missing_buyer"),
    ({"items": []}, "missing_items"), ({"complete": False}, "incomplete_order_snapshot"),
    ({"shop_id": "unknown"}, "shop_unmapped_or_inactive")])
async def test_incomplete_fields_and_retry(session, changes, error):
    await sync(session)
    g = Fake(orders=[order(**changes)])
    result = await b.run_orders(session, g)
    assert result["failed"] == 1
    assert (await session.scalar(select(BridgeOrder))).error == error
    assert await session.scalar(select(func.count()).select_from(Pesanan)) == 0
    g.order_rows = [order(**({"shop_id": "unknown"} if "shop_id" in changes else {}))]
    if "shop_id" not in changes:
        assert (await b.run_orders(session, g, retry=True))["imported"] == 1


@pytest.mark.asyncio
async def test_status_payment_shopee_and_conflicting_package_snapshots(session):
    await sync(session)
    for changes in ({"status": "unpaid"}, {"paid": None}, {"status": "cancelled"}, {"status": "hold"}):
        assert (await b.run_orders(session, Fake(orders=[order(**changes)])))["skipped"] == 1
    assert (await b.run_orders(session, Fake(orders=[order(platform="shopee")])))["imported"] == 0
    a, c = order(), order(package_ids=["partial"])
    assert (await b.run_orders(session, Fake(orders=[a, c])))["failed"] == 1
    assert await session.scalar(select(func.count()).select_from(Pesanan)) == 0


@pytest.mark.asyncio
async def test_rollback_after_partial_manual_write_then_retry(session, monkeypatch):
    await sync(session)
    original = b.pesanan_manual.buat

    async def fail(s, payload):
        await original(s, payload)
        raise RuntimeError("sensitive upstream failure must not leak")

    monkeypatch.setattr(b.pesanan_manual, "buat", fail)
    assert (await b.run_orders(session, Fake()))["failed"] == 1
    assert await session.scalar(select(func.count()).select_from(Pesanan)) == 0
    row = await session.scalar(select(BridgeOrder))
    assert row.error == "import_transaction_failed"
    monkeypatch.setattr(b.pesanan_manual, "buat", original)
    assert (await b.run_orders(session, Fake(), retry=True))["imported"] == 1


@pytest.mark.asyncio
async def test_safe_delete_all_transaction_relations(session):
    await sync(session)
    t = await session.scalar(select(TokoPenjualan))
    assert (await tp.hapus(session, t.id))["can_delete"]
    await b.run_orders(session, Fake())
    refs = await tp.hapus(session, t.id)
    assert not refs["can_delete"] and "mpe_pesanan" in refs["relations"] and "mpe_bridge_order" in refs["relations"]
    with pytest.raises(HTTPException) as exc:
        await tp.hapus(session, t.id, confirm=True)
    assert exc.value.status_code == 409
    empty = await tp.buat(session, "Empty", "internal", "webapp", None, "")
    assert (await tp.hapus(session, empty["id"], confirm=True))["deleted"]
    assert await session.get(TokoPenjualan, empty["id"]) is None


@pytest.mark.asyncio
async def test_single_flight_pause_and_checkpoint_failure(session):
    async with b.exclusive(session):
        with pytest.raises(HTTPException):
            await b.run_orders(session, Fake())
    assert (await b.run_orders(session, Fake(), automatic=True))["status"] == "paused"

    class Fail(Fake):
        async def orders(self, since):
            raise eb.BridgeProblem("upstream_timeout")

    assert (await b.run_orders(session, Fail()))["error"] == "upstream_timeout"
    assert (await b.state(session)).checkpoint is None


@pytest.mark.asyncio
async def test_gateway_pagination_timeout_business_error_and_redaction(monkeypatch):
    calls = []

    async def no_sleep(*a):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            raise httpx.ReadTimeout("secret", request=req)
        return httpx.Response(200, json={"contract": "bridge-v1", "success": True,
            "items": [shop().model_dump()] if "cursor" not in req.url.params else [],
            "next_cursor": "second" if "cursor" not in req.url.params else None})

    async with httpx.AsyncClient(base_url="https://fixture/", transport=httpx.MockTransport(handler)) as c:
        assert len(await eb.Gateway(c).shops()) == 1
        assert len(calls) == 3
    for data in ({"success": True}, {"contract": "bridge-v1", "success": False, "error": "SECRET"}):
        async with httpx.AsyncClient(base_url="https://fixture/", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))) as c:
            with pytest.raises(eb.BridgeProblem, match="upstream_contract"):
                await eb.Gateway(c).shops()
    assert eb.native_time("2026-11-01 12:00:00").isoformat() == "2026-11-01T04:00:00+00:00"
    with pytest.raises(ValueError):
        order(ordered_at="2026-11-01T12:00:00")


@pytest.mark.asyncio
async def test_routes_auth_read_write_and_unverified_gate(session, monkeypatch):
    await akses.pastikan_bawaan(session)
    await session.commit()
    app = FastAPI()
    app.include_router(router, prefix="/api/marketplace-erp")

    async def db():
        yield session

    app.dependency_overrides[get_db_marketplace_erp] = db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/api/marketplace-erp/order-bridge")).status_code == 401
        async def staff():
            return SimpleNamespace(id=None, role="staff")
        app.dependency_overrides[get_current_user_marketplace_erp] = staff
        assert (await c.post("/api/marketplace-erp/order-bridge/orders")).status_code == 403
        async def admin():
            return SimpleNamespace(id=None, role="admin")
        app.dependency_overrides[get_current_user_marketplace_erp] = admin
        assert (await c.get("/api/marketplace-erp/order-bridge")).status_code == 200
        monkeypatch.delenv("EASYBOSS_BRIDGE_CONTRACT_VERIFIED", raising=False)
        assert (await c.put("/api/marketplace-erp/order-bridge/automation", json={"enabled": True})).status_code == 409
        assert (await c.post("/api/marketplace-erp/order-bridge/orders")).status_code == 503


@pytest.mark.asyncio
async def test_native_read_contract_forms_pagination_and_business_error():
    calls = []

    def handler(req):
        from urllib.parse import parse_qs
        fields = parse_qs(req.content.decode())
        calls.append((req.url.path, fields))
        if req.url.path.endswith("getShopAndOrderCountMap"):
            return httpx.Response(200, json={"result": "success", "shopAndCountMap": []})
        page = int(fields["page"][0])
        count = 100 if page == 1 else 1
        return httpx.Response(200, json={"result": "success", "total": 101,
            "packageList": [{"opOrderPackageId": str(page * 100 + i)} for i in range(count)]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        native = eb.NativeReadClient(c)
        assert await native.shop_counts("waitShip") == []
        assert len(await native.packages("waitShip")) == 101
    assert calls[-1][1]["page"] == ["2"] and calls[-1][1]["appPackageTab"] == ["waitShip"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"result": "error", "secret": "hidden"}))) as c:
        with pytest.raises(eb.BridgeProblem, match="native_business_error"):
            await eb.NativeReadClient(c).packages("waitShip")


@pytest.mark.asyncio
async def test_bad_item_does_not_block_valid_order_in_same_batch(session):
    await sync(session)
    bad = order(order_id="bad").model_dump(mode="json")
    bad["items"][0]["quantity"] = 0
    data = {"contract": "bridge-v1", "success": True, "next_cursor": None,
            "items": [bad, order().model_dump(mode="json")]}
    async with httpx.AsyncClient(base_url="https://fixture/", transport=httpx.MockTransport(lambda req: httpx.Response(200, json=data))) as c:
        result = await b.run_orders(session, eb.Gateway(c))
    assert result["imported"] == 1 and result["failed"] == 1
    assert await session.scalar(select(func.count()).select_from(Pesanan)) == 1


@pytest.mark.asyncio
async def test_late_page_error_no_checkpoint_or_partial_snapshot_import(session):
    await sync(session)

    def handler(req):
        if "cursor" in req.url.params:
            return httpx.Response(200, json={"success": False})
        return httpx.Response(200, json={"contract": "bridge-v1", "success": True, "next_cursor": "page2",
                                       "items": [order().model_dump(mode="json")]})

    async with httpx.AsyncClient(base_url="https://fixture/", transport=httpx.MockTransport(handler)) as c:
        result = await b.run_orders(session, eb.Gateway(c))
    assert result["status"] == "failed"
    assert await session.scalar(select(func.count()).select_from(Pesanan)) == 0
    assert (await b.state(session)).checkpoint is None


@pytest.mark.asyncio
async def test_retry_refetches_and_respects_new_cancellation(session):
    await sync(session)
    await b.run_orders(session, Fake(orders=[order(deadline=None)]))
    assert (await b.run_orders(session, Fake(orders=[order(status="cancelled")]), retry=True))["skipped"] == 1
    assert await session.scalar(select(func.count()).select_from(Pesanan)) == 0


@pytest.mark.asyncio
async def test_custom_readonly_role_cannot_mutate_bridge_or_delete_shop(session):
    from tenants.marketplace_erp.adapters.api.v1.toko_penjualan_router import router as shops_router
    await akses.pastikan_bawaan(session)
    await akses.buat(session, "Bridge Reader", "", "semua", {"toko_penjualan": "baca"})
    await session.commit()
    app = FastAPI()
    app.include_router(router, prefix="/api/marketplace-erp")
    app.include_router(shops_router, prefix="/api/marketplace-erp")

    async def db():
        yield session

    async def reader():
        return SimpleNamespace(id=None, role="bridge_reader")

    app.dependency_overrides[get_db_marketplace_erp] = db
    app.dependency_overrides[get_current_user_marketplace_erp] = reader
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/api/marketplace-erp/order-bridge")).status_code == 200
        assert (await c.post("/api/marketplace-erp/order-bridge/orders")).status_code == 403
        assert (await c.post("/api/marketplace-erp/order-bridge/shops", json={})).status_code == 403
        assert (await c.put("/api/marketplace-erp/order-bridge/automation", json={"enabled": False})).status_code == 403
        assert (await c.delete("/api/marketplace-erp/toko-penjualan/any?konfirmasi=true")).status_code == 403
