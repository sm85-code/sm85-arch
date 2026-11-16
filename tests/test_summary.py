"""Summary reconciliation, source identity, missing data and permission regression."""
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select

from tenants.marketplace_erp.modules.marketplace_erp.application import akses, summary
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import RoleHalaman
from tenants.marketplace_erp.adapters.api.v1.summary_router import router, scope

DAY = date(2026, 11, 16)
NOW = datetime(2026, 11, 16, 17, tzinfo=timezone.utc)


def row(sn="A", **kwargs):
    return {"platform": "shopee", "id_eksternal": sn, "akun_id": "s1", "external_shop": "market-s1",
            "status": "completed", "status_marketplace": "COMPLETED", "detail_json": '{"currency":"IDR"}',
            "bridge_platform": None, "toko_id": None, "shop_id": None, "order_id": None,
            "source_status": None, "snapshot_json": None, "ordered": datetime(2026, 11, 16, 3, tzinfo=timezone.utc),
            "dipesan_at": NOW, "updated_at": NOW, "source_updated": None, "shop_name": "Toko A",
            "units": 2, "sales": Decimal("100.10"), "line_count": 1, **kwargs}


def eb(sn="E", **kwargs):
    snapshot = {"currency": "IDR", "complete": True, "status": "completed", "items": [
        {"item_id": "a", "quantity": 3, "line_total": "100.01"},
        {"item_id": "b", "quantity": 2, "line_total": "200.02"}]}
    return row(sn, platform="manual", akun_id=None, bridge_platform="tiktokshop", toko_id="ebshop",
               shop_id="native-eb", order_id=sn, source_status="DELIVERED", snapshot_json=json.dumps(snapshot),
               **kwargs)


def test_jakarta_period_boundaries_and_limit():
    assert summary.period("hari_ini", now=NOW) == (date(2026, 11, 17), date(2026, 11, 17))
    assert summary.period("kemarin", now=NOW) == (DAY, DAY)
    assert summary.period("7", now=NOW)[0] == date(2026, 11, 11)
    assert summary.period("30", now=NOW)[0] == date(2026, 10, 19)
    assert summary.period("bulan_ini", now=NOW)[0] == date(2026, 11, 1)
    assert summary.period("bulan_lalu", now=NOW) == (date(2026, 10, 1), date(2026, 10, 31))
    for a, b in [(DAY, date(2025, 1, 1)), (date(2020, 1, 1), DAY)]:
        with pytest.raises(HTTPException):
            summary.period("khusus", a, b)


def test_decimal_reconciliation_dedup_shop_identity_and_cancellation():
    rows = [row(), row(), row("B", status="cancelled", status_marketplace="CANCELLED"), eb(),
            row("A", akun_id="s2", external_shop="native-s2", sales=Decimal("10.01"))]
    result = summary.project(rows, DAY, DAY)
    t = result["totals"]
    assert t["orders"] == 4
    assert t["units"] == 9  # cancelled order excluded
    assert t["gross"] == Decimal("510.24")
    assert t["cancellation_value"] == Decimal("100.10")
    assert t["net"] is None and t["return_value"] is None and t["returns"] is None
    assert result["quality"]["deduplicated"] == 1
    assert sum(g["gross"] for g in result["marketplaces"]) == t["gross"]
    assert sum(r["orders"] for r in result["trend"]) == t["orders"]
    assert sum(r["gross"] for r in result["trend"]) == t["gross"]
    for g in result["marketplaces"]:
        assert sum(s["orders"] for s in g["shops"]) == g["orders"]
        assert sum((s["gross"] for s in g["shops"]), Decimal(0)) == g["gross"]


def test_all_filters_pagination_and_missing_money():
    rows = [row(), row("B", sales=None), eb()]
    r = summary.project(rows, DAY, DAY)
    assert r["totals"]["gross"] is None
    assert r["totals"]["known_sales"] == Decimal("400.13")
    assert r["quality"]["missing_sales"] == 1
    assert summary.project(rows, DAY, DAY, platform="tiktokshop")["totals"]["orders"] == 1
    assert summary.project(rows, DAY, DAY, shop="s1")["totals"]["orders"] == 2
    assert summary.project(rows, DAY, DAY, status="dibatalkan")["totals"]["orders"] == 0
    assert len(summary.project(rows, DAY, DAY, page=2, page_size=1)["orders"]) == 1
    assert summary.project(rows, date(2026, 11, 17), date(2026, 11, 17))["totals"]["orders"] == 0


def test_native_status_wins_without_mutating_source_and_duplicate_lines_not_counted():
    r = eb()
    r["source_status"] = "CANCELLED"
    assert summary.project([r], DAY, DAY)["totals"]["cancelled"] == 1
    assert r["status"] == "completed"
    assert summary.phase("tiktokshop", "UNRECOGNIZED", "to_ship", {}) == "tidak_dikenal"
    source = json.loads(r["snapshot_json"])
    source["items"][1]["item_id"] = "a"
    r["snapshot_json"] = json.dumps(source)
    assert summary.project([r], DAY, DAY)["totals"]["gross"] is None
    r["source_status"] = "RETURNED"
    t = summary.project([r], DAY, DAY)["totals"]
    assert t["units"] == 0 and t["net"] is None


@pytest.mark.asyncio
async def test_permission_defaults_grant_revoke_and_no_other_permission_changes(sesi_peran):
    session = sesi_peran
    before = [(r.role_id, r.halaman, r.tingkat) for r in (await session.scalars(select(RoleHalaman))).all()]
    assert await scope(session, SimpleNamespace(role="admin", id="admin")) is None
    for role in ("owner", "staff"):
        with pytest.raises(HTTPException) as exc:
            await scope(session, SimpleNamespace(role=role, id=role))
        assert exc.value.status_code == 403
    assert ("summary", "Summary", "harian", ("baca",)) in akses.HALAMAN
    session.add(RoleHalaman(role_id="staff", halaman="summary", tingkat="baca"))
    await session.flush()
    assert await scope(session, SimpleNamespace(role="staff", id="staff")) == []
    await session.delete(await session.get(RoleHalaman, ("staff", "summary")))
    await session.flush()
    after = [(r.role_id, r.halaman, r.tingkat) for r in (await session.scalars(select(RoleHalaman))).all()]
    assert sorted(after) == sorted(before)
    assert all(route.methods == {"GET"} for route in router.routes)


@pytest.mark.asyncio
async def test_query_read_only_and_no_hidden_writes(sesi_peran):
    session = sesi_peran
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(session.bind.sync_engine, "before_cursor_execute", capture)
    try:
        r = await summary.read(session, DAY, DAY, allowed=[])
        assert r["totals"]["orders"] == 0
        assert all(s.lstrip().upper().startswith("SELECT") for s in statements)
    finally:
        event.remove(session.bind.sync_engine, "before_cursor_execute", capture)


@pytest.mark.asyncio
async def test_summary_http_permission_filters_and_write_methods(sesi_peran):
    import httpx
    from fastapi import FastAPI
    from tenants.marketplace_erp.adapters.api.v1.summary_router import readonly_session
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
    session = sesi_peran
    app = FastAPI()
    app.include_router(router)
    current = SimpleNamespace(role="staff", id="reader")
    async def db():
        yield session
    async def user():
        return current
    app.dependency_overrides[readonly_session] = db
    app.dependency_overrides[get_current_user_marketplace_erp] = user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get('/summary')).status_code == 403
        session.add(RoleHalaman(role_id='staff', halaman='summary', tingkat='baca'))
        await session.flush()
        assert (await client.get('/summary')).json()['total_orders'] == 0
        assert (await client.get('/summary?shop=some-other-store')).status_code == 403
        assert (await client.get('/summary?status=invalid')).status_code == 422
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            assert (await client.request(method, '/summary')).status_code == 405
        current.role = 'admin'
        assert (await client.get('/summary?preset=khusus&start=2026-11-16&end=2026-11-16')).status_code == 200
        assert (await client.get('/summary')).headers['cache-control'] == 'no-store, private'
