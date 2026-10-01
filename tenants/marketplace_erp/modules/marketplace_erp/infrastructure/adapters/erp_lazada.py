"""Lazada adapter -- PLACEHOLDER for marketplace_erp Tahap 2.

Shopee is first; this file keeps the same surface (sync_produk / sync_pesanan /
proses_pesanan) so OMS soft-fail dispatch stays uniform. Fill LAZADA_APP_KEY/
LAZADA_APP_SECRET and implement OAuth once credentials exist -- never commit secrets.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException, status

LAZADA_APP_KEY = os.getenv("LAZADA_APP_KEY", "").strip()
LAZADA_APP_SECRET = os.getenv("LAZADA_APP_SECRET", "").strip()


class LazadaNotConfigured(HTTPException):
    def __init__(
        self,
        detail: str = (
            "Integrasi Lazada belum dikonfigurasi. "
            "Isi LAZADA_APP_KEY/LAZADA_APP_SECRET setelah aplikasi partner disetujui."
        ),
    ):
        super().__init__(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=detail)


def _configured() -> bool:
    return bool(LAZADA_APP_KEY and LAZADA_APP_SECRET)


def _akun_ok(akun: Any) -> bool:
    return bool(getattr(akun, "access_token", None))


async def sync_produk(akun: Any) -> list[dict]:
    if not _configured() or not _akun_ok(akun):
        raise LazadaNotConfigured()
    raise NotImplementedError("Lazada sync_produk() placeholder -- Shopee first.")


async def sync_pesanan(akun: Any) -> list[dict]:
    if not _configured() or not _akun_ok(akun):
        raise LazadaNotConfigured()
    raise NotImplementedError("Lazada sync_pesanan() placeholder -- Shopee first.")


async def proses_pesanan(akun: Any, pesanan: Any) -> None:
    if not _configured() or not _akun_ok(akun):
        raise LazadaNotConfigured()
    raise NotImplementedError("Lazada proses_pesanan() placeholder -- Shopee first.")
