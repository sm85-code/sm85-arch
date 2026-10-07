"""Provider contract and retry safety tests: all writes mocked, never calls a marketplace."""

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
import requests
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import services, workflows
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn
from tenants.marketplace_erp.modules.marketplace_erp.application.workflow_schemas import PublishIn, DisputeIn
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import (
    erp_shopee as shopee,
    erp_shopee_workflows as adapter,
    erp_shopee_upload as upload,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import shopee_iklan


def body():
    return {
        "operation_id": str(uuid4()),
        "nama": "Produk uji",
        "deskripsi": "Deskripsi produk asli",
        "category_id": 1,
        "price": "15000.25",
        "weight": "0.5",
        "dimension": {"package_length": 12, "package_width": 8, "package_height": 5},
        "image_ids": ["image"],
        "logistic_info": [{"logistic_id": 1}],
        "tiers": [],
        "models": [],
    }


def metadata():
    return {
        "categories": [{"category_id": 1, "has_children": False}],
        "channels": [{"logistics_channel_id": 1, "enabled": True}],
        "attributes": [],
        "limits": {},
    }


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


def test_exact_cartesian_models_and_nonfinite_validation():
    data = body()
    data["tiers"] = [{"name": "Warna", "options": [{"option": "Merah"}, {"option": "Biru"}]}]
    with pytest.raises(ValidationError):
        PublishIn(**data)
    data["models"] = [{"tier_index": [i], "price": "123"} for i in range(2)]
    p = PublishIn(**data)
    payload = adapter.variant_body(p, 12)
    assert payload["standardise_tier_variation"][0]["variation_option_list"][0]["variation_option_name"] == "Merah"
    assert all(m["seller_stock"] == [{"stock": 0}] for m in payload["model"])
    assert adapter.parent_body(p)["item_status"] == "UNLIST"
    for v in ["NaN", "Infinity", "1e1000"]:
        data["price"] = v
        with pytest.raises(ValidationError):
            PublishIn(**data)


def test_destination_metadata_and_conditional_attributes():
    meta = metadata()
    meta["attributes"] = [
        {
            "attribute_id": 1,
            "name": "Bahan",
            "mandatory": True,
            "attribute_info": {"input_type": 1},
            "attribute_value_list": [
                {
                    "value_id": 10,
                    "name": "Kayu",
                    "child_attribute_list": [
                        {
                            "attribute_id": 2,
                            "name": "Jenis kayu",
                            "mandatory": True,
                            "attribute_info": {"input_type": 3},
                            "attribute_value_list": [],
                        }
                    ],
                }
            ],
        }
    ]
    data = body()
    with pytest.raises(HTTPException):
        adapter.validate_publication(PublishIn(**data), meta)
    data["attribute_list"] = [
        {"attribute_id": 1, "attribute_value_list": [{"value_id": 10, "original_value_name": "Kayu"}]},
        {"attribute_id": 2, "attribute_value_list": [{"value_id": 0, "original_value_name": "Jati"}]},
    ]
    adapter.validate_publication(PublishIn(**data), meta)
    data["attribute_list"][0]["attribute_value_list"][0]["original_value_name"] = "Besi"
    with pytest.raises(HTTPException):
        adapter.validate_publication(PublishIn(**data), meta)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", [False, True])
async def test_publication_receipt_prevents_duplicate_parent_on_timeout_or_success(session, monkeypatch, transport):
    account = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="A"))

    async def meta(*args):
        return metadata()

    writes = []

    async def write(*args, **kwargs):
        writes.append(args[2])
        # Receipt must already be durable when provider call starts.
        record = await workflows.receipt(session, account.id, payload.operation_id)
        assert record is not None
        if transport:
            raise requests.Timeout()
        return {"response": {"item_id": 99}, "request_id": "req"}

    async def snapshot(*args):
        raise HTTPException(424, "snapshot delayed")

    monkeypatch.setattr(adapter, "metadata", meta)
    monkeypatch.setattr(shopee, "signed_shop_request", write)
    monkeypatch.setattr(shopee, "ambil_satu_produk", snapshot)
    payload = PublishIn(**body())
    result = await workflows.publish(session, account.id, payload)
    repeated = await workflows.publish(session, account.id, payload)
    assert repeated == result and len(writes) == 1
    assert result["ok"] is not transport
    assert result["item_id"] == (None if transport else "99")
    assert result["warnings"]
    different = payload.model_copy(update={"nama": "Berbeda"})
    with pytest.raises(HTTPException) as exc:
        await workflows.publish(session, account.id, different)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_variant_failure_leaves_parent_hidden_and_receipt_with_item_id(session, monkeypatch):
    account = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="A"))

    async def meta(*args):
        return metadata()

    paths = []

    async def write(session, akun, path, **kw):
        paths.append(path)
        if path.endswith("add_item"):
            assert kw["body"]["item_status"] == "UNLIST"
            return {"response": {"item_id": 99}}
        assert (await workflows.receipt(session, account.id, payload.operation_id)).item_id == "99"
        return {"response": {"item_id": 99, "model": []}}

    async def delay(*args):
        pass

    monkeypatch.setattr(adapter, "metadata", meta)
    monkeypatch.setattr(shopee, "signed_shop_request", write)
    monkeypatch.setattr(workflows.asyncio, "sleep", delay)
    data = body() | {
        "aktif": True,
        "tiers": [{"name": "Warna", "options": [{"option": "Merah"}]}],
        "models": [{"tier_index": [0], "price": "15000"}],
    }
    payload = PublishIn(**data)
    result = await workflows.publish(session, account.id, payload)
    assert not result["ok"] and result["status"] == "sebagian" and result["item_id"] == "99"
    assert len(paths) == 2 and not any("unlist_item" in p for p in paths)
    assert await workflows.publish(session, account.id, payload) == result


@pytest.mark.asyncio
async def test_dispute_enforces_live_modules_before_write(monkeypatch):
    paths = []

    async def write(session, akun, path, **kwargs):
        paths.append(path)
        if path.endswith("get_return_dispute_reason"):
            return {
                "response": {
                    "dispute_reason_list": [
                        {
                            "dispute_reason": 50,
                            "dispute_requirement": "Bukti",
                            "evidence_module_list": [
                                {"module_index": 1, "requirement": "Foto wajib", "is_required": True}
                            ],
                        }
                    ]
                }
            }
        assert kwargs["body"]["image_list"][0]["requirement"] == "Foto wajib"
        return {"response": {"return_sn": "RET1"}}

    monkeypatch.setattr(shopee, "signed_shop_request", write)
    data = {"email": "owner@example.com", "reason_id": 50}
    with pytest.raises(HTTPException):
        await adapter.dispute(None, None, "RET1", DisputeIn(**data))
    assert len(paths) == 1
    data["evidence"] = [{"module_index": 1, "urls": ["https://fileproxy.scsusercontent.com/photo.jpg"]}]
    assert (await adapter.dispute(None, None, "RET1", DisputeIn(**data)))["ok"]
    assert not adapter.evidence_url("https://scsusercontent.com.evil.test/photo")
    assert not adapter.evidence_url("http://fileproxy.scsusercontent.com/photo")
    assert not adapter.evidence_url("https://fileproxy.scsusercontent.com:bad/photo")


@pytest.mark.asyncio
async def test_wallet_offset_negative_amount_unknown_type_and_more(monkeypatch):
    async def write(session, akun, path, **kwargs):
        assert kwargs["params"]["page_no"] == 40
        return {
            "response": {
                "more": True,
                "transaction_list": [
                    {
                        "status": "COMPLETED",
                        "transaction_type": "FUTURE_TYPE",
                        "create_time": 12,
                        "amount": -10.25,
                        "current_balance": 0,
                        "withdrawal_id": 9007199254740993,
                    }
                ],
            }
        }

    monkeypatch.setattr(shopee, "signed_shop_request", write)
    result = await adapter.wallet(None, SimpleNamespace(nama_toko="A"), date(2026, 10, 1), date(2026, 10, 2), 40)
    assert result["next_offset"] == 41 and result["ada_lagi"] is True
    assert result["items"][0]["amount"] == "-10.25"
    assert result["items"][0]["current_balance"] == "0"
    assert result["items"][0]["withdrawal_id"] == "9007199254740993"
    assert "currency" not in result["items"][0]


@pytest.mark.asyncio
async def test_copy_uses_original_price_and_zero_stock(monkeypatch):
    async def read(*args, **kwargs):
        return {
            "item_list": [
                {
                    "item_id": 12,
                    "item_name": "Produk",
                    "description": "Asli",
                    "price_info": [{"current_price": 20, "original_price": 30}],
                    "image": {"image_id_list": ["same-photo"]},
                    "weight": 0.5,
                    "has_model": False,
                }
            ]
        }

    monkeypatch.setattr(adapter, "read", read)
    draft = await adapter.copy_draft(None, None, 12)
    assert draft["price"] == "30" and draft["stock"] == 0
    assert draft["image_ids"] == ["same-photo"] and "logistic_info" not in draft


def test_ads_keyword_partial_rejection_is_not_success():
    data = {
        "response": [{"campaign_id": 12, "failed_edits": [{"keyword": "abc", "error": "not_allowed"}]}],
        "request_id": "req",
    }
    with pytest.raises(shopee.ShopeeAPIError) as exc:
        shopee_iklan.konfirmasi_mutasi("/api/v2/ads/edit_manual_product_ad_keywords", data, 12)
    assert exc.value.request_id == "req"
    with pytest.raises(HTTPException):
        shopee_iklan.validasi_nilai({"budget": float("inf")})
    assert shopee_iklan.konfirmasi_mutasi(
        "/api/v2/ads/edit_manual_product_ads", {"response": [{"campaign_id": 12}]}, 12
    )


@pytest.mark.asyncio
async def test_account_scope_denied_before_remote_read(monkeypatch):
    from tenants.marketplace_erp.adapters.api.v1 import workflow_router

    calls = []

    async def deny(*args):
        raise HTTPException(403, "not assigned")

    async def remote(*args):
        calls.append(True)

    monkeypatch.setattr(workflow_router, "pastikan_akses_akun", deny)
    monkeypatch.setattr(adapter, "metadata", remote)
    with pytest.raises(HTTPException) as exc:
        await workflow_router.metadata("other-shop", None, None, SimpleNamespace(role="staff"))
    assert exc.value.status_code == 403 and not calls


@pytest.mark.asyncio
async def test_public_media_upload_has_partner_signature_only(monkeypatch):
    monkeypatch.setattr(shopee, "live_sync_enabled", lambda: True)
    monkeypatch.setattr(shopee, "partner_configured", lambda: True)
    monkeypatch.setattr(shopee, "_partner_id_int", lambda: 1)
    monkeypatch.setattr(shopee, "_host", lambda: "https://partner.shopeemobile.com")

    def sign(path, timestamp, **kwargs):
        assert not kwargs
        return "public-sign"

    def send(url, **kwargs):
        assert "access_token" not in url and "shop_id" not in url
        assert kwargs["allow_redirects"] is False
        assert kwargs["data"] == {"scene": "normal"}
        return SimpleNamespace(ok=True, json=lambda: {"response": {"image_info": {"image_id": "image"}}})

    monkeypatch.setattr(shopee, "sign_request", sign)
    monkeypatch.setattr(upload.requests, "post", send)
    result = await upload.upload(None, SimpleNamespace(), b"\xff\xd8\xfffake")
    assert result["image_id"] == "image"
