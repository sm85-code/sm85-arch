"""Lean EasyBoss bridge: request counting, detail-only-on-change, 429 backoff, daily cap, interval.

Synthetic fixtures and an in-memory database only. The real MCPGateway.call() runs against an
httpx.MockTransport so budget accounting and 429 detection are exercised end to end.
"""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import order_bridge as bridge
from tenants.marketplace_erp.modules.marketplace_erp.application import order_bridge_lean as lean
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters.easyboss_mcp import MCPGateway, rate_limited
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    BridgeOrder, BridgeState, Pesanan, TokoPenjualan,
)

CN = timezone(timedelta(hours=8))


def gmt(delta=timedelta()):
    return (datetime.now(CN) - delta).strftime("%Y-%m-%d %H:%M:%S")


def package(order_id, pkg, status="AWAITING_SHIPMENT"):
    return {"shopId": "shop", "platformOrderSn": order_id, "opOrderPackageId": pkg, "isSplit": 0, "itemQuantity": "1",
            "orderInfo": {"platform": "tiktok", "shopId": "shop", "platformOrderSn": order_id, "opOrderId": "eb-" + order_id,
                          "platformOrderStatus": status, "gmtPay": gmt(), "isCod": "0", "buyerUsername": "b***r",
                          "gmtOrderStart": gmt(), "gmtLastDelivery": gmt(-timedelta(days=2)), "itemQuantity": "1"},
            "items": {"l": {"opOrderItemId": "line-" + order_id, "opOrderId": "eb-" + order_id, "shopId": "shop",
                            "quantity": "1", "title": "Pakan", "skuSubName": "1kg"}}}


class Upstream:
    """Scripted easyboss-mcp. `orders` = {order_id: dict(pkg, status, dibuat)} listed for TikTok."""

    def __init__(self):
        self.orders = {}
        self.calls = []
        self.throttle = False

    def row(self, oid, o):
        return {"id": o["pkg"], "shopId": "shop", "no_pesanan": oid, "platform": "tiktok",
                "status": o["status"], "dibuat": o["dibuat"]}

    def tool(self, name, a):
        if self.throttle:
            return {"total": None, "pesanan": [], "error": "HTTP 200",
                    "detail": '{"result":"error","code":429,"message":"base.tooManyRequest"}'}
        if name == "eb_pesanan_list" and "extra" in a:
            oid = a["extra"]["platformOrderSns"]
            return {"total": 1, "pesanan": [self.row(oid, self.orders[oid])]}
        if name == "eb_pesanan_list":
            if a["platform"] != "tiktok":
                return {"total": 0, "pesanan": []}
            rows = sorted(self.orders.items(), key=lambda kv: kv[1]["dibuat"], reverse=True)
            page = rows[(a["halaman"] - 1) * a["per_halaman"]: a["halaman"] * a["per_halaman"]]
            return {"total": len(rows), "pesanan": [self.row(k, v) for k, v in page]}
        if name == "eb_pesanan_detail":
            o = self.orders[a["no_pesanan"]]
            return package(a["no_pesanan"], o["pkg"], o.get("source", "AWAITING_SHIPMENT"))
        raise AssertionError(name)

    def handler(self, request):
        body = json.loads(request.content)
        name, args = body["params"]["name"], body["params"]["arguments"]
        self.calls.append((name, args.get("tab"), args.get("platform"), args.get("no_pesanan") or (args.get("extra") or {}).get("platformOrderSns")))
        text = json.dumps(self.tool(name, args))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                         "result": {"content": [{"type": "text", "text": text}]}})


@pytest_asyncio.fixture
async def env(monkeypatch):
    monkeypatch.setenv("EASYBOSS_BRIDGE_CALL_DELAY_SECONDS", "0")
    for name in ("EASYBOSS_BRIDGE_DAILY_CAP", "EASYBOSS_BRIDGE_INTERVAL_SECONDS", "EASYBOSS_BRIDGE_ALERT_TELEGRAM_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        s.add(TokoPenjualan(jenis="internal", platform="tiktokshop", nama="Synthetic", nama_key="synthetic",
                            keterangan="", aktif=True, sumber="easyboss", easyboss_shop_id="shop", sinkron_status="synced"))
        s.add(BridgeState(id="easyboss", enabled=True))
        await s.commit()
        up = Upstream()
        client = httpx.AsyncClient(base_url="https://mcp.example/mcp", transport=httpx.MockTransport(up.handler))
        yield s, up, client
        await client.aclose()
    await engine.dispose()


async def run(s, client, **kw):
    return await bridge.run_orders(s, MCPGateway(client), **kw)


async def lean_state(s):
    st = await s.get(BridgeState, "easyboss")
    await s.refresh(st)
    return json.loads(st.lean_json)


def details(up):
    return [c for c in up.calls if c[0] == "eb_pesanan_detail"]


async def seed_imported(s, oid, source, *, pkg=None):
    """Already-imported EasyBoss order, so first-sight / refresh cannot create a second Pesanan."""
    s.add(Pesanan(id="p-" + oid, platform="manual", id_eksternal=oid, status="to_ship",
                  detail_json=json.dumps({"easyboss": {"platform": "tiktokshop", "shop_id": "shop", "order_id": oid}})))
    s.add(BridgeOrder(platform="tiktokshop", shop_id="shop", order_id=oid, pesanan_id="p-" + oid,
                      status="imported", source_status=source, snapshot_json="{}"))
    await s.commit()


@pytest.mark.asyncio
async def test_first_round_fetches_new_orders_and_first_sight_of_known_receipts(env):
    s, up, client = env
    await seed_imported(s, "old", "AWAITING_SHIPMENT")
    up.orders = {"old": {"pkg": "p-old", "status": "waitShip", "source": "AWAITING_COLLECTION", "dibuat": gmt(timedelta(hours=5))},
                 "new": {"pkg": "p-new", "status": "waitProcess", "source": "AWAITING_SHIPMENT", "dibuat": gmt(timedelta(minutes=5))}}
    result = await run(s, client)
    # 2 list pages (TikTok + Lazada) + 2 reads each for the new order and the known receipt.
    assert result["requests"] == 6 and len(up.calls) == 6
    assert {c[3] for c in details(up)} == {"old", "new"}
    assert result["imported"] == 1
    assert await s.scalar(select(func.count()).select_from(Pesanan)) == 2
    assert (await s.scalar(select(BridgeOrder).where(BridgeOrder.order_id == "old"))).source_status == "AWAITING_COLLECTION"
    st = await lean_state(s)
    assert st["requests"] == 6 and st["last_round"]["requests"] == 6

    up.calls.clear()
    result = await run(s, client)
    # Just refreshed: no slow-refresh yet, exactly the two list pages.
    assert result["requests"] == 2 and details(up) == []
    assert (await lean_state(s))["requests"] == 8


@pytest.mark.asyncio
async def test_detail_only_when_list_status_changes(env):
    s, up, client = env
    up.orders = {"a": {"pkg": "pa", "status": "waitProcess", "dibuat": gmt(timedelta(minutes=50))},
                 "b": {"pkg": "pb", "status": "waitProcess", "dibuat": gmt(timedelta(minutes=10))}}
    await run(s, client)
    up.calls.clear()
    up.orders["a"]["status"] = "waitShip"
    result = await run(s, client)
    assert details(up) == [("eb_pesanan_detail", None, None, "a")]
    assert result["requests"] == 4
    # Imported order is never duplicated by a status change.
    assert await s.scalar(select(func.count()).select_from(Pesanan)) == 2


@pytest.mark.asyncio
async def test_history_outside_lookback_is_never_imported(env):
    s, up, client = env
    up.orders = {"ancient": {"pkg": "px", "status": "finished", "dibuat": gmt(timedelta(days=10))}}
    result = await run(s, client)
    assert details(up) == [] and result["requests"] == 2
    assert await s.scalar(select(func.count()).select_from(Pesanan)) == 0


@pytest.mark.asyncio
async def test_paging_stops_at_known_orders(env):
    s, up, client = env
    up.orders = {f"o{i}": {"pkg": f"p{i}", "status": "finished", "dibuat": gmt(timedelta(days=5, minutes=i))}
                 for i in range(120)}
    await run(s, client)
    up.orders["fresh"] = {"pkg": "pf", "status": "waitProcess", "dibuat": gmt()}
    up.calls.clear()
    await run(s, client)
    lists = [c for c in up.calls if c[0] == "eb_pesanan_list" and c[3] is None]
    assert len(lists) == 2  # page 1 per platform only, known rows stop the paging


@pytest.mark.asyncio
async def test_429_in_http_200_json_backs_off_60_minutes_without_retry(env):
    s, up, client = env
    up.throttle = True
    result = await run(s, client)
    assert result["status"] == "failed" and result["error"] == "upstream_rate_limited"
    assert len(up.calls) == 1  # stopped on the first throttled call, no retry loop
    st = await lean_state(s)
    until = datetime.fromisoformat(st["backoff_until"])
    assert until - datetime.now(timezone.utc) > timedelta(minutes=59)
    up.calls.clear()
    up.throttle = False
    again = await run(s, client)
    assert again["status"] == "backoff" and up.calls == []
    again = await run(s, client, automatic=True)
    assert again["status"] == "backoff" and up.calls == []


@pytest.mark.asyncio
async def test_daily_cap_stops_and_alerts_once(env, monkeypatch, caplog):
    s, up, client = env
    monkeypatch.setenv("EASYBOSS_BRIDGE_DAILY_CAP", "3")
    up.orders = {"x": {"pkg": "px", "status": "waitProcess", "dibuat": gmt()},
                 "y": {"pkg": "py", "status": "waitProcess", "dibuat": gmt()}}
    with caplog.at_level("WARNING", logger="easyboss_bridge"):
        result = await run(s, client)
    assert len(up.calls) == 3 and result["error"] == "daily_cap_reached"
    assert any("batas harian 3" in r.getMessage() for r in caplog.records)
    up.calls.clear()
    again = await run(s, client)
    assert again["status"] == "daily_cap" and up.calls == []
    # Next WIB day resets the budget.
    st = await s.get(BridgeState, "easyboss")
    data = json.loads(st.lean_json)
    data["day"] = "2000-01-01"
    st.lean_json = json.dumps(data)
    await s.commit()
    await run(s, client)
    assert up.calls


@pytest.mark.asyncio
async def test_automatic_rounds_respect_persisted_interval(env, monkeypatch):
    s, up, client = env
    monkeypatch.setenv("EASYBOSS_BRIDGE_INTERVAL_SECONDS", "60")  # floor raises this to 600
    assert lean.interval_seconds() == 600
    monkeypatch.setenv("EASYBOSS_BRIDGE_INTERVAL_SECONDS", "1800")
    await run(s, client, automatic=True)
    up.calls.clear()
    second = await run(s, client, automatic=True)
    assert second["status"] == "not_due" and up.calls == []


def test_rate_limit_detection():
    assert rate_limited({"error": "HTTP 200", "detail": '{"code":429}'})
    assert rate_limited({"error": "x", "detail": "base.tooManyRequest"})
    assert rate_limited("Permintaan terlalu sering")
    assert not rate_limited({"error": "HTTP 500", "detail": "boom"})

@pytest.mark.asyncio
async def test_slow_refresh_rechecks_oldest_open_orders_even_when_list_signature_is_unchanged(env):
    """List operate status can stay ship_success while the marketplace status used by LZ-TS tabs moves."""
    s, up, client = env
    now = datetime.now(timezone.utc)
    for i, oid in enumerate(("stale", "mid", "freshish", "done")):
        source = "DELIVERED" if oid == "done" else "AWAITING_COLLECTION"
        await seed_imported(s, oid, source)
    # Oldest first; the completed order must never consume a refresh slot.
    ages = {"stale": 10, "mid": 6, "freshish": 5, "done": 12}
    for oid, hours in ages.items():
        await s.execute(BridgeOrder.__table__.update().where(BridgeOrder.order_id == oid).values(
            updated_at=now - timedelta(hours=hours)))
    await s.commit()
    up.orders = {oid: {"pkg": "p-" + oid, "status": "ship_success" if oid != "done" else "ship_success",
                       "source": "DELIVERED" if oid == "done" else "IN_TRANSIT",
                       "dibuat": gmt(timedelta(hours=ages[oid]))} for oid in ages}
    seen = {f"tiktokshop|shop|{oid}": [f"ship_success:p-{oid}", (now - timedelta(hours=1)).isoformat()]
            for oid in ages}
    st = await s.get(BridgeState, "easyboss")
    st.lean_json = json.dumps({"day": now.astimezone(lean.WIB).date().isoformat(), "requests": 0,
                               "seen": seen, "alerts": []})
    await s.commit()

    result = await run(s, client)
    # 2 list pages + 2 reads each for the two oldest open orders (cap default 5, only 3 are open).
    assert {c[3] for c in details(up)} == {"stale", "mid", "freshish"}
    assert "done" not in {c[3] for c in details(up)}
    assert result["requests"] == 8
    rows = {r.order_id: r.source_status for r in (await s.scalars(select(BridgeOrder))).all()}
    assert rows["stale"] == rows["mid"] == rows["freshish"] == "IN_TRANSIT"
    assert rows["done"] == "DELIVERED"


@pytest.mark.asyncio
async def test_slow_refresh_caps_per_round_and_picks_oldest_first(env, monkeypatch):
    s, up, client = env
    monkeypatch.setenv("EASYBOSS_BRIDGE_OPEN_REFRESH", "2")
    now = datetime.now(timezone.utc)
    for i, oid in enumerate(("a", "b", "c")):
        await seed_imported(s, oid, "AWAITING_COLLECTION")
        await s.execute(BridgeOrder.__table__.update().where(BridgeOrder.order_id == oid).values(
            updated_at=now - timedelta(hours=9 - i)))
    await s.commit()
    up.orders = {oid: {"pkg": "p-" + oid, "status": "ship_success", "source": "IN_TRANSIT",
                       "dibuat": gmt(timedelta(hours=3))} for oid in ("a", "b", "c")}
    seen = {f"tiktokshop|shop|{oid}": [f"ship_success:p-{oid}", now.isoformat()] for oid in ("a", "b", "c")}
    st = await s.get(BridgeState, "easyboss")
    st.lean_json = json.dumps({"day": now.astimezone(lean.WIB).date().isoformat(), "requests": 0,
                               "seen": seen, "alerts": []})
    await s.commit()
    await run(s, client)
    assert [c[3] for c in details(up)] == ["a", "b"]
    up.calls.clear()
    # Immediately afterwards they are fresh; the remaining open order is still stale and is next.
    await run(s, client)
    assert [c[3] for c in details(up)] == ["c"]


@pytest.mark.asyncio
async def test_catch_up_refreshes_all_stale_open_orders_in_one_round(env):
    s, up, client = env
    now = datetime.now(timezone.utc)
    for oid in ("u", "v", "w"):
        await seed_imported(s, oid, "AWAITING_COLLECTION")
        await s.execute(BridgeOrder.__table__.update().where(BridgeOrder.order_id == oid).values(
            updated_at=now - timedelta(hours=8)))
    await s.commit()
    up.orders = {oid: {"pkg": "p-" + oid, "status": "ship_success", "source": "IN_TRANSIT",
                       "dibuat": gmt(timedelta(hours=4))} for oid in ("u", "v", "w")}
    seen = {f"tiktokshop|shop|{oid}": [f"ship_success:p-{oid}", now.isoformat()] for oid in ("u", "v", "w")}
    st = await s.get(BridgeState, "easyboss")
    st.lean_json = json.dumps({"day": now.astimezone(lean.WIB).date().isoformat(), "requests": 0,
                               "seen": seen, "alerts": []})
    await s.commit()
    result = await run(s, client, catch_up=True)
    assert {c[3] for c in details(up)} == {"u", "v", "w"}
    assert result["requests"] == 8

