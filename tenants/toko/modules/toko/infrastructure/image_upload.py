"""Product photo upload for the toko module.

Reuses the existing generic Google Drive adapter (adapters/external/
gdrive_adapter.py, already used by the BUMDes reporting side) instead of
local disk: this app deploys via Procfile to a Heroku-style/App Platform
target with an ephemeral filesystem, so anything saved to local disk is
lost on the next deploy or dyno restart.

Uses its OWN folder (GDRIVE_FOLDER_ID_TOKO), separate from the BUMDes
reports folder (GDRIVE_FOLDER_ID), so product photos don't end up mixed
in with financial report exports.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException, status

from adapters.external.gdrive_adapter import set_gdrive_file_public, upload_file_to_gdrive

GDRIVE_FOLDER_ID_TOKO = os.getenv("GDRIVE_FOLDER_ID_TOKO")

_ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB

_NAMA_FILE_PREFIX = "ampelkuning"
_CONTENT_TYPE_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def build_nama_file_foto(content_type: str) -> str:
    """Builds a standardized filename for every photo we upload to our own
    Google Drive: ampelkuning_yyyymmdd_random.ext. `content_type` must
    already be validated against _ALLOWED_CONTENT_TYPES -- the extension is
    always derived from it, never trusted from any caller-supplied filename,
    so the extension is guaranteed to match the actual file content."""
    ext = _CONTENT_TYPE_TO_EXT[content_type]
    tanggal = datetime.now(timezone.utc).strftime("%Y%m%d")
    random_part = secrets.token_hex(4)
    return f"{_NAMA_FILE_PREFIX}_{tanggal}_{random_part}{ext}"


class UploadNotConfigured(HTTPException):
    def __init__(self):
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Upload foto produk belum dikonfigurasi (GDRIVE_FOLDER_ID_TOKO / GDRIVE_SERVICE_ACCOUNT_JSON belum diisi).",
        )


def _is_configured() -> bool:
    has_creds = bool(os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))
    return bool(GDRIVE_FOLDER_ID_TOKO and has_creds)


async def upload_produk_photo(file_bytes: bytes, content_type: str) -> str:
    """Uploads to the toko Drive folder, makes it publicly viewable, and
    returns a direct embeddable image URL (not the Drive web viewer link,
    which can't be used in an <img> tag). The uploaded filename is always
    generated internally (see build_nama_file_foto) -- callers no longer
    control it, so every file in GDRIVE_FOLDER_ID_TOKO follows the same
    ampelkuning_yyyymmdd_random.ext pattern regardless of upload path."""
    if not _is_configured():
        raise UploadNotConfigured()
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Format foto harus JPEG, PNG, atau WebP",
        )
    if len(file_bytes) > _MAX_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ukuran foto maksimal 5 MB")

    file_name = build_nama_file_foto(content_type)
    result = await upload_file_to_gdrive(file_bytes, file_name, content_type, folder_id=GDRIVE_FOLDER_ID_TOKO)
    file_id = result["id"]
    await set_gdrive_file_public(file_id)

    return f"https://drive.google.com/uc?export=view&id={file_id}"
