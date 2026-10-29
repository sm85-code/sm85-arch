"""Compact read-only ads summary for the bot. Shopee writes are never called."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tenants.marketplace_erp.modules.marketplace_erp.application import agen_ringkasan
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import shopee_iklan


def test_summary_route_is_an_ads_page_read():
    from tenants.marketplace_erp.modules.marketplace_erp.application import peta_akses

    assert peta_akses.cari("/api/marketplace-erp/akun/ABC/iklan/agen/ringkasan", "GET") == ("iklan", "baca")


def test_auto_product_ads_stay_callable_but_are_flagged_deprecated():
    assert shopee_iklan.USANG == frozenset({"buat_otomatis", "ubah_otomatis"})
    for aksi in shopee_iklan.USANG:
        method, path = shopee_iklan.IKLAN_API[aksi]
        assert method == "POST" and "auto_product_ads" in path
    baris = {r["aksi"]: r for r in shopee_iklan.daftar_aksi()}
    assert baris["buat_otomatis"]["usang"] is True
    assert baris["ubah_otomatis"]["usang"] is True
    assert baris["buat_manual"]["usang"] is False
    assert baris["buat_gmv"]["usang"] is False


def test_shop_toggles_keep_campaign_surge_and_auto_top_up_only_as_flags():
    hasil = agen_ringkasan.ringkas_toggle({
        "response": {
            "campaign_surge": True,
            "auto_top_up": False,
            "data_timestamp": 1700000000,
            "nested": {"some_switch": True, "label": "abaikan"},
        }
    })
    assert hasil["campaign_surge"] is True
    assert hasil["auto_top_up"] is False
    assert hasil["saklar"]["campaign_surge"] is True
    assert hasil["saklar"]["nested.some_switch"] is True
    assert "label" not in str(hasil["saklar"])
    assert agen_ringkasan.ringkas_toggle({"response": []})["campaign_surge"] is None


def test_one_campaign_is_compact_and_drops_deleted_keywords():
    kamp, sisa = agen_ringkasan.ringkas_kampanye(
        {
            "campaign_id": 111,
            "common_info": {
                "ad_name": "Kursi " * 40,
                "ad_type": "manual",
                "campaign_status": "ongoing",
                "bidding_method": "manual",
                "campaign_budget": 50000,
                "item_id_list": [9, 10],
            },
            "manual_bidding_info": {
                "selected_keywords": [
                    {"keyword": "kursi", "status": "normal", "match_type": "broad", "bid_price_per_click": 300},
                    {"keyword": "lama", "status": "deleted", "match_type": "exact", "bid_price_per_click": 100},
                ],
                "negative_keyword_list": ["gratis", {"keyword": "free", "status": "deleted"}],
            },
        },
        {"metrics_list": [{"impression": 10, "clicks": 2, "expense": 1000, "direct_order": 1, "direct_gmv": 4000}]},
    )
    assert kamp["campaign_id"] == "111"
    assert len(kamp["nama"]) <= agen_ringkasan.MAKS_NAMA
    assert [k["kata"] for k in kamp["kata_kunci"]] == ["kursi"]
    assert kamp["kata_kunci"][0]["bid"] == 300
    assert kamp["kata_negatif"] == ["gratis"]
    assert kamp["kinerja"]["roas"] == 4
    assert sisa == 0


def test_keywords_and_campaigns_are_capped():
    kata = [{"keyword": f"k{i}", "status": "normal", "match_type": "broad", "bid_price_per_click": 1} for i in range(30)]
    kamp, sisa = agen_ringkasan.ringkas_kampanye(
        {"campaign_id": 1, "common_info": {"ad_name": "A", "item_id_list": []}, "manual_bidding_info": {"selected_keywords": kata}},
        None,
    )
    assert len(kamp["kata_kunci"]) == agen_ringkasan.MAKS_KATA
    assert sisa == 30 - agen_ringkasan.MAKS_KATA
    assert kamp["kinerja"] is None
    banyak = [{"campaign_id": i, "common_info": {"ad_name": str(i), "item_id_list": [i]}} for i in range(40)]
    hasil = agen_ringkasan.susun(
        kampanye_mentah=banyak, kinerja={}, saldo=10, toggle={}, tarif={}, jam=[], produk={}, catatan=[], ada_lagi=True,
    )
    assert len(hasil["kampanye"]) == agen_ringkasan.MAKS_KAMPANYE
    assert hasil["dipotong"]["kampanye"] is True


def test_hourly_rows_collapse_to_one_row_per_hour():
    jam = agen_ringkasan.ringkas_jam({
        "response": [
            {"hour": 9, "impression": 5, "clicks": 1, "expense": 100, "broad_order": 0},
            {"hour": 9, "impression": 5, "clicks": 1, "expense": 50, "broad_order": 1},
            {"hour": 25, "impression": 1},
        ]
    })
    assert jam == [{"hour": 9, "impression": 10, "clicks": 2, "expense": 150, "broad_order": 1, "broad_gmv": 0}]


@pytest.mark.asyncio
async def test_summary_reads_each_shop_and_never_calls_a_write(monkeypatch):
    akun = SimpleNamespace(id="toko-1", platform="shopee", nama_toko="Toko A")
    dipanggil = []

    async def fake(session, akun_, aksi, *, params=None, body=None):
        dipanggil.append((aksi, params, body))
        if aksi == "saldo":
            return {"request_id": "req-1", "response": {"total_balance": 150000}}
        if aksi == "toggle":
            return {"response": {"campaign_surge": True, "auto_top_up": True}}
        if aksi == "tarif":
            return {"response": {"shop_rate": 1.5, "note": "abaikan teks panjang"}}
        if aksi == "daftar_kampanye":
            return {"response": {"has_next_page": False, "campaign_list": [{"campaign_id": 111}]}}
        if aksi == "pengaturan_kampanye":
            assert params["campaign_id_list"] == "111"
            return {"response": {"campaign_list": [{
                "campaign_id": 111,
                "common_info": {"ad_name": "Kursi", "ad_type": "manual", "campaign_status": "ongoing",
                                "bidding_method": "manual", "campaign_budget": 50000, "item_id_list": [9]},
                "manual_bidding_info": {"selected_keywords": [
                    {"keyword": "kursi", "status": "normal", "match_type": "broad", "bid_price_per_click": 300}
                ]},
            }]}}
        if aksi == "kinerja_harian":
            assert params["start_date"] != params["end_date"]
            return {"response": [{"campaign_list": [{"campaign_id": 111, "metrics_list": [
                {"impression": 100, "clicks": 4, "expense": 2000, "direct_order": 1, "direct_gmv": 8000}
            ]}]}]}
        if aksi == "kinerja_cpc_jam":
            return {"response": [{"hour": 8, "impression": 3, "clicks": 1, "expense": 10, "broad_order": 0}]}
        raise AssertionError(aksi)

    async def nama(session, akun_id, item_ids):
        assert akun_id == "toko-1" and item_ids == ["9"]
        return {"9": {"nama": "Kursi Rotan", "harga": 250000}}

    monkeypatch.setattr(shopee_iklan, "panggil", fake)
    monkeypatch.setattr(agen_ringkasan, "_produk_lokal", nama)
    hasil = await agen_ringkasan.ringkasan_toko(None, akun)
    assert {a for a, _, body in dipanggil if body} == set()
    assert {a for a, _, _ in dipanggil} <= agen_ringkasan.AKSI_BACA
    assert hasil["akun_id"] == "toko-1" and hasil["saldo"] == 150000
    assert hasil["pengaturan_toko"]["campaign_surge"] is True
    assert hasil["pengaturan_toko"]["auto_top_up"] is True
    assert hasil["tarif"] == {"shop_rate": 1.5}
    assert hasil["kampanye"][0]["kata_kunci"][0]["kata"] == "kursi"
    assert hasil["produk"] == [{"item_id": "9", "nama": "Kursi Rotan", "harga": 250000}]
    assert hasil["performa_jam"][0]["hour"] == 8
    assert hasil["request_id"] == "req-1"
    assert "buat_otomatis" in hasil["api_usang"]
    assert hasil["catatan"] == []


@pytest.mark.asyncio
async def test_a_failed_read_becomes_a_note_and_a_non_shopee_shop_is_refused(monkeypatch):
    akun = SimpleNamespace(id="toko-1", platform="shopee", nama_toko="Toko A")

    async def fake(session, akun_, aksi, *, params=None, body=None):
        if aksi == "saldo":
            raise HTTPException(status_code=502, detail="saldo down")
        if aksi == "daftar_kampanye":
            return {"response": {"has_next_page": False, "campaign_list": []}}
        return {"response": {}}

    monkeypatch.setattr(shopee_iklan, "panggil", fake)
    hasil = await agen_ringkasan.ringkasan_toko(None, akun)
    assert hasil["saldo"] is None and hasil["kampanye"] == []
    assert any("saldo" in c for c in hasil["catatan"])

    with pytest.raises(HTTPException) as exc:
        await agen_ringkasan.ringkasan_toko(None, SimpleNamespace(id="x", platform="lazada", nama_toko="L"))
    assert exc.value.status_code == 409
