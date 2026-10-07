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

    if getattr(akun, 'platform', 'shopee') != 'shopee':
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail='API iklan ini khusus toko Shopee.')
    if aksi not in IKLAN_API:
        raise ValueError(f"Aksi iklan tidak dikenal: {aksi}")
    method, path = IKLAN_API[aksi]
    validasi_nilai(body)
    data = await erp_shopee.signed_shop_request(session, akun, path, method=method, params=params, body=body)
    return konfirmasi_mutasi(path, data, (body or {}).get('campaign_id')) if aksi.startswith(('buat_', 'ubah_')) else data


def validasi_nilai(body):
    """Reject NaN/Infinity before a paid write reaches provider JSON serialization."""
    import math
    from decimal import Decimal
    from fastapi import HTTPException
    if isinstance(body, dict):
        for value in body.values():
            validasi_nilai(value)
    elif isinstance(body, list):
        for value in body:
            validasi_nilai(value)
    elif isinstance(body, (float, Decimal)) and not math.isfinite(body):
        raise HTTPException(status_code=422, detail='Nilai anggaran/harga iklan harus angka terbatas.')


def konfirmasi_mutasi(path, data, campaign_id=None):
    """HTTP success is insufficient: preserve per-keyword/item rejections and campaign identity."""
    from .adapters.erp_shopee import ShopeeAPIError
    response = data.get('response')
    if not isinstance(response, (dict, list)) or not response:
        raise ShopeeAPIError(path, 'unconfirmed_response', 'Hasil iklan belum terkonfirmasi. Segarkan kampanye sebelum mengirim ulang.', data.get('request_id'))
    def check(value):
        if isinstance(value, list):
            for row in value:
                check(row)
        elif isinstance(value, dict):
            for key in ('failed_edits', 'failure_list', 'failed_list'):
                if value.get(key):
                    raise ShopeeAPIError(path, 'partial_rejection', f'Beberapa perubahan ditolak: {value[key]}. Segarkan kampanye sebelum mengirim ulang.', data.get('request_id'))
            if str(value.get('error') or '').strip():
                raise ShopeeAPIError(path, str(value['error']), str(value.get('message') or ''), data.get('request_id'))
            for nested in value.values():
                if isinstance(nested, (list, dict)):
                    check(nested)
    check(response)
    if path.rsplit('/', 1)[-1] in ('create_manual_product_ads', 'edit_manual_product_ads', 'edit_manual_product_ad_keywords'):
        rows = response if isinstance(response, list) else [response]
        if not rows or any(not r.get('campaign_id') or (campaign_id is not None and str(r['campaign_id']) != str(campaign_id)) for r in rows):
            raise ShopeeAPIError(path, 'unconfirmed_response', 'Identitas kampanye belum dikonfirmasi. Segarkan sebelum mengirim ulang.', data.get('request_id'))
    return data
