"""Copied Shopee endpoint contracts; every provider request is mocked."""
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path
import json

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1 import commerce_router as router
from tenants.marketplace_erp.modules.marketplace_erp.application import commerce_schemas as schema, management_schemas, services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee_commerce as adapter, erp_shopee_management as management
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import KatalogShopee, Pesanan, Produk, ProdukListing


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


def test_write_validation():
    for value in ({}, {"shop_name": " "}, {"shop_logo": "javascript:bad"}, {"unknown": 1}):
        with pytest.raises(ValidationError):
            schema.ShopProfileEdit(**value)
    with pytest.raises(ValidationError):
        schema.ChannelEdit()
    with pytest.raises(ValidationError):
        schema.OrderNote(note="x" * 501)
    with pytest.raises(ValidationError):
        schema.ModelAdd(models=[{"tier_index": [-1], "price": "1"}])
    with pytest.raises(ValidationError):
        schema.ModelAdd(models=[{"tier_index": [1], "price": "NaN"}])
    with pytest.raises(ValidationError):
        schema.HolidayEdit(holiday_mode_on=True, holiday_mode_start_time=3601, holiday_mode_end_time=7199)
    assert schema.HolidayEdit(holiday_mode_on=True, holiday_mode_start_time=3600, holiday_mode_end_time=7199)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{}, {"error": ""}, {"request_id": "r"}, {"error": "denied", "request_id": "r"}])
async def test_missing_write_confirmation_never_reports_success(monkeypatch, response):
    monkeypatch.setattr(adapter.provider, "signed_shop_request", AsyncMock(return_value=response))
    with pytest.raises(adapter.provider.ShopeeAPIError):
        await adapter.acknowledged(None, None, "/api/v2/order/set_note", {})


@pytest.mark.asyncio
async def test_new_models_live_indices_and_exact_stock_payload(monkeypatch):
    monkeypatch.setattr(adapter.workflows, "read", AsyncMock(return_value={"tier_variation": [{"option_list": [{"option": "A"}, {"option": "B"}]}], "model": [{"tier_index": [0], "model_id": 9}]}))
    request = AsyncMock(return_value={"response": {"model": [{"tier_index": [1], "model_id": 10}]}, "request_id": "r"})
    monkeypatch.setattr(adapter.provider, "signed_shop_request", request)
    await adapter.add_models(None, None, 7, schema.ModelAdd(models=[{"tier_index": [1], "price": "123.25", "stock": 4, "sku": "B"}]))
    assert request.call_args.args[2] == "/api/v2/product/add_model"
    assert request.call_args.kwargs == {"method": "POST", "body": {"item_id": 7, "model_list": [{"tier_index": [1], "original_price": 123.25, "seller_stock": [{"stock": 4}], "model_sku": "B"}]}}
    for index in [0, 2]:
        with pytest.raises(HTTPException):
            await adapter.add_models(None, None, 7, schema.ModelAdd(models=[{"tier_index": [index], "price": 1}]))
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_delete_model_rejects_foreign_or_last_variant(monkeypatch):
    monkeypatch.setattr(management, "models", AsyncMock(return_value=[{"model_id": 9}]))
    request = AsyncMock()
    monkeypatch.setattr(adapter.provider, "signed_shop_request", request)
    for model in [9, 10]:
        with pytest.raises(HTTPException):
            await adapter.delete_model(None, None, 7, model)
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_income_top_level_envelope_pending_and_cursor(monkeypatch):
    request = AsyncMock(return_value={"income_detail_list": {"next_page": {"cursor": "next"}, "income_detail_list_item": [{"order_sn": "A", "released_amount": None}]}, "request_id": "r"})
    monkeypatch.setattr(adapter.provider, "signed_shop_request", request)
    result = await adapter.income(None, None, date(2026, 9, 1), date(2026, 9, 2), 2, "", 30)
    assert result["next_cursor"] == "next" and result["items"][0]["released_amount"] is None
    assert request.call_args.kwargs["params"] == {"date_from": "2026-09-01", "date_to": "2026-09-02", "income_status": 2, "cursor": "", "page_size": 30}
    with pytest.raises(adapter.provider.ShopeeAPIError):
        await adapter.income(None, None, date(2026, 9, 1), date(2026, 9, 2), 2, "next", 30)
    request.return_value = {"response": {"income_detail_list": []}}
    with pytest.raises(adapter.provider.ShopeeAPIError):
        await adapter.income(None, None, date(2026, 9, 1), date(2026, 9, 2), 2, "", 30)


@pytest.mark.asyncio
async def test_order_reads_reject_wrong_target_and_keep_package(monkeypatch):
    read = AsyncMock(return_value={"order_sn": "OTHER", "tracking_info": []})
    monkeypatch.setattr(adapter.workflows, "read", read)
    with pytest.raises(adapter.provider.ShopeeAPIError):
        await adapter.tracking(None, None, "A", "PACKAGE")
    assert read.call_args.args[3] == {"order_sn": "A", "package_number": "PACKAGE"}
    read.return_value = {"order_sn": "OTHER", "order_income": {}}
    with pytest.raises(adapter.provider.ShopeeAPIError):
        await adapter.order_income(None, None, "A")


@pytest.mark.asyncio
async def test_compulsory_channel_and_preparation_bounds(monkeypatch):
    monkeypatch.setattr(adapter, "settings_read", AsyncMock(return_value={"logistics_channel_list": [{"logistics_channel_id": 1, "force_enable": True, "auto_call_driver_setting": {"auto_call_driver_eligible": True, "preparation_time_limit": {"min_preparation_time": 10, "max_preparation_time": 30}}}]}))
    write = AsyncMock(return_value={"error": "", "request_id": "r"})
    monkeypatch.setattr(adapter.provider, "signed_shop_request", write)
    for ident, body in [(1, {"enabled": False}), (2, {"enabled": True}), (1, {"auto_call_driver_setting": {"auto_call_driver_enabled": True, "preparation_time": 40}})]:
        with pytest.raises(HTTPException):
            await adapter.update_channel(None, None, ident, schema.ChannelEdit(**body))
    write.assert_not_awaited()
    await adapter.update_channel(None, None, 1, schema.ChannelEdit(cod_enabled=True))
    assert write.call_args.kwargs["body"] == {"logistics_channel_id": 1, "cod_enabled": True}


@pytest.mark.asyncio
async def test_gmv_omits_optional_campaign_and_uses_returned_identity(monkeypatch):
    monkeypatch.setattr(management_schemas, "today", lambda: date(2026, 10, 10))
    request = AsyncMock(return_value={"response": {"campaign_id": 7, "report": {"expense": 0}}})
    monkeypatch.setattr(management.provider, "signed_shop_request", request)
    result = await management.gmv_performance(None, None, None, date(2026, 10, 1), date(2026, 10, 10))
    assert result["campaign_id"] == "7"
    assert "campaign_id" not in request.call_args.kwargs["body"]


@pytest.mark.asyncio
async def test_account_access_denied_before_provider_call(monkeypatch):
    read_order = AsyncMock(return_value=SimpleNamespace(platform="shopee", akun_id="foreign", status_marketplace="SHIPPED"))
    monkeypatch.setattr(router.services, "get_pesanan", read_order)
    monkeypatch.setattr(router, "pastikan_akses_akun", AsyncMock(side_effect=HTTPException(403, "Ditolak")))
    provider = AsyncMock()
    monkeypatch.setattr(router.adapter, "tracking", provider)
    with pytest.raises(HTTPException) as exc:
        await router.tracking("order", None, None, SimpleNamespace(role="staff"))
    assert exc.value.status_code == 403
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_product_delete_preserves_stock_history_and_other_listings(session, monkeypatch):
    a = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="A"))
    p = Produk(sku_induk="SKU", nama="Produk", stok=5)
    session.add(p)
    await session.flush()
    catalog = KatalogShopee(akun_id=a.id, item_id="7", nama="Produk")
    listings = [ProdukListing(produk_id=p.id, akun_id=a.id, platform="shopee", id_eksternal=x, aktif=True) for x in ["7:9", "70:9"]]
    session.add_all([catalog, *listings])
    await session.commit()
    monkeypatch.setattr(router, "listing", AsyncMock(return_value=(catalog, a)))
    monkeypatch.setattr(router.adapter, "delete_product", AsyncMock(return_value={"ok": True, "warnings": []}))
    await router.delete_product(catalog.id, session, None)
    assert catalog.status == "SELLER_DELETE" and p.stok == 5
    assert not listings[0].aktif and listings[1].aktif
    assert len((await session.execute(select(KatalogShopee))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_order_note_only_updates_after_confirmation(session, monkeypatch):
    a = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="A"))
    p = Pesanan(platform="shopee", akun_id=a.id, id_eksternal="A", status_marketplace="PROCESSED", detail_json='{"note":"lama","message_to_seller":"putih"}')
    session.add(p)
    await session.commit()
    monkeypatch.setattr(router, "order", AsyncMock(return_value=(p, a)))
    write = AsyncMock(side_effect=HTTPException(502, "Network"))
    monkeypatch.setattr(router.adapter, "acknowledged", write)
    with pytest.raises(HTTPException):
        await router.note(p.id, schema.OrderNote(note="baru"), session, None)
    assert '"lama"' in p.detail_json
    write.side_effect = None
    write.return_value = {"ok": True, "warnings": []}
    await router.note(p.id, schema.OrderNote(note="baru"), session, None)
    assert '"baru"' in p.detail_json and '"putih"' in p.detail_json


@pytest.mark.asyncio
async def test_income_real_idr_sample_and_released_period_limit(monkeypatch):
    data = json.loads((Path(__file__).parent / "fixtures/shopee_income_detail.json").read_text())
    request = AsyncMock(return_value=data)
    monkeypatch.setattr(adapter.provider, "signed_shop_request", request)
    result = await adapter.income(None, None, date(2026, 9, 1), date(2026, 9, 14), 1, "", 30)
    assert result["items"] == data["income_detail_list"]["list"]
    for start, end in [(date(2026, 9, 1), date(2026, 9, 1)), (date(2026, 9, 1), date(2026, 9, 30))]:
        with pytest.raises(HTTPException):
            await adapter.income(None, None, start, end, 1, "", 30)
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_initial_variants_require_complete_matrix_and_no_existing_models(monkeypatch):
    tiers = [{"name": "Warna", "options": [{"option": "A"}, {"option": "B"}]}]
    with pytest.raises(ValidationError):
        schema.ModelsInit(tiers=tiers, models=[{"tier_index": [0], "price": 10}])
    payload = schema.ModelsInit(tiers=tiers, models=[{"tier_index": [i], "price": 10} for i in range(2)])
    monkeypatch.setattr(management, "item", AsyncMock(return_value={"has_model": True}))
    request = AsyncMock(return_value={"response": {"model": [{"tier_index": [i], "model_id": i + 1} for i in range(2)]}})
    monkeypatch.setattr(adapter.provider, "signed_shop_request", request)
    with pytest.raises(HTTPException):
        await adapter.init_models(None, None, 7, payload)
    request.assert_not_awaited()
    monkeypatch.setattr(management, "item", AsyncMock(return_value={"has_model": False}))
    await adapter.init_models(None, None, 7, payload)
    assert request.call_args.args[2] == "/api/v2/product/init_tier_variation"
    assert request.call_args.kwargs["body"]["model"][0]["seller_stock"] == [{"stock": 0}]


@pytest.mark.asyncio
async def test_hourly_campaign_shape_method_and_identity(monkeypatch):
    request = AsyncMock(return_value={"response": [{"campaign_list": [{"campaign_id": 7, "metrics_list": [{"hour": 20, "expense": None}, {"hour": 1, "expense": 0}]}]}]})
    monkeypatch.setattr(adapter.provider, "signed_shop_request", request)
    result = await adapter.hourly_ads(None, None, date(2026, 9, 1), 7)
    assert [r["hour"] for r in result["items"]] == [1, 20]
    assert request.call_args.kwargs == {"params": {"performance_date": "01-09-2026", "campaign_id_list": "7"}}
    with pytest.raises(adapter.provider.ShopeeAPIError):
        await adapter.hourly_ads(None, None, date(2026, 9, 1), 8)


@pytest.mark.asyncio
async def test_address_config_requires_live_shop_address(monkeypatch):
    monkeypatch.setattr(adapter, "settings_read", AsyncMock(return_value={"address_list": [{"address_id": 7}]}))
    request = AsyncMock(return_value={"error": "", "request_id": "r"})
    monkeypatch.setattr(adapter.provider, "signed_shop_request", request)
    with pytest.raises(HTTPException):
        await adapter.address_config(None, None, schema.AddressConfig(address_id=8, address_type=["DEFAULT_ADDRESS"]))
    request.assert_not_awaited()
    await adapter.address_config(None, None, schema.AddressConfig(address_id=7, address_type=["DEFAULT_ADDRESS", "RETURN_ADDRESS"]))
    assert request.call_args.kwargs["body"] == {"address_type_config": {"address_id": 7, "address_type": ["DEFAULT_ADDRESS", "RETURN_ADDRESS"]}}


@pytest.mark.asyncio
@pytest.mark.parametrize("section,response", [("jasa-kirim", {"logistics_channel_list": [None]}), ("alamat", {"address_list": [{"address_id": 1, "address_type": None}]}), ("profil", {"shop_name": None})])
async def test_malformed_settings_raise_provider_error_not_internal_error(monkeypatch, section, response):
    monkeypatch.setattr(adapter.workflows, "read", AsyncMock(return_value=response))
    with pytest.raises(adapter.provider.ShopeeAPIError):
        await adapter.settings_read(None, None, section)


def test_model_confirmation_requires_unique_positive_ids_and_exact_combinations():
    payload = schema.ModelAdd(models=[{"tier_index": [0], "price": 1}, {"tier_index": [1], "price": 1}])
    assert adapter.confirmed_models([{"model_id": 1, "tier_index": [0]}, {"model_id": 2, "tier_index": [1]}], payload)
    for rows in ([None, None], [{"model_id": 1, "tier_index": [0]}, {"model_id": 1, "tier_index": [1]}], [{"model_id": 1, "tier_index": [0]}], [{"model_id": 0, "tier_index": [0]}, {"model_id": 2, "tier_index": [1]}]):
        assert not adapter.confirmed_models(rows, payload)


@pytest.mark.asyncio
async def test_confirmed_write_retains_success_when_local_commit_fails():
    session = SimpleNamespace(commit=AsyncMock(side_effect=RuntimeError("db unavailable")), rollback=AsyncMock())
    result = await router.save_confirmed(session, {"ok": True, "request_id": "confirmed", "warnings": []})
    assert result["ok"] and result["request_id"] == "confirmed" and result["warnings"]
    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_income_http_query_coerces_status_and_rejects_out_of_range(monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[router.DB.dependency] = lambda: None
    app.dependency_overrides[router.ADMIN.dependency] = lambda: SimpleNamespace(role="admin")
    monkeypatch.setattr(router, "account", AsyncMock(return_value="account"))
    read = AsyncMock(return_value={"items": [], "ada_lagi": False, "next_cursor": ""})
    monkeypatch.setattr(adapter, "income", read)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        params = {"dari": "2026-09-28", "sampai": "2026-10-11", "cursor": ""}
        for status in ("1", "2"):
            response = await client.get("/akun/shop/pendapatan-shopee", params={**params, "income_status": status})
            assert response.status_code == 200, response.text
            assert read.call_args.args[4] == int(status)
        for status in ("0", "3", "invalid", "1.5"):
            response = await client.get("/akun/shop/pendapatan-shopee", params={**params, "income_status": status})
            assert response.status_code == 422
        assert read.await_count == 2


@pytest.mark.asyncio
async def test_income_accepts_wrapped_contract_without_changing_records(monkeypatch):
    fixture = json.loads((Path(__file__).parent / "fixtures/shopee_income_detail.json").read_text())
    wrapped = {"request_id": "wrapped", "response": {"income_detail_list": fixture["income_detail_list"]}}
    monkeypatch.setattr(adapter.provider, "signed_shop_request", AsyncMock(return_value=wrapped))
    result = await adapter.income(None, None, date(2026, 9, 28), date(2026, 10, 10), 2, "", 30)
    assert result["items"] == fixture["income_detail_list"]["list"]
    assert result["next_cursor"] == fixture["income_detail_list"]["next_page"]["cursor"]


@pytest.mark.asyncio
async def test_income_diagnostics_log_shape_without_private_values(monkeypatch, caplog):
    response = {"request_id": "trace-id", "access_token": "SECRET-TOKEN", "income_detail_list": {"list": [{"order_sn": "PRIVATE-ORDER", "released_amount": 12345}], "next_page": "PRIVATE-CURSOR"}}
    monkeypatch.setattr(adapter.provider, "signed_shop_request", AsyncMock(return_value=response))
    with pytest.raises(adapter.provider.ShopeeAPIError):
        await adapter.income(None, None, date(2026, 9, 28), date(2026, 10, 10), 2, "", 30)
    assert "reason=pagination_envelope" in caplog.text and "trace-id" in caplog.text
    for value in ("SECRET-TOKEN", "PRIVATE-ORDER", "PRIVATE-CURSOR", "12345"):
        assert value not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("list_field", ["list", "income_detail_list_item"])
async def test_income_direct_response_preserves_rows_and_pagination(monkeypatch, list_field):
    response = {"error": "", "request_id": "direct", "response": {list_field: [{"order_sn": "ORDER", "released_amount": None}], "next_page": {"cursor": "next"}}}
    monkeypatch.setattr(adapter.provider, "signed_shop_request", AsyncMock(return_value=response))
    result = await adapter.income(None, None, date(2026, 9, 28), date(2026, 10, 10), 2, "", 30)
    assert result["items"] == response["response"][list_field]
    assert result["next_cursor"] == "next" and result["ada_lagi"]
    response["response"].pop("next_page")
    result = await adapter.income(None, None, date(2026, 9, 28), date(2026, 10, 10), 2, "", 30)
    assert result["items"] == response["response"][list_field]
    assert result["pagination_known"] is False and result["ada_lagi"] is None and result["warnings"]
    response["response"]["next_page"] = "malformed"
    with pytest.raises(adapter.provider.ShopeeAPIError):
        await adapter.income(None, None, date(2026, 9, 28), date(2026, 10, 10), 2, "", 30)


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [[], [{"order_sn": "ORDER", "released_amount": None}]])
async def test_income_live_layout_without_pagination_does_not_claim_complete(monkeypatch, rows):
    monkeypatch.setattr(adapter.provider, "signed_shop_request", AsyncMock(return_value={"error": "", "request_id": "live-shape", "response": {"list": rows}}))
    result = await adapter.income(None, None, date(2026, 9, 28), date(2026, 10, 10), 2, "", 30)
    assert result["items"] == rows and result["warnings"]
    assert result["ada_lagi"] is None and not result["pagination_known"]
