"""Product photo storage on Cloudflare R2 (S3-compatible API).

The database stores only the object KEY (ProdukStore.foto_key); the public
URL is MEDIA_BASE_URL + "/" + key (the bucket's custom domain, e.g.
https://img.ampelkuning.com), so switching storage or domain later is an env
change, not a data migration.

Needs R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET.
Until all four are set, uploads answer HTTP 501 "not active yet" instead of
failing obscurely.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, NamedTuple, Optional

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

_ALLOWED_CONTENT_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_MAX_BYTES = 5 * 1024 * 1024
_KEY_PREFIX = "produk/"
_CHAT_PREFIX = "chat/"
_CHAT_TYPES = {
    "image/jpeg": ("jpg", "gambar"),
    "image/png": ("png", "gambar"),
    "image/webp": ("webp", "gambar"),
    "video/mp4": ("mp4", "video"),
    "video/webm": ("webm", "video"),
    "video/quicktime": ("mov", "video"),
}
_CHAT_MAX_IMAGE = 5 * 1024 * 1024
_CHAT_MAX_VIDEO = 20 * 1024 * 1024
# File names are random and never reused, so an object never changes: let browsers and
# Cloudflare keep it for a year.
_CACHE_CONTROL = "public, max-age=31536000, immutable"


class MediaNotReady(HTTPException):
    def __init__(self):
        super().__init__(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Penyimpanan foto (Cloudflare R2) belum aktif.",
        )


class R2Config(NamedTuple):
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str


def r2_config() -> Optional[R2Config]:
    values = [(os.getenv(name) or "").strip() for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")]
    return R2Config(*values) if all(values) else None


def build_nama_file_foto(content_type: str) -> str:
    """ampelkuning_yyyymmdd_random.ext -- generated here, never by callers."""
    ext = _ALLOWED_CONTENT_TYPES[content_type]
    return f"ampelkuning_{datetime.now(timezone.utc):%Y%m%d}_{secrets.token_hex(6)}.{ext}"


def media_url(key: Optional[str]) -> Optional[str]:
    base = (os.getenv("MEDIA_BASE_URL") or "").strip().rstrip("/")
    if not key or not base:
        return None
    return f"{base}/{key}"


def _looks_like(content_type: str, data: bytes) -> bool:
    """Check the real bytes: the Content-Type header is chosen by the client."""
    if content_type in ("video/mp4", "video/quicktime"):
        return data[4:8] == b"ftyp"
    if content_type == "video/webm":
        return data.startswith(b"\x1a\x45\xdf\xa3")
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


@lru_cache(maxsize=2)
def _client(cfg: R2Config) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{cfg.account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=cfg.access_key_id,
        aws_secret_access_key=cfg.secret_access_key,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=20,
        ),
    )


def _put_sync(cfg: R2Config, key: str, body: bytes, content_type: str) -> None:
    _client(cfg).put_object(
        Bucket=cfg.bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
        CacheControl=_CACHE_CONTROL,
    )


async def upload_produk_photo(file_bytes: bytes, content_type: str) -> str:
    """Validate and store a product photo; returns the object key."""
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Format foto harus JPEG, PNG, atau WebP")
    if len(file_bytes) > _MAX_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ukuran foto maksimal 5 MB")
    if not _looks_like(content_type, file_bytes):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Isi file bukan foto yang valid")
    cfg = r2_config()
    if cfg is None:
        raise MediaNotReady()

    key = f"{_KEY_PREFIX}{build_nama_file_foto(content_type)}"
    try:
        await asyncio.to_thread(_put_sync, cfg, key, file_bytes, content_type)
    except Exception:
        # Never leak bucket/credential details to the client; the cause is in the log.
        logger.exception("R2 upload failed key=%s", key)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Foto gagal disimpan. Coba lagi sebentar lagi.",
        ) from None
    return key


async def upload_chat_media(file_bytes: bytes, content_type: str) -> tuple[str, str]:
    """Validate and store a chat attachment (photo or short video); returns (object key, "gambar" | "video")."""
    if content_type not in _CHAT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Format harus JPEG, PNG, WebP, MP4, WebM, atau MOV"
        )
    ext, jenis = _CHAT_TYPES[content_type]
    limit = _CHAT_MAX_VIDEO if jenis == "video" else _CHAT_MAX_IMAGE
    if len(file_bytes) > limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ukuran {jenis} maksimal {limit // (1024 * 1024)} MB",
        )
    if not _looks_like(content_type, file_bytes):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Isi file bukan media yang valid")
    cfg = r2_config()
    if cfg is None:
        raise MediaNotReady()

    key = f"{_CHAT_PREFIX}ampelkuning_{datetime.now(timezone.utc):%Y%m%d}_{secrets.token_hex(8)}.{ext}"
    try:
        await asyncio.to_thread(_put_sync, cfg, key, file_bytes, content_type)
    except Exception:
        logger.exception("R2 chat upload failed key=%s", key)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Lampiran gagal disimpan. Coba lagi sebentar lagi."
        ) from None
    return key, jenis


def _delete_sync(cfg: R2Config, key: str) -> None:
    _client(cfg).delete_object(Bucket=cfg.bucket, Key=key)


async def delete_foto(key: Optional[str]) -> None:
    """Best-effort removal of a product photo that nothing points at any more.

    Never raises: a leftover file only costs a few KB, while failing here would
    turn a successful photo replacement into an error for the admin. Only keys
    under our own prefix are ever deleted.
    """
    cfg = r2_config()
    if not key or cfg is None or not key.startswith(_KEY_PREFIX):
        return
    try:
        await asyncio.to_thread(_delete_sync, cfg, key)
    except Exception:  # noqa: BLE001 -- details go to the log, not to the client
        logger.warning("R2 delete failed key=%s", key, exc_info=True)
