"""Offline contract and integration tests for the Oct 30 / Nov 6 Shopee notices."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee as api
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import shopee_push
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn
from tenants.marketplace_erp.adapters.api.v1 import marketplace_erp_router as router
from starlette.requests import Request


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(api, "SHOPEE_LIVE_SYNC", True)
    monkeypatch.setattr(api, "SHOPEE_PARTNER_ID", "1")
    monkeypatch.setattr(api, "SHOPEE_PARTNER_KEY", "test")
    monkeypatch.setattr(api, "_DOC_POLL_DELAY", 0)
    monkeypatch.delenv("SHOPEE_SHIPPING_DOCUMENT_FLOW", raising=False)
    return SimpleNamespace(access_token="test-token", id_toko_eksternal="5")


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


@pytest.mark.parametrize("template", sorted(api.SHIPPING_DOCUMENT_TYPES))
def test_all_document_types_and_booking_boundary(template):
    body = api.shipping_document_job_body(template, booking_sn=[f"B{i}" for i in range(600)])
    assert len(body["booking_sn"]) == 600


@pytest.mark.parametrize("fields", [
    {}, {"package_list": ["P"], "booking_sn": ["B"]}, {"booking_sn": ["B"] * 601},
    {"package_list": [""]}, {"package_list": ["P", "P"]}, {"package_list": [{"package_number": "P"}]},
    {"unpackaged_sku_requests": [{"unpackaged_sku_id": "U", "quantity": 601}]},
    {"unpackaged_sku_requests": [{"unpackaged_sku_id": "U", "quantity": True}]},
])
def test_invalid_document_requests(fields):
    with pytest.raises(HTTPException):
        api.shipping_document_job_body("THERMAL_AIR_WAYBILL", **fields)


@pytest.mark.asyncio
async def test_job_flow_all_packages_and_expired_link_refresh(live, monkeypatch):
    calls = []
    polls = iter(["PROCESSING", "READY"])
    links = iter(["https://files.example/expired", "https://files.example/fresh"])

    async def request(session, account, path, **kw):
        calls.append((path, kw))
        if path == api._PATH_DOC_PARAM:
            return {"response": {"result_list": [{"order_sn": "S", "selectable_shipping_document_type": ["THERMAL_AIR_WAYBILL"]}]}}
        if path == api._PATH_ORDER_DETAIL:
            return {"response": {"order_list": [{"order_sn": "S", "package_list": [{"package_number": "P1"}, {"package_number": "P2"}]}]}}
        if path == api._PATH_DOC_JOB:
            assert kw["body"] == {"shipping_document_type": "THERMAL_AIR_WAYBILL", "package_list": ["P1", "P2"]}
            return {"response": {"job_id": "J", "success_id_list": ["P1", "P2"], "fail_list": []}}
        assert kw["body"] == {"job_id": "J"} and not kw.get("raw")
        if path == api._PATH_DOC_JOB_STATUS:
            return {"response": {"job_id": "J", "job_status": next(polls)}}
        assert path == api._PATH_DOC_FILELINK
        return {"file_link": next(links)}

    monkeypatch.setattr(api, "signed_shop_request", request)
    fetch = AsyncMock(side_effect=[None, b"%PDF-1.4 test"])
    monkeypatch.setattr(api, "_fetch_document_link", fetch)
    assert await api.unduh_resi_banyak(None, live, [("S", None)]) == b"%PDF-1.4 test"
    assert [p for p, _ in calls].count(api._PATH_DOC_JOB) == 1
    assert fetch.await_count == 2
    assert not {p for p, _ in calls} & {api._PATH_DOC_CREATE, api._PATH_DOC_DOWNLOAD}


@pytest.mark.asyncio
@pytest.mark.parametrize("create,status", [
    ({"job_id": "J", "success_id_list": ["P"], "fail_list": [{"package_number": "X"}]}, "READY"),
    ({"job_id": "J", "success_id_list": []}, "READY"),
    ({"success_id_list": ["P"]}, "READY"),
    ({"job_id": "J", "success_id_list": ["P"]}, "FAILED"),
    ({"job_id": "J", "success_id_list": ["P"]}, "EXPIRED"),
    ({"job_id": "J", "success_id_list": ["P"]}, "UNKNOWN"),
])
async def test_job_fail_closed(live, monkeypatch, create, status):
    async def request(s, a, path, **kw):
        if path == api._PATH_DOC_JOB:
            return {"response": create}
        assert path == api._PATH_DOC_JOB_STATUS
        return {"response": {"job_status": status}}
    monkeypatch.setattr(api, "signed_shop_request", request)
    with pytest.raises(HTTPException) as exc:
        await api.download_document_job(None, live, api.shipping_document_job_body("NORMAL_AIR_WAYBILL", package_list=["P"]))
    assert exc.value.status_code == 424


@pytest.mark.asyncio
async def test_job_poll_bounded_and_no_legacy_fallback(live, monkeypatch):
    monkeypatch.setattr(api, "_DOC_POLL_TRIES", 2)
    request = AsyncMock(side_effect=[{"response": {"job_id": "J", "success_id_list": ["P"]}},
                                    {"response": {"job_status": "PROCESSING"}},
                                    {"response": {"job_status": "PROCESSING"}}])
    monkeypatch.setattr(api, "signed_shop_request", request)
    with pytest.raises(HTTPException) as exc:
        await api.download_document_job(None, live, api.shipping_document_job_body("NORMAL_AIR_WAYBILL", package_list=["P"]))
    assert exc.value.status_code == 409 and request.await_count == 3


@pytest.mark.asyncio
async def test_downgrade_pages_deduplicates_and_preserves_watermarks(live, session, monkeypatch):
    account = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="Test", id_toko_eksternal="5"))
    account.access_token = "test"
    old = datetime(2026, 11, 1, tzinfo=timezone.utc)
    account.watermark_sinkron_pesanan = old
    account.sinkron_penuh_pesanan_at = old
    calls = []

    async def request(s, a, path, *, params=None, **kw):
        calls.append((path, params))
        if path == api._PATH_ORDER_LIST:
            return {"warning": "Search result might be outdated. Query v2.order.get_shipment_list for more up-to-date information"}
        if path.endswith("get_shipment_list"):
            if "cursor" not in params:
                return {"response": {"more": True, "next_cursor": "next", "order_list": [{"order_sn": "S"}]}}
            return {"response": {"more": False, "order_list": [{"order_sn": "S"}, {"order_sn": "T"}]}}
        assert path == api._PATH_ORDER_DETAIL
        return {"response": {"order_list": [{"order_sn": sn, "order_status": "READY_TO_SHIP"} for sn in params["order_sn_list"].split(",")]}}

    monkeypatch.setattr(api, "signed_shop_request", request)
    for _ in range(2):
        result = await services.sinkron_pesanan_akun(session, account)
        assert result["pulled"] == 2
        assert account.watermark_sinkron_pesanan == old and account.sinkron_penuh_pesanan_at == old
    assert len(await services.list_pesanan(session, platform="shopee")) == 2
    assert not any("time_from" in p for path, p in calls if path.endswith("get_shipment_list"))


@pytest.mark.asyncio
async def test_downgrade_repeating_cursor_rejected(live, monkeypatch):
    request = AsyncMock(side_effect=[{"response": {"warning": "Query get_shipment_list"}},
                                    {"response": {"more": True, "next_cursor": "x", "order_list": []}},
                                    {"response": {"more": True, "next_cursor": "x", "order_list": []}}])
    monkeypatch.setattr(api, "signed_shop_request", request)
    with pytest.raises(HTTPException) as exc:
        await api.sync_pesanan(None, live)
    assert exc.value.status_code == 424


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [3, 4, 15, 30])
async def test_signed_push_semantic_duplicates_survive_sessions(session, monkeypatch, code):
    monkeypatch.setenv("SHOPEE_PUSH_KEY", "test-key")
    now = int(datetime.now(timezone.utc).timestamp())
    tasks = BackgroundTasks()
    data = {"ordersn": "S", "package_number": "P", "status": "READY", "update_time": now}

    async def deliver(stamp, payload, shop=5):
        body = json.dumps({"shop_id": shop, "code": code, "timestamp": stamp, "data": payload}, indent=2).encode()
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        request = Request({"type": "http", "method": "POST", "path": "/", "scheme": "https", "server": ("test", 443), "query_string": b"", "headers": [(b"authorization", shopee_push.tanda_tangan("test-key", shopee_push.callback_url(), body).encode())]}, receive)
        return await router.terima_push_shopee(request, tasks, session)

    assert (await deliver(now, data)).status_code == 200
    session.expunge_all()  # dedup is in the database, not an in-process set
    assert (await deliver(now + 1, dict(reversed(list(data.items()))))).status_code == 200
    assert len(tasks.tasks) == 1
    await deliver(now + 2, {**data, "status": "FAILED", "update_time": now + 2})
    await deliver(now + 3, data, shop=6)
    assert len(tasks.tasks) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,limit", [(True, 0), (True, 10), (False, None)])
async def test_instant_limit_payload(live, monkeypatch, enabled, limit):
    request = AsyncMock(return_value={"response": {"update_result": "succeed"}})
    monkeypatch.setattr(api, "signed_shop_request", request)
    await api.instant_order_limit(None, live, enabled=enabled, limit=limit)
    body = request.call_args.kwargs["body"]
    assert body == ({"is_order_limit_enabled": True, "order_count_limit": limit} if enabled else {"is_order_limit_enabled": False})


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,limit", [(True, None), (True, -1), (True, True), (False, 0), ("true", 1)])
async def test_invalid_instant_limit_never_calls_provider(live, monkeypatch, enabled, limit):
    request = AsyncMock()
    monkeypatch.setattr(api, "signed_shop_request", request)
    with pytest.raises(HTTPException):
        await api.instant_order_limit(None, live, enabled=enabled, limit=limit)
    request.assert_not_called()


@pytest.mark.asyncio
async def test_download_uses_pinned_ip_tls_and_no_provider_auth(monkeypatch):
    import socket
    import urllib3
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    class Response:
        status = 200
        def read(self, size):
            return b"%PDF-1.4 mock"
        def close(self):
            pass
    class Pool:
        def __init__(self, host, port, **kw):
            assert host == "8.8.8.8" and kw["assert_hostname"] == kw["server_hostname"] == "files.example"
        def request(self, method, path, **kw):
            calls.append(kw)
            assert method == "GET" and path == "/file?signature=example"
            return Response()
        def close(self):
            pass
    monkeypatch.setattr(urllib3, "HTTPSConnectionPool", Pool)
    assert await api._fetch_document_link("https://files.example/file?signature=example") == b"%PDF-1.4 mock"
    assert calls[0]["headers"] == {"Host": "files.example"} and calls[0]["redirect"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("link", ["file:///etc/passwd", "https://user:pass@files.example/f", "http://127.0.0.1/f", "http://169.254.169.254/f", "http://files.example/label", "https://files.example:22/f"])
async def test_download_rejects_unsafe_links(link):
    with pytest.raises(HTTPException):
        await api._fetch_document_link(link)


def test_payment_keeps_new_fee_without_double_deducting_escrow():
    from decimal import Decimal
    row = api.normalisasi_escrow("S", None, None, {"order_income": {
        "escrow_amount": "950", "escrow_amount_after_adjustment": "940", "sspl_fee": "10",
    }})
    assert row["jumlah_cair"] == row["escrow"] == Decimal("940")
    assert json.loads(row["rincian"])["sspl_fee"] == "10"


@pytest.mark.asyncio
async def test_normal_sync_is_complete_after_downgrade(live, monkeypatch):
    request = AsyncMock(return_value={"response": {"more": False, "order_list": []}})
    monkeypatch.setattr(api, "signed_shop_request", request)
    rows = await api.sync_pesanan(None, live)
    assert rows.complete and rows == []


@pytest.mark.asyncio
async def test_total_document_deadline_covers_parameter_lookup(live, monkeypatch):
    import asyncio
    monkeypatch.setattr(api, "_DOC_BATAS_DETIK", .01)
    async def slow(*a, **kw):
        await asyncio.sleep(1)
    monkeypatch.setattr(api, "signed_shop_request", slow)
    with pytest.raises(HTTPException) as exc:
        await api.unduh_resi_banyak(None, live, [("S", None)])
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_instant_read_and_feature_rejection(live, monkeypatch):
    request = AsyncMock(side_effect=api.ShopeeAPIError("instant", "logistics.shop_not_in_feature_toggle", "not eligible"))
    monkeypatch.setattr(api, "signed_shop_request", request)
    with pytest.raises(api.ShopeeAPIError) as exc:
        await api.instant_order_limit(None, live)
    assert exc.value.error == "logistics.shop_not_in_feature_toggle"
    assert request.call_args.args[2].endswith("get_order_count_limit_outside_instant_operating_time")


@pytest.mark.asyncio
async def test_signed_http_job_integration(live, monkeypatch):
    """Exercise real signing/JSON/error handling with mock Shopee HTTP transport."""
    import hashlib
    import hmac
    import requests
    from urllib.parse import parse_qs, urlsplit
    calls = []
    monkeypatch.setattr(api, "pastikan_token_segar", AsyncMock())
    monkeypatch.setattr(api, "SHOPEE_ENV", "sandbox")

    class Response:
        status_code = 200
        def __init__(self, payload):
            self.payload = payload
        def json(self):
            return self.payload

    def transport(url, **kw):
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        expected = hmac.new(b"test", f"1{parsed.path}{query['timestamp'][0]}test-token5".encode(), hashlib.sha256).hexdigest()
        assert query["sign"] == [expected] and kw["allow_redirects"] is False
        assert "sandbox" in parsed.hostname
        calls.append(parsed.path)
        if parsed.path == api._PATH_DOC_PARAM:
            return Response({"response": {"result_list": [{"order_sn": "S", "selectable_shipping_document_type": ["NORMAL_AIR_WAYBILL"]}]}})
        if parsed.path == api._PATH_ORDER_DETAIL:
            return Response({"response": {"order_list": [{"order_sn": "S", "package_list": [{"package_number": "P"}]}]}})
        if parsed.path == api._PATH_DOC_JOB:
            assert kw["json"] == {"shipping_document_type": "NORMAL_AIR_WAYBILL", "package_list": ["P"]}
            return Response({"response": {"job_id": "J", "success_id_list": ["P"]}})
        assert kw["json"] == {"job_id": "J"}
        if parsed.path == api._PATH_DOC_JOB_STATUS:
            return Response({"response": {"job_id": "J", "job_status": "READY"}})
        assert parsed.path == api._PATH_DOC_FILELINK
        return Response({"file_link": "https://files.example/label"})

    monkeypatch.setattr(requests, "get", transport)
    monkeypatch.setattr(requests, "post", transport)
    monkeypatch.setattr(api, "_fetch_document_link", AsyncMock(return_value=b"%PDF-1.4 integration"))
    assert await api.unduh_resi_banyak(None, live, [("S", None)]) == b"%PDF-1.4 integration"
    assert calls == [api._PATH_DOC_PARAM, api._PATH_ORDER_DETAIL, api._PATH_DOC_JOB, api._PATH_DOC_JOB_STATUS, api._PATH_DOC_FILELINK]


@pytest.mark.asyncio
async def test_signed_http_provider_error_is_not_a_download(live, monkeypatch):
    import requests
    monkeypatch.setattr(api, "pastikan_token_segar", AsyncMock())
    class Response:
        status_code = 200
        def json(self):
            return {"error": "logistics.invalid_job_id", "message": "invalid", "request_id": "mock-id"}
    monkeypatch.setattr(requests, "post", lambda *a, **kw: Response())
    with pytest.raises(api.ShopeeAPIError) as exc:
        await api.download_document_job(None, live, api.shipping_document_job_body("NORMAL_AIR_WAYBILL", package_list=["P"]))
    assert exc.value.request_id == "mock-id"


@pytest.mark.asyncio
async def test_malformed_shipment_response_preserves_watermark(live, monkeypatch):
    request = AsyncMock(side_effect=[{"warning": "get_shipment_list"}, {"response": {}}])
    monkeypatch.setattr(api, "signed_shop_request", request)
    with pytest.raises(HTTPException) as exc:
        await api.sync_pesanan(None, live)
    assert exc.value.status_code == 424
