"""Shopee Ads campaigns: list with settings + performance, and the validated write actions."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

PENGATURAN = {
    "campaign_id": 111,
    "common_info": {
        "ad_type": "manual", "ad_name": "Kursi Rotan", "campaign_status": "ongoing", "bidding_method": "manual",
        "campaign_placement": "search", "campaign_budget": 50000.5,
        "campaign_duration": {"start_time": 1700000000, "end_time": 0}, "item_id_list": [9, 10],
    },
    "manual_bidding_info": {
        "selected_keywords": [
            {"keyword": "kursi", "status": "normal", "match_type": "broad", "bid_price_per_click": 300},
            {"keyword": "lama", "status": "deleted", "match_type": "exact", "bid_price_per_click": 100},
        ]
    },
}
KINERJA = {
    "campaign_id": 111,
    "metrics_list": [
        {"date": "01-10-2026", "impression": 1000, "clicks": 50, "expense": 10000, "direct_order": 2, "direct_gmv": 80000},
        {"date": "02-10-2026", "impression": 500, "clicks": 25, "expense": 5000, "direct_order": 1, "direct_gmv": 20000},
    ],
}


def test_a_campaign_joins_settings_with_summed_performance():
    k = erp_shopee.normalisasi_kampanye(PENGATURAN, KINERJA)
    assert k["campaign_id"] == "111" and k["nama"] == "Kursi Rotan" and k["status"] == "ongoing"
    assert k["anggaran"] == Decimal("50000.5") and k["selesai"] is None and k["item_id"] == ["9", "10"]
    assert [x["kata"] for x in k["kata_kunci"]] == ["kursi"]  # the deleted keyword is hidden
    assert k["kinerja"]["expense"] == Decimal(15000) and k["kinerja"]["direct_gmv"] == Decimal(100000)
    assert k["kinerja"]["roas"] == Decimal(100000) / Decimal(15000) and k["kinerja"]["clicks"] == 75


def test_a_campaign_without_performance_or_with_no_spend_has_no_roas():
    assert erp_shopee.normalisasi_kampanye(PENGATURAN, None)["kinerja"] is None
    sepi = {"campaign_id": 111, "metrics_list": [{"date": "01-10-2026", "impression": 0, "clicks": 0, "expense": 0}]}
    k = erp_shopee.normalisasi_kampanye({"campaign_id": 1}, sepi)
    assert k["nama"] == "Kampanye 1" and k["kinerja"]["roas"] is None and k["kinerja"]["ctr"] is None


@pytest.mark.asyncio
async def test_the_list_pages_through_ids_and_notes_a_failed_balance(monkeypatch):
    monkeypatch.setattr(erp_shopee, "SHOPEE_LIVE_SYNC", True)
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_ID", "1")
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_KEY", "k")
    akun = SimpleNamespace(access_token="t", id_toko_eksternal="5", platform="shopee")
    panggilan = []

    async def fake(session, akun_, path, **kw):
        panggilan.append((path, kw.get("params")))
        if path == erp_shopee._PATH_ADS_KAMPANYE:
            if kw["params"]["offset"] == 0:
                return {"response": {"has_next_page": True, "campaign_list": [{"campaign_id": 111}]}}
            return {"response": {"has_next_page": False, "campaign_list": [{"campaign_id": 222}]}}
        if path == erp_shopee._PATH_ADS_PENGATURAN:
            assert kw["params"]["campaign_id_list"] == "111,222"
            return {"response": {"campaign_list": [PENGATURAN, {"campaign_id": 222, "common_info": {"ad_name": "B"}}]}}
        if path == erp_shopee._PATH_ADS_KINERJA:
            return {"response": [{"campaign_list": [KINERJA]}]}
        raise HTTPException(status_code=502, detail="saldo down")

    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    hasil = await erp_shopee.daftar_kampanye_iklan(None, akun, 7)
    assert [k["campaign_id"] for k in hasil["kampanye"]] == ["111", "222"]
    assert hasil["kampanye"][0]["kinerja"] is not None and hasil["kampanye"][1]["kinerja"] is None
    assert hasil["saldo"] is None and hasil["catatan"] == ["Saldo iklan gagal dimuat: saldo down"]


def test_actions_are_whitelisted_and_the_budget_has_a_typo_guard():
    b = erp_shopee.susun_aksi_kampanye(7, {"aksi": "pause"})
    assert b["edit_action"] == "pause" and b["campaign_id"] == 7 and b["reference_id"]
    assert erp_shopee.susun_aksi_kampanye(7, {"aksi": "change_budget", "budget": "25000"})["budget"] == 25000.0
    for buruk in ({"aksi": "hack"}, {}, {"aksi": "change_budget"}, {"aksi": "change_budget", "budget": 0},
                  {"aksi": "change_budget", "budget": 99_999_999}, {"aksi": "change_roas_target", "roas_target": 500}):
        with pytest.raises(HTTPException) as exc:
            erp_shopee.susun_aksi_kampanye(7, buruk)
        assert exc.value.status_code == 422


def test_keyword_changes_need_the_right_fields():
    b = erp_shopee.susun_kata_kunci(7, {"kata_kunci": [{"aksi": "add", "kata": " kursi ", "bid": 300, "tipe": "exact"}, {"aksi": "delete", "kata": "x"}]})
    assert b["selected_keywords"][0] == {"edit_action": "add", "keyword": "kursi", "bid_price_per_click": 300.0, "match_type": "exact"}
    assert b["selected_keywords"][1] == {"edit_action": "delete", "keyword": "x"}
    for buruk in ({}, {"kata_kunci": []}, {"kata_kunci": [{"aksi": "add", "kata": "a", "tipe": "exact"}]},
                  {"kata_kunci": [{"aksi": "add", "kata": "a", "bid": 5, "tipe": "mirip"}]}, {"kata_kunci": [{"aksi": "zzz", "kata": "a"}]}):
        with pytest.raises(HTTPException):
            erp_shopee.susun_kata_kunci(7, buruk)


def test_a_new_ad_validates_item_budget_and_manual_keywords():
    auto = erp_shopee.susun_iklan_baru({"item_id": "123", "budget": 50000, "roas_target": 4})
    assert auto["item_id"] == 123 and auto["bidding_method"] == "auto" and auto["roas_target"] == 4.0 and auto["end_date"] == ""
    manual = erp_shopee.susun_iklan_baru({"item_id": 1, "budget": 1000, "bidding": "manual", "kata_kunci": [{"kata": "kursi", "bid": 200}]})
    assert manual["selected_keywords"] == [{"keyword": "kursi", "match_type": "broad", "bid_price_per_click": 200.0}]
    for buruk in ({"item_id": "abc", "budget": 1}, {"item_id": 1}, {"item_id": 1, "budget": 1, "bidding": "manual"},
                  {"item_id": 1, "budget": 1, "bidding": "xx"}):
        with pytest.raises(HTTPException):
            erp_shopee.susun_iklan_baru(buruk)


@pytest.mark.asyncio
async def test_a_write_calls_shopee_with_the_validated_body_and_is_logged(monkeypatch, caplog):
    akun = SimpleNamespace(platform="shopee", nama_toko="Toko A")
    kirim = {}

    async def fake(session, akun_, path, **kw):
        kirim.update(path=path, **kw)
        return {"response": []}

    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    with caplog.at_level("WARNING"):
        await services.ubah_iklan_shopee(None, akun, "aksi", 9, {"aksi": "delete"}, SimpleNamespace(username="budi"))
    assert kirim["path"] == erp_shopee._PATH_ADS_UBAH and kirim["method"] == "POST" and kirim["body"]["edit_action"] == "delete"
    assert "budi" in caplog.text and "Toko A" in caplog.text
    with pytest.raises(HTTPException) as exc:
        await services.ubah_iklan_shopee(None, SimpleNamespace(platform="tiktok", nama_toko="x"), "aksi", 9, {"aksi": "pause"}, None)
    assert exc.value.status_code == 501


@pytest.mark.asyncio
async def test_recommendations_are_read_per_part_and_one_failure_does_not_hide_the_rest(monkeypatch):
    async def fake(session, akun, path, **kw):
        if path.endswith("get_product_recommended_roi_target"):
            assert kw["params"]["item_id"] == 5 and kw["params"]["reference_id"]
            return {"response": {"lower_bound": {"value": 3.5, "percentile": 80}, "exact": {"value": 5.9, "percentile": 50}}}
        if path.endswith("get_create_product_ad_budget_suggestion"):
            assert kw["body"]["product_selection"] == "manual" and kw["body"]["bidding_method"] == "auto"
            raise HTTPException(status_code=502, detail="anggaran down")
        assert kw["params"] == {"item_id": 5, "input_keyword": "kursi"}
        return {"response": {"suggested_keywords": [{"keyword": "kursi rotan", "quality_score": 8, "search_volume": 900, "suggested_bid": 450}]}}

    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    h = await erp_shopee.iklan_saran(None, SimpleNamespace(), 5, "kursi")
    assert h["roas"]["rendah"] == {"nilai": 3.5, "persentil": 80} and "tinggi" not in h["roas"]
    assert h["anggaran"] is None and h["catatan"] == ["Saran anggaran tidak tersedia: anggaran down"]
    assert h["kata_kunci"] == [{"kata": "kursi rotan", "skor": 8, "volume": 900, "bid": 450}]
