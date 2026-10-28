"""One-screen product edit: only changed parts, in Shopee's order; every provider call is mocked."""
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from tenants.marketplace_erp.modules.marketplace_erp.application.edit_schemas import ProductEdit
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee_edit as edit

CURRENT = {
    "tier_variation": [{"name": "Warna", "option_list": [{"option": "Hitam"}, {"option": "Putih"}]}],
    "standardise_tier_variation": [{"variation_id": 7, "variation_name": "Warna", "variation_option_list": [{"variation_option_id": 70, "variation_option_name": "Hitam"}, {"variation_option_id": 71, "variation_option_name": "Putih"}]}],
    "model": [{"model_id": 11, "tier_index": [0], "model_sku": "A"}, {"model_id": 12, "tier_index": [1], "model_sku": "B"}],
}
OK = {"error": "", "request_id": "r", "response": {}}


@pytest.fixture
def calls(monkeypatch):
    log = []

    async def post(session, akun, path, method="POST", body=None):
        log.append((path, body))
        if path.endswith("add_model"):
            return {"error": "", "request_id": "r", "response": {"model": [{"model_id": 99, "tier_index": m["tier_index"]} for m in body["model_list"]]}}
        return OK

    async def read(session, akun, path, params):
        if "base_info" in path:
            return {"item_list": [{"item_id": 5, "has_model": True}]}
        tiers = next((b for p, b in log if p.endswith("update_tier_variation")), None)
        if tiers:  # Shopee answers with the appended option once the variation is updated
            return {**CURRENT, "tier_variation": [{"name": t.get("variation_name", "Warna"), "option_list": [{"option": o.get("variation_option_name", "x")} for o in t["variation_option_list"]]} for t in tiers["standardise_tier_variation"]]}
        return CURRENT

    monkeypatch.setattr(edit.provider, "signed_shop_request", post)
    monkeypatch.setattr(edit.workflows, "read", read)
    return log


def test_needs_a_change():
    with pytest.raises(ValidationError):
        ProductEdit()


@pytest.mark.asyncio
async def test_price_and_stock_only_go_straight_to_shopee(calls):
    r = await edit.apply(None, None, 5, ProductEdit(models=[{"model_id": 11, "price": "15000", "stock": 3}]))
    assert [p for p, _ in calls] == ["/api/v2/product/update_price", "/api/v2/product/update_stock"]
    assert calls[0][1]["price_list"] == [{"model_id": 11, "original_price": 15000.0}]
    assert calls[1][1]["stock_list"] == [{"model_id": 11, "seller_stock": [{"stock": 3}]}]
    assert r["langkah"] == ["Harga", "Stok"]


@pytest.mark.asyncio
async def test_new_option_then_model_in_order_keeping_standard_ids(calls):
    body = ProductEdit(
        tiers=[{"name": "Warna", "options": [{"option": "Hitam", "asal": 0}, {"option": "Putih", "asal": 1}, {"option": "Abu", "asal": None}]}],
        add_models=[{"tier_index": [2], "price": "20000", "stock": 4}],
        models=[{"model_id": 12, "model_sku": "B2"}],
    )
    await edit.apply(None, None, 5, body)
    paths = [p for p, _ in calls]
    assert paths == ["/api/v2/product/update_tier_variation", "/api/v2/product/add_model", "/api/v2/product/update_model"]
    tier = calls[0][1]["standardise_tier_variation"][0]
    assert tier["variation_id"] == 7
    assert [o.get("variation_option_id") for o in tier["variation_option_list"]] == [70, 71, 0]
    assert tier["variation_option_list"][2]["variation_option_name"] == "Abu"
    assert calls[0][1]["model_list"] == [{"model_id": 11, "tier_index": [0]}, {"model_id": 12, "tier_index": [1]}]


@pytest.mark.asyncio
async def test_old_options_cannot_be_dropped_or_reordered(calls):
    with pytest.raises(HTTPException) as e:
        await edit.apply(None, None, 5, ProductEdit(tiers=[{"name": "Warna", "options": [{"option": "Putih", "asal": 1}, {"option": "Hitam", "asal": 0}]}]))
    assert e.value.status_code == 409 and not calls


@pytest.mark.asyncio
async def test_unknown_model_is_rejected_before_any_write(calls):
    with pytest.raises(HTTPException):
        await edit.apply(None, None, 5, ProductEdit(models=[{"model_id": 404, "stock": 1}]))
    assert not calls


@pytest.mark.asyncio
async def test_failure_names_the_steps_already_saved(calls, monkeypatch):
    async def post(session, akun, path, method="POST", body=None):
        if path.endswith("update_stock"):
            return {"error": "", "request_id": "r", "response": {"failure_list": [{"model_id": 11, "failed_reason": "stok dikunci"}]}}
        return OK
    monkeypatch.setattr(edit.provider, "signed_shop_request", post)
    with pytest.raises(HTTPException) as e:
        await edit.apply(None, None, 5, ProductEdit(models=[{"model_id": 11, "price": "1000", "stock": 1}]))
    assert "Stok" in e.value.detail and "Sudah tersimpan di Shopee: Harga" in e.value.detail


@pytest.mark.asyncio
async def test_item_without_variants_price_stock(calls, monkeypatch):
    monkeypatch.setattr(edit.workflows, "read", AsyncMock(return_value={"tier_variation": [], "model": []}))
    await edit.apply(None, None, 5, ProductEdit(price="5000", stock=2))
    assert calls[0][1]["price_list"] == [{"model_id": 0, "original_price": 5000.0}]
    assert calls[1][1]["stock_list"] == [{"model_id": 0, "seller_stock": [{"stock": 2}]}]
