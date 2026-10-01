"""Product photo storage -- Cloudflare R2 PLACEHOLDER.

R2 is not subscribed yet, so uploads fail with HTTP 503 until it is wired.
What is already final: validation, the generated file name, and the fact
that the database stores only the object KEY (ProdukStore.foto_key); the
public URL is MEDIA_BASE_URL + "/" + key, so switching storage or domain
later is an env change, not a data migration.

TODO: implement _put_object() against the R2 S3 API (R2_ACCOUNT_ID,
R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET) and set MEDIA_BASE_URL
(e.g. https://img.ampelkuning.com).
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status

_ALLOWED_CONTENT_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_MAX_BYTES = 5 * 1024 * 1024


class MediaNotReady(HTTPException):
    def __init__(self):
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Penyimpanan foto (Cloudflare R2) belum aktif.",
        )


def build_nama_file_foto(content_type: str) -> str:
    """ampelkuning_yyyymmdd_random.ext -- generated here, never by callers."""
    ext = _ALLOWED_CONTENT_TYPES[content_type]
    return f"ampelkuning_{datetime.now(timezone.utc):%Y%m%d}_{secrets.token_hex(6)}.{ext}"


def media_url(key: Optional[str]) -> Optional[str]:
    base = (os.getenv("MEDIA_BASE_URL") or "").strip().rstrip("/")
    if not key or not base:
        return None
    return f"{base}/{key}"


async def upload_produk_photo(file_bytes: bytes, content_type: str) -> str:
    """Validate and store a product photo; returns the object key."""
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Format foto harus JPEG, PNG, atau WebP")
    if len(file_bytes) > _MAX_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ukuran foto maksimal 5 MB")
    raise MediaNotReady()
