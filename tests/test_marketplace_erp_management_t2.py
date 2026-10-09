from datetime import date
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from tenants.marketplace_erp.modules.marketplace_erp.application import management_schemas as schemas
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee_management as management, erp_shopee_insights as insights


@pytest.mark.asyncio
async def test_diagnosis_official_excellent_sample_and_incomplete(monkeypatch):
    request = AsyncMock(return_value={"response": {"success_item_list": [{"item_id": 7, "quality_level": 3}]}})
    monkeypatch.setattr(insights.provider, "signed_shop_request", request)
    result = await insights.diagnosis(None, None, [7])
    assert result["success_item_list"][0]["unfinished_task"] == []
    assert request.call_args.kwargs == {"method": "POST", "body": {"item_id_list": [7]}}
    with pytest.raises(insights.provider.ShopeeAPIError):
        await insights.diagnosis(None, None, [7, 8])


@pytest.mark.asyncio
async def test_parent_weight_requires_explicit_confirmation(monkeypatch):
    monkeypatch.setattr(management, "item", AsyncMock(return_value={"has_model": True}))
    request = AsyncMock(return_value={"response": {"item_id": 7}, "request_id": "r"})
    monkeypatch.setattr(management.provider, "signed_shop_request", request)
    with pytest.raises(HTTPException) as error:
        await management.update_item(None, None, 7, schemas.ItemEdit(weight="1.5"))
    assert error.value.status_code == 409
    request.assert_not_awaited()
    await management.update_item(None, None, 7, schemas.ItemEdit(weight="1.5", apply_to_all_models=True))
    assert request.call_args.kwargs["body"] == {"item_id": 7, "weight": 1.5}


@pytest.mark.asyncio
async def test_model_acknowledgement_and_foreign_model_rejection(monkeypatch):
    monkeypatch.setattr(management, "models", AsyncMock(return_value=[{"model_id": 9, "tier_index": [0]}]))
    request = AsyncMock(return_value={"error": "", "request_id": "r"})
    monkeypatch.setattr(management.provider, "signed_shop_request", request)
    result = await management.update_variants(None, None, 7, schemas.ModelsEdit(model=[{"model_id": 9, "model_sku": "SKU"}]))
    assert result["ok"] and request.call_args.kwargs["body"]["model"] == [{"model_id": 9, "model_sku": "SKU"}]
    with pytest.raises(HTTPException):
        await management.update_variants(None, None, 7, schemas.ModelsEdit(model=[{"model_id": 10, "model_sku": "SKU"}]))
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_gmv_ineligible_never_creates(monkeypatch):
    monkeypatch.setattr(management, "eligibility", AsyncMock(return_value={"is_eligible": False, "reason": "whitelist"}))
    request = AsyncMock()
    monkeypatch.setattr(management.provider, "signed_shop_request", request)
    payload = schemas.GmvCreate(daily_budget=10000, start_date=schemas.today(), reference_id=uuid4())
    with pytest.raises(HTTPException) as error:
        await management.gmv_mutation(None, None, payload, "create")
    assert error.value.status_code == 409
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_gmv_report_uses_post_and_calendar_bounds(monkeypatch):
    monkeypatch.setattr(schemas, "today", lambda: date(2026, 10, 9))
    request = AsyncMock(return_value={"response": {"campaign_id": 7, "report": {"expense": 0}}})
    monkeypatch.setattr(management.provider, "signed_shop_request", request)
    await management.gmv_performance(None, None, 7, date(2026, 9, 1), date(2026, 10, 9))
    assert request.call_args.kwargs == {"method": "POST", "body": {"campaign_id": 7, "start_date": "01-09-2026", "end_date": "09-10-2026"}}
    with pytest.raises(HTTPException):
        await management.gmv_performance(None, None, 7, date(2026, 4, 8), date(2026, 4, 9))
    assert request.await_count == 1


def test_contract_rejects_empty_changes_and_wrong_action_fields():
    with pytest.raises(ValidationError):
        schemas.ItemEdit(apply_to_all_models=True)
    with pytest.raises(ValidationError):
        schemas.ModelEdit(model_id=9, model_sku="x", dimension={"package_length": 1, "package_width": 1, "package_height": 1})
    with pytest.raises(ValidationError):
        schemas.GmvEdit(campaign_id=7, edit_action="pause", daily_budget=1, reference_id=uuid4())
