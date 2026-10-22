"""One non-product-ads campaign id (e.g. Shop GMV Max) must not blank settings/performance of every campaign."""
import pytest

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

BAD = 100065112


@pytest.mark.asyncio
async def test_invalid_campaign_id_is_dropped_and_rest_is_kept(monkeypatch):
    monkeypatch.setattr(erp_shopee, "live_sync_enabled", lambda: True)
    monkeypatch.setattr(erp_shopee, "_akun_configured", lambda a: True)
    calls = []

    async def fake(session, akun, path, *, params=None, **_):
        calls.append((path, (params or {}).get("campaign_id_list")))
        ids = [int(i) for i in (params or {}).get("campaign_id_list", "").split(",") if i]
        if path == erp_shopee._PATH_ADS_KAMPANYE:
            return {"response": {"campaign_list": [{"campaign_id": i} for i in (1, BAD, 2)], "has_next_page": False}}
        if BAD in ids and path in (erp_shopee._PATH_ADS_PENGATURAN, erp_shopee._PATH_ADS_KINERJA):
            raise erp_shopee.ShopeeAPIError(path, "error_param", f"Wrong parameters. campaign_id_list: has invalid product ads campaign ids- {BAD}")
        if path == erp_shopee._PATH_ADS_PENGATURAN:
            return {"response": {"campaign_list": [{"campaign_id": i, "common_info": {"ad_type": "auto"}} for i in ids]}}
        if path == erp_shopee._PATH_ADS_KINERJA:
            return {"response": [{"campaign_list": [{"campaign_id": i, "metrics_list": [{"expense": 1000, "impression": 10}]} for i in ids]}]}
        return {"response": {"total_balance": 0}}

    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    hasil = await erp_shopee.daftar_kampanye_iklan(None, object(), 7)
    assert [k["campaign_id"] for k in hasil["kampanye"]] == ["1", "2"]
    assert all(k["kinerja"] is not None for k in hasil["kampanye"])
    assert any(str(BAD) in c for c in hasil["catatan"])
    # The performance call never resends the rejected id.
    assert all(str(BAD) not in (ids or "") for p, ids in calls if p == erp_shopee._PATH_ADS_KINERJA)


@pytest.mark.asyncio
async def test_other_errors_are_not_swallowed(monkeypatch):
    async def fake(session, akun, path, *, params=None, **_):
        raise erp_shopee.ShopeeAPIError(path, "error_auth", "Invalid access_token")

    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    with pytest.raises(erp_shopee.ShopeeAPIError):
        await erp_shopee._ads_per_kelompok(None, None, "/x", [1, 2], set(), lambda k: {})
