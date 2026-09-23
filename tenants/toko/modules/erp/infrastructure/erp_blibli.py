"""Blibli Seller Center (BliMart/BSC) sync adapter -- PLACEHOLDER.

STATUS: not wired to the real Blibli API yet -- same reasoning as
erp_shopee.py / erp_lazada.py: no client id/secret has been issued yet.
Every call below fails loudly (NotImplementedError) instead of faking a
sync.

TODO once you have Blibli Seller Center API credentials:
  1. Fill BLIBLI_CLIENT_ID / BLIBLI_CLIENT_SECRET / BLIBLI_MERCHANT_CODE
     in .env.
  2. Confirm the current Blibli Seller API auth scheme (signature/HMAC
     over the request, per merchant onboarding docs) against
     https://dev.blibli.com -- unreachable from this session's network,
     must be checked from wherever you read this.
  3. Implement product pull, order pull and chat pull mapped into
     ProdukERP / PesananERP+ItemPesananERP / PercakapanERP+PesanChatERP
     rows (upsert keyed on (platform="blibli", id_eksternal)).
  4. Map Blibli's own order status values to STATUS_PESANAN_ERP -- see the
     mapping rationale in infrastructure/models.py.
  5. Remove the NotConfigured guards once verified end-to-end against a
     Blibli test merchant account.
"""
from __future__ import annotations

import os

from fastapi import HTTPException, status

BLIBLI_CLIENT_ID = os.getenv("BLIBLI_CLIENT_ID")
BLIBLI_CLIENT_SECRET = os.getenv("BLIBLI_CLIENT_SECRET")
BLIBLI_MERCHANT_CODE = os.getenv("BLIBLI_MERCHANT_CODE")


class BlibliNotConfigured(HTTPException):
    def __init__(self, detail: str = "Integrasi Blibli belum dikonfigurasi. Isi BLIBLI_CLIENT_ID/BLIBLI_CLIENT_SECRET/BLIBLI_MERCHANT_CODE setelah akun merchant terverifikasi."):
        super().__init__(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


def _is_configured() -> bool:
    return bool(BLIBLI_CLIENT_ID and BLIBLI_CLIENT_SECRET and BLIBLI_MERCHANT_CODE)


async def sync_produk() -> list[dict]:
    """Pull the seller's Blibli catalog. NOT IMPLEMENTED -- see module
    docstring."""
    if not _is_configured():
        raise BlibliNotConfigured()
    raise NotImplementedError("Blibli sync_produk() belum diimplementasikan -- verifikasi bentuk API dulu.")


async def sync_pesanan() -> list[dict]:
    """Pull recent Blibli orders. NOT IMPLEMENTED -- see module docstring."""
    if not _is_configured():
        raise BlibliNotConfigured()
    raise NotImplementedError("Blibli sync_pesanan() belum diimplementasikan -- verifikasi bentuk API dulu.")


async def sync_chat() -> list[dict]:
    """Pull recent Blibli buyer chat messages. NOT IMPLEMENTED -- see module
    docstring."""
    if not _is_configured():
        raise BlibliNotConfigured()
    raise NotImplementedError("Blibli sync_chat() belum diimplementasikan -- verifikasi bentuk API dulu.")
