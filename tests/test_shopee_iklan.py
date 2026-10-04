from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import shopee_iklan

def test_ads_allowlist_covers_the_documented_module():
    paths = {path for _, path in shopee_iklan.IKLAN_API.values()}
    assert "/api/v2/ads/create_manual_product_ads" in paths
    assert "/api/v2/ads/get_recommended_keyword_list" in paths
    assert "/api/v2/ads/create_gms_product_campaign" in paths
    assert len(shopee_iklan.IKLAN_API) >= 24
