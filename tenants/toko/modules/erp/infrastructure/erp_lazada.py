"""Lazada Open Platform sync adapter -- PLACEHOLDER.

STATUS: not wired to the real Lazada API yet -- same reasoning as
erp_shopee.py: no app key/secret has been issued yet. Every call below
fails loudly (NotImplementedError) instead of faking a sync.

TODO once you have Lazada Open Platform app credentials:
  1. Fill LAZADA_APP_KEY / LAZADA_APP_SECRET / LAZADA_ACCESS_TOKEN in .env.
  2. Confirm the current Lazada Open API auth scheme (HMAC-SHA256 over the
     sorted request params, signed with app_secret) against
     https://open.lazada.com/doc -- unreachable from this session's
     network, must be checked from wherever you read this.
  3. Implement product pull (/products/get), order pull (/orders/get +
     /order/items/get) and chat pull (Lazada's messaging center API)
     mapped into ProdukERP / PesananERP+ItemPesananERP /
     PercakapanERP+PesanChatERP rows (upsert keyed on
     (platform="lazada", id_eksternal)).
  4. Map Lazada's own order status values (pending/ready_to_ship/shipped/
     delivered/canceled/...) to STATUS_PESANAN_ERP -- see the mapping
     rationale in infrastructure/models.py.
  5. Remove the NotConfigured guards once verified end-to-end against a
     Lazada test seller account.
"""
from __future__ import annotations

import os

from fastapi import HTTPException, status

LAZADA_APP_KEY = os.getenv("LAZADA_APP_KEY")
LAZADA_APP_SECRET = os.getenv("LAZADA_APP_SECRET")
LAZADA_ACCESS_TOKEN = os.getenv("LAZADA_ACCESS_TOKEN")


class LazadaNotConfigured(HTTPException):
    def __init__(self, detail: str = "Integrasi Lazada belum dikonfigurasi. Isi LAZADA_APP_KEY/LAZADA_APP_SECRET/LAZADA_ACCESS_TOKEN setelah aplikasi partner disetujui."):
        super().__init__(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


def _is_configured() -> bool:
    return bool(LAZADA_APP_KEY and LAZADA_APP_SECRET and LAZADA_ACCESS_TOKEN)


async def sync_produk() -> list[dict]:
    """Pull the seller's Lazada catalog. NOT IMPLEMENTED -- see module
    docstring."""
    if not _is_configured():
        raise LazadaNotConfigured()
    raise NotImplementedError("Lazada sync_produk() belum diimplementasikan -- verifikasi bentuk API dulu.")


async def sync_pesanan() -> list[dict]:
    """Pull recent Lazada orders. NOT IMPLEMENTED -- see module docstring."""
    if not _is_configured():
        raise LazadaNotConfigured()
    raise NotImplementedError("Lazada sync_pesanan() belum diimplementasikan -- verifikasi bentuk API dulu.")


async def sync_chat() -> list[dict]:
    """Pull recent Lazada buyer chat messages. NOT IMPLEMENTED -- see module
    docstring."""
    if not _is_configured():
        raise LazadaNotConfigured()
    raise NotImplementedError("Lazada sync_chat() belum diimplementasikan -- verifikasi bentuk API dulu.")
