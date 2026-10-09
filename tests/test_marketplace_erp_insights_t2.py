from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import PromosiUpdateIn
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import (
    erp_shopee_insights as insights,
    erp_shopee_promotions as promotions,
)


@pytest.mark.asyncio
async def test_product_stats_identity_missing_and_zero(monkeypatch):
    request = AsyncMock(return_value={"response": {"item_list": [{"item_id": 7, "sale": 0, "views": None}]}})
    monkeypatch.setattr(insights.provider, "signed_shop_request", request)
    result = await insights.product_stats(None, None, "7")
    assert result["sale"] == 0 and result["views"] is None and result["rating_star"] is None
    assert request.call_args.kwargs["params"] == {"item_id_list": "7"}
    with pytest.raises(insights.provider.ShopeeAPIError):
        await insights.product_stats(None, None, 8)


@pytest.mark.asyncio
async def test_shop_preserves_period_units_and_rejects_missing(monkeypatch):
    row = {"metric_id": 1, "metric_name": "late_shipment_rate", "current_period": None, "last_period": 0, "unit": 2}
    request = AsyncMock(return_value={"response": {"metric_list": [row]}})
    monkeypatch.setattr(insights.provider, "signed_shop_request", request)
    result = await insights.shop_performance(None, None)
    assert result["metrics"] == [row] and result["overall_performance"] is None
    request.return_value = {"response": {}}
    with pytest.raises(insights.provider.ShopeeAPIError):
        await insights.shop_performance(None, None)


@pytest.mark.asyncio
async def test_edit_promotion_only_supplied_fields_and_ongoing_start(monkeypatch):
    monkeypatch.setattr(
        promotions, "detail", AsyncMock(return_value={"status": "ongoing", "mulai_at": 100, "selesai_at": 5000})
    )
    monkeypatch.setattr(promotions.time, "time", lambda: 1000)
    request = AsyncMock(return_value={"response": {"discount_id": 9}})
    monkeypatch.setattr(promotions, "signed_shop_request", request)
    result = await promotions.ubah(None, None, "9", PromosiUpdateIn(nama=" Baru "))
    assert result["ok"]
    assert request.call_args.kwargs["body"] == {"discount_id": 9, "discount_name": "Baru"}
    with pytest.raises(HTTPException):
        await promotions.ubah(None, None, "9", PromosiUpdateIn(mulai_at=1500))
    assert request.await_count == 1
    with pytest.raises(HTTPException):
        await promotions.ubah(None, None, "9", PromosiUpdateIn(selesai_at=1200))
    assert request.await_count == 1


def test_edit_empty_or_whitespace_rejected():
    for values in [{}, {"nama": "   "}, {"nama": None}]:
        with pytest.raises(ValidationError):
            PromosiUpdateIn(**values)
