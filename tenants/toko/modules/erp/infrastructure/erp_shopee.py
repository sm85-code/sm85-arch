"""Shopee Open Platform (seller-center) sync adapter -- PLACEHOLDER.

STATUS: not wired to the real Shopee API yet. Same reasoning as
tenants/toko/modules/toko/infrastructure/payment_ipaymu.py and
shipping_biteship.py: no partner_id/partner_key has been issued yet, so
there is nothing to build or test against. Every call below fails loudly
(NotImplementedError) instead of pretending to sync real data.

Credential model (per-account, not a single global env var): a Shopee
"partner" app has ONE global partner_id/partner_key (the app-level
credentials issued once by Shopee Open Platform -- these genuinely are
global, not per-shop, so they stay as env vars below). Each SHOP the
partner app is authorized against then gets its OWN shop_id/access_token/
refresh_token from the OAuth flow -- that per-shop data is what
AkunMarketplace (toko_erp_akun, see infrastructure/models.py) now stores
in the DB instead of the old SHOPEE_SHOP_ID-style single global env var,
because a seller can have several Shopee shops connected at once. Every
sync_* function below therefore takes an `akun` (an AkunMarketplace row,
or an object/mapping shaped like one) and is meant to be called once per
active Shopee AkunMarketplace, e.g.:

    for akun in await services.list_akun_marketplace(session, platform="shopee"):
        if akun.status == "aktif":
            await sync_produk(akun)

TODO once you have Shopee Open Platform partner credentials:
  1. Fill SHOPEE_PARTNER_ID / SHOPEE_PARTNER_KEY in .env (app-level,
     global across all connected shops).
  2. Confirm the current Shopee Open API v2 auth scheme (HMAC-SHA256 over
     partner_id + api_path + timestamp [+ access_token + shop_id], signed
     with partner_key) against https://open.shopee.com/documents --
     unreachable from this session's network, must be checked from
     wherever you read this.
  3. Implement the real OAuth authorization callback that fills in an
     AkunMarketplace row's id_toko_eksternal (shop_id)/access_token/
     refresh_token/token_kedaluwarsa once a shop owner approves the app
     (for now those fields are filled manually via the admin PATCH
     /api/toko/marketplace/admin/akun/{id} endpoint, see erp_router.py).
  4. Implement product pull (GetItemList/GetItemBaseInfo), order pull
     (GetOrderList/GetOrderDetail) and chat pull (GetConversationList/
     GetMessage) mapped into ProdukERP / PesananERP+ItemPesananERP /
     PercakapanERP+PesanChatERP rows (upsert keyed on
     (platform="shopee", id_eksternal), each row tagged with the
     akun.id passed in).
  5. Map Shopee's own order status values (UNPAID/READY_TO_SHIP/SHIPPED/
     COMPLETED/CANCELLED/...) to STATUS_PESANAN_ERP -- see the mapping
     rationale in infrastructure/models.py.
  6. Remove the NotConfigured guards once verified end-to-end in Shopee's
     sandbox/test shop.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException, status

# App-level (partner) credentials -- genuinely global across all connected
# Shopee shops, so these stay as env vars.
SHOPEE_PARTNER_ID = os.getenv("SHOPEE_PARTNER_ID")
SHOPEE_PARTNER_KEY = os.getenv("SHOPEE_PARTNER_KEY")


class ShopeeNotConfigured(HTTPException):
    def __init__(self, detail: str = "Integrasi Shopee belum dikonfigurasi. Isi SHOPEE_PARTNER_ID/SHOPEE_PARTNER_KEY setelah aplikasi partner disetujui."):
        super().__init__(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


def _is_app_configured() -> bool:
    return bool(SHOPEE_PARTNER_ID and SHOPEE_PARTNER_KEY)


def _is_akun_configured(akun: Any) -> bool:
    """`akun` is expected to be an AkunMarketplace row (or anything with
    the same attributes) -- per-shop credentials issued after OAuth."""
    return bool(getattr(akun, "access_token", None) and getattr(akun, "id_toko_eksternal", None))


async def sync_produk(akun: Any) -> list[dict]:
    """Pull one Shopee shop's catalog, using `akun`'s stored access_token/
    shop_id. NOT IMPLEMENTED -- see module docstring."""
    if not _is_app_configured() or not _is_akun_configured(akun):
        raise ShopeeNotConfigured()
    raise NotImplementedError("Shopee sync_produk() belum diimplementasikan -- verifikasi bentuk API dulu.")


async def sync_pesanan(akun: Any) -> list[dict]:
    """Pull recent orders for one Shopee shop. NOT IMPLEMENTED -- see
    module docstring."""
    if not _is_app_configured() or not _is_akun_configured(akun):
        raise ShopeeNotConfigured()
    raise NotImplementedError("Shopee sync_pesanan() belum diimplementasikan -- verifikasi bentuk API dulu.")


async def sync_chat(akun: Any) -> list[dict]:
    """Pull recent buyer chat messages for one Shopee shop. NOT
    IMPLEMENTED -- see module docstring."""
    if not _is_app_configured() or not _is_akun_configured(akun):
        raise ShopeeNotConfigured()
    raise NotImplementedError("Shopee sync_chat() belum diimplementasikan -- verifikasi bentuk API dulu.")


async def proses_pesanan(akun: Any, pesanan: Any) -> None:
    """Push a local "to_ship" ("Proses Pesanan") acknowledgement for one
    order to Shopee (e.g. SetOrderReadyToShip / ship endpoint). NOT
    IMPLEMENTED -- see module docstring. Caller (erp.application.services)
    treats this as soft-fail: whatever this raises is caught and recorded
    on the PesananERP row, the local status change is applied regardless."""
    if not _is_app_configured() or not _is_akun_configured(akun):
        raise ShopeeNotConfigured()
    raise NotImplementedError("Shopee proses_pesanan() belum diimplementasikan -- verifikasi bentuk API dulu.")
