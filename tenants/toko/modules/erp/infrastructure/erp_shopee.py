"""Shopee Open Platform (seller-center) sync adapter -- PLACEHOLDER.

STATUS: not wired to the real Shopee API yet. Same reasoning as
tenants/toko/modules/toko/infrastructure/payment_ipaymu.py and
shipping_biteship.py: no partner_id/partner_key has been issued yet, so
there is nothing to build or test against. Every call below fails loudly
(NotImplementedError) instead of pretending to sync real data.

TODO once you have Shopee Open Platform partner credentials:
  1. Fill SHOPEE_PARTNER_ID / SHOPEE_PARTNER_KEY / SHOPEE_SHOP_ID in .env.
  2. Confirm the current Shopee Open API v2 auth scheme (HMAC-SHA256 over
     partner_id + api_path + timestamp [+ access_token + shop_id], signed
     with partner_key) against https://open.shopee.com/documents --
     unreachable from this session's network, must be checked from
     wherever you read this.
  3. Implement product pull (GetItemList/GetItemBaseInfo), order pull
     (GetOrderList/GetOrderDetail) and chat pull (GetConversationList/
     GetMessage) mapped into ProdukERP / PesananERP+ItemPesananERP /
     PercakapanERP+PesanChatERP rows (upsert keyed on
     (platform="shopee", id_eksternal)).
  4. Map Shopee's own order status values (UNPAID/READY_TO_SHIP/SHIPPED/
     COMPLETED/CANCELLED/...) to STATUS_PESANAN_ERP -- see the mapping
     rationale in infrastructure/models.py.
  5. Remove the NotConfigured guards once verified end-to-end in Shopee's
     sandbox/test shop.
"""
from __future__ import annotations

import os

from fastapi import HTTPException, status

SHOPEE_PARTNER_ID = os.getenv("SHOPEE_PARTNER_ID")
SHOPEE_PARTNER_KEY = os.getenv("SHOPEE_PARTNER_KEY")
SHOPEE_SHOP_ID = os.getenv("SHOPEE_SHOP_ID")


class ShopeeNotConfigured(HTTPException):
    def __init__(self, detail: str = "Integrasi Shopee belum dikonfigurasi. Isi SHOPEE_PARTNER_ID/SHOPEE_PARTNER_KEY/SHOPEE_SHOP_ID setelah aplikasi partner disetujui."):
        super().__init__(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


def _is_configured() -> bool:
    return bool(SHOPEE_PARTNER_ID and SHOPEE_PARTNER_KEY and SHOPEE_SHOP_ID)


async def sync_produk() -> list[dict]:
    """Pull the seller's Shopee catalog. NOT IMPLEMENTED -- see module
    docstring."""
    if not _is_configured():
        raise ShopeeNotConfigured()
    raise NotImplementedError("Shopee sync_produk() belum diimplementasikan -- verifikasi bentuk API dulu.")


async def sync_pesanan() -> list[dict]:
    """Pull recent Shopee orders. NOT IMPLEMENTED -- see module docstring."""
    if not _is_configured():
        raise ShopeeNotConfigured()
    raise NotImplementedError("Shopee sync_pesanan() belum diimplementasikan -- verifikasi bentuk API dulu.")


async def sync_chat() -> list[dict]:
    """Pull recent Shopee buyer chat messages. NOT IMPLEMENTED -- see module
    docstring."""
    if not _is_configured():
        raise ShopeeNotConfigured()
    raise NotImplementedError("Shopee sync_chat() belum diimplementasikan -- verifikasi bentuk API dulu.")
