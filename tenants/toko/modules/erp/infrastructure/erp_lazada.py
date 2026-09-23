"""Lazada Open Platform sync adapter -- PLACEHOLDER.

STATUS: not wired to the real Lazada API yet -- same reasoning as
erp_shopee.py: no app key/secret has been issued yet. Every call below
fails loudly (NotImplementedError) instead of faking a sync.

Credential model (per-account, not a single global env var): a Lazada
Open Platform app has ONE global app_key/app_secret (issued once,
genuinely global across all connected sellers -- stays as env vars
below). Each SELLER account the app is authorized against then gets its
own access_token/refresh_token from the OAuth flow -- that per-seller
data is what AkunMarketplace (toko_erp_akun, see infrastructure/
models.py) now stores in the DB instead of the old LAZADA_ACCESS_TOKEN-
style single global env var, because a seller can have several Lazada
shops connected at once. Every sync_* function below therefore takes an
`akun` (an AkunMarketplace row, or an object/mapping shaped like one) and
is meant to be called once per active Lazada AkunMarketplace, e.g.:

    for akun in await services.list_akun_marketplace(session, platform="lazada"):
        if akun.status == "aktif":
            await sync_produk(akun)

TODO once you have Lazada Open Platform app credentials:
  1. Fill LAZADA_APP_KEY / LAZADA_APP_SECRET in .env (app-level, global
     across all connected sellers).
  2. Confirm the current Lazada Open API auth scheme (HMAC-SHA256 over the
     sorted request params, signed with app_secret) against
     https://open.lazada.com/doc -- unreachable from this session's
     network, must be checked from wherever you read this.
  3. Implement the real OAuth authorization callback that fills in an
     AkunMarketplace row's access_token/refresh_token/token_kedaluwarsa
     once a seller approves the app (for now those fields are filled
     manually via the admin PATCH /api/toko/marketplace/admin/akun/{id}
     endpoint, see erp_router.py).
  4. Implement product pull (/products/get), order pull (/orders/get +
     /order/items/get) and chat pull (Lazada's messaging center API)
     mapped into ProdukERP / PesananERP+ItemPesananERP /
     PercakapanERP+PesanChatERP rows (upsert keyed on
     (platform="lazada", id_eksternal), each row tagged with the
     akun.id passed in).
  5. Map Lazada's own order status values (pending/ready_to_ship/shipped/
     delivered/canceled/...) to STATUS_PESANAN_ERP -- see the mapping
     rationale in infrastructure/models.py.
  6. Remove the NotConfigured guards once verified end-to-end against a
     Lazada test seller account.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException, status

# App-level credentials -- genuinely global across all connected Lazada
# sellers, so these stay as env vars.
LAZADA_APP_KEY = os.getenv("LAZADA_APP_KEY")
LAZADA_APP_SECRET = os.getenv("LAZADA_APP_SECRET")


class LazadaNotConfigured(HTTPException):
    def __init__(self, detail: str = "Integrasi Lazada belum dikonfigurasi. Isi LAZADA_APP_KEY/LAZADA_APP_SECRET setelah aplikasi partner disetujui."):
        super().__init__(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


def _is_app_configured() -> bool:
    return bool(LAZADA_APP_KEY and LAZADA_APP_SECRET)


def _is_akun_configured(akun: Any) -> bool:
    """`akun` is expected to be an AkunMarketplace row (or anything with
    the same attributes) -- per-seller credentials issued after OAuth."""
    return bool(getattr(akun, "access_token", None))


async def sync_produk(akun: Any) -> list[dict]:
    """Pull one Lazada seller's catalog, using `akun`'s stored
    access_token. NOT IMPLEMENTED -- see module docstring."""
    if not _is_app_configured() or not _is_akun_configured(akun):
        raise LazadaNotConfigured()
    raise NotImplementedError("Lazada sync_produk() belum diimplementasikan -- verifikasi bentuk API dulu.")


async def sync_pesanan(akun: Any) -> list[dict]:
    """Pull recent orders for one Lazada seller. NOT IMPLEMENTED -- see
    module docstring."""
    if not _is_app_configured() or not _is_akun_configured(akun):
        raise LazadaNotConfigured()
    raise NotImplementedError("Lazada sync_pesanan() belum diimplementasikan -- verifikasi bentuk API dulu.")


async def sync_chat(akun: Any) -> list[dict]:
    """Pull recent buyer chat messages for one Lazada seller. NOT
    IMPLEMENTED -- see module docstring."""
    if not _is_app_configured() or not _is_akun_configured(akun):
        raise LazadaNotConfigured()
    raise NotImplementedError("Lazada sync_chat() belum diimplementasikan -- verifikasi bentuk API dulu.")


async def proses_pesanan(akun: Any, pesanan: Any) -> None:
    """Push a local "to_ship" ("Proses Pesanan") acknowledgement for one
    order to Lazada. NOT IMPLEMENTED -- see module docstring. Caller
    (erp.application.services) treats this as soft-fail: whatever this
    raises is caught and recorded on the PesananERP row, the local status
    change is applied regardless."""
    if not _is_app_configured() or not _is_akun_configured(akun):
        raise LazadaNotConfigured()
    raise NotImplementedError("Lazada proses_pesanan() belum diimplementasikan -- verifikasi bentuk API dulu.")
