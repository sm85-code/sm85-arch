"""Blibli adapter -- PLACEHOLDER for marketplace_erp Tahap 2.

Shopee is first; this file keeps the same surface (sync_produk / sync_pesanan /
proses_pesanan) so OMS soft-fail dispatch stays uniform. Fill BLIBLI_CLIENT_ID/
BLIBLI_CLIENT_SECRET and implement OAuth once credentials exist -- never commit secrets.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException, status

BLIBLI_CLIENT_ID = os.getenv("BLIBLI_CLIENT_ID", "").strip()
BLIBLI_CLIENT_SECRET = os.getenv("BLIBLI_CLIENT_SECRET", "").strip()


class BlibliNotConfigured(HTTPException):
    def __init__(
        self,
        detail: str = (
            "Integrasi Blibli belum dikonfigurasi. "
            "Isi BLIBLI_CLIENT_ID/BLIBLI_CLIENT_SECRET setelah aplikasi partner disetujui."
        ),
    ):
        super().__init__(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=detail)


def _configured() -> bool:
    return bool(BLIBLI_CLIENT_ID and BLIBLI_CLIENT_SECRET)


def _akun_ok(akun: Any) -> bool:
    return bool(getattr(akun, "access_token", None))


async def sync_produk(akun: Any) -> list[dict]:
    if not _configured() or not _akun_ok(akun):
        raise BlibliNotConfigured()
    raise NotImplementedError("Blibli sync_produk() placeholder -- Shopee first.")


async def sync_pesanan(akun: Any) -> list[dict]:
    if not _configured() or not _akun_ok(akun):
        raise BlibliNotConfigured()
    raise NotImplementedError("Blibli sync_pesanan() placeholder -- Shopee first.")


async def proses_pesanan(akun: Any, pesanan: Any) -> None:
    if not _configured() or not _akun_ok(akun):
        raise BlibliNotConfigured()
    raise NotImplementedError("Blibli proses_pesanan() placeholder -- Shopee first.")
