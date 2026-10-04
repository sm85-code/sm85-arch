"""Shopee Ads Open API (module 117) as documented for Seller In House apps.

Auto ads and Shop GMV Max only work for shops Shopee has whitelisted in Seller Center.
"""
from __future__ import annotations

from typing import Any

# method, path. GET reads; POST creates or edits.
IKLAN_API: dict[str, tuple[str, str]] = {
    "saldo": ("GET", "/api/v2/ads/get_total_balance"),
    "toggle": ("GET", "/api/v2/ads/get_shop_toggle_info"),
    "tarif": ("GET", "/api/v2/ads/get_ads_facil_shop_rate"),
    "daftar_kampanye": ("GET", "/api/v2/ads/get_product_level_campaign_id_list"),
    "pengaturan_kampanye": ("GET", "/api/v2/ads/get_product_level_campaign_setting_info"),
    "kinerja_harian": ("GET", "/api/v2/ads/get_product_campaign_daily_performance"),
    "kinerja_jam": ("GET", "/api/v2/ads/get_product_campaign_hourly_performance"),
    "kinerja_cpc_harian": ("GET", "/api/v2/ads/get_all_cpc_ads_daily_performance"),
    "kinerja_cpc_jam": ("GET", "/api/v2/ads/get_all_cpc_ads_hourly_performance"),
    "saran_produk": ("GET", "/api/v2/ads/get_recommended_item_list"),
    "saran_kata_kunci": ("GET", "/api/v2/ads/get_recommended_keyword_list"),
    "saran_roas": ("GET", "/api/v2/ads/get_product_recommended_roi_target"),
    "saran_anggaran": ("POST", "/api/v2/ads/get_create_product_ad_budget_suggestion"),
    "buat_manual": ("POST", "/api/v2/ads/create_manual_product_ads"),
    "ubah_manual": ("POST", "/api/v2/ads/edit_manual_product_ads"),
    "ubah_kata_kunci": ("POST", "/api/v2/ads/edit_manual_product_ad_keywords"),
    "buat_otomatis": ("POST", "/api/v2/ads/create_auto_product_ads"),
    "ubah_otomatis": ("POST", "/api/v2/ads/edit_auto_product_ads"),
    "cek_gmv": ("GET", "/api/v2/ads/check_create_gms_product_campaign_eligibility"),
    "buat_gmv": ("POST", "/api/v2/ads/create_gms_product_campaign"),
    "ubah_gmv": ("POST", "/api/v2/ads/edit_gms_product_campaign"),
    "ubah_gmv_item": ("POST", "/api/v2/ads/edit_gms_item_product_campaign"),
    "kinerja_gmv": ("GET", "/api/v2/ads/get_gms_campaign_performance"),
    "kinerja_gmv_item": ("GET", "/api/v2/ads/get_gms_item_performance"),
    "item_dihapus_gmv": ("GET", "/api/v2/ads/list_gms_user_deleted_item"),
}


async def panggil(session: Any, akun: Any, aksi: str, *, params: dict | None = None, body: dict | None = None) -> dict:
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    if aksi not in IKLAN_API:
        raise ValueError(f"Aksi iklan tidak dikenal: {aksi}")
    method, path = IKLAN_API[aksi]
    return await erp_shopee.signed_shop_request(session, akun, path, method=method, params=params, body=body)
