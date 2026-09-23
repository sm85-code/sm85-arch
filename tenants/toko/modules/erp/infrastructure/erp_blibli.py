"""Blibli Seller Center (BliMart/BSC) sync adapter -- PLACEHOLDER.

STATUS: not wired to the real Blibli API yet -- same reasoning as
erp_shopee.py / erp_lazada.py: no client id/secret has been issued yet.
Every call below fails loudly (NotImplementedError) instead of faking a
sync.

Credential model (per-account, not a single global env var): a Blibli
Seller Center integration has ONE global client_id/client_secret (issued
once, genuinely global across all connected merchants -- stays as env
vars below). Each MERCHANT account the app is authorized against then
gets its own access_token/refresh_token from the OAuth flow -- that
per-merchant data is what AkunMarketplace (toko_erp_akun, see
infrastructure/models.py) now stores in the DB instead of the old
BLIBLI_MERCHANT_CODE-style single global env var, because a seller can
have several Blibli merchant accounts connected at once. Every sync_*
function below therefore takes an `akun` (an AkunMarketplace row, or an
object/mapping shaped like one) and is meant to be called once per active
Blibli AkunMarketplace, e.g.:

    for akun in await services.list_akun_marketplace(session, platform="blibli"):
        if akun.status == "aktif":
            await sync_produk(akun)

TODO once you have Blibli Seller Center API credentials:
  1. Fill BLIBLI_CLIENT_ID / BLIBLI_CLIENT_SECRET in .env (app-level,
     global across all connected merchants).
  2. Confirm the current Blibli Seller API auth scheme (signature/HMAC
     over the request, per merchant onboarding docs) against
     https://dev.blibli.com -- unreachable from this session's network,
     must be checked from wherever you read this.
  3. Implement the real OAuth authorization callback that fills in an
     AkunMarketplace row's id_toko_eksternal (merchant code)/
     access_token/refresh_token/token_kedaluwarsa once a merchant
     approves the app (for now those fields are filled manually via the
     admin PATCH /api/toko/marketplace/admin/akun/{id} endpoint, see
     erp_router.py).
  4. Implement product pull, order pull and chat pull mapped into
     ProdukERP / PesananERP+ItemPesananERP / PercakapanERP+PesanChatERP
     rows (upsert keyed on (platform="blibli", id_eksternal), each row
     tagged with the akun.id passed in).
  5. Map Blibli's own order status values to STATUS_PESANAN_ERP -- see the
     mapping rationale in infrastructure/models.py.
  6. Remove the NotConfigured guards once verified end-to-end against a
     Blibli test merchant account.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException, status

# App-level credentials -- genuinely global across all connected Blibli
# merchants, so these stay as env vars.
BLIBLI_CLIENT_ID = os.getenv("BLIBLI_CLIENT_ID")
BLIBLI_CLIENT_SECRET = os.getenv("BLIBLI_CLIENT_SECRET")


class BlibliNotConfigured(HTTPException):
    def __init__(self, detail: str = "Integrasi Blibli belum dikonfigurasi. Isi BLIBLI_CLIENT_ID/BLIBLI_CLIENT_SECRET setelah akun merchant terverifikasi."):
        super().__init__(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


def _is_app_configured() -> bool:
    return bool(BLIBLI_CLIENT_ID and BLIBLI_CLIENT_SECRET)


def _is_akun_configured(akun: Any) -> bool:
    """`akun` is expected to be an AkunMarketplace row (or anything with
    the same attributes) -- per-merchant credentials issued after OAuth."""
    return bool(getattr(akun, "access_token", None) and getattr(akun, "id_toko_eksternal", None))


async def sync_produk(akun: Any) -> list[dict]:
    """Pull one Blibli merchant's catalog, using `akun`'s stored
    access_token/merchant code. NOT IMPLEMENTED -- see module docstring."""
    if not _is_app_configured() or not _is_akun_configured(akun):
        raise BlibliNotConfigured()
    raise NotImplementedError("Blibli sync_produk() belum diimplementasikan -- verifikasi bentuk API dulu.")


async def sync_pesanan(akun: Any) -> list[dict]:
    """Pull recent orders for one Blibli merchant. NOT IMPLEMENTED -- see
    module docstring."""
    if not _is_app_configured() or not _is_akun_configured(akun):
        raise BlibliNotConfigured()
    raise NotImplementedError("Blibli sync_pesanan() belum diimplementasikan -- verifikasi bentuk API dulu.")


async def sync_chat(akun: Any) -> list[dict]:
    """Pull recent buyer chat messages for one Blibli merchant. NOT
    IMPLEMENTED -- see module docstring."""
    if not _is_app_configured() or not _is_akun_configured(akun):
        raise BlibliNotConfigured()
    raise NotImplementedError("Blibli sync_chat() belum diimplementasikan -- verifikasi bentuk API dulu.")


async def proses_pesanan(akun: Any, pesanan: Any) -> None:
    """Push a local "to_ship" ("Proses Pesanan") acknowledgement for one
    order to Blibli. NOT IMPLEMENTED -- see module docstring. Caller
    (erp.application.services) treats this as soft-fail: whatever this
    raises is caught and recorded on the PesananERP row, the local status
    change is applied regardless."""
    if not _is_app_configured() or not _is_akun_configured(akun):
        raise BlibliNotConfigured()
    raise NotImplementedError("Blibli proses_pesanan() belum diimplementasikan -- verifikasi bentuk API dulu.")
