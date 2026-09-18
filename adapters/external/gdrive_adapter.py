"""Google Drive upload adapter (service account)."""
from __future__ import annotations

import asyncio
import io
import json
import os
from typing import Any, Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _drive_service() -> Any:
    raw = os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON")
    path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if raw:
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
    elif path and os.path.isfile(path):
        creds = service_account.Credentials.from_service_account_file(path, scopes=_SCOPES)
    else:
        raise RuntimeError(
            "Set GDRIVE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS"
        )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _upload_sync(
    file_bytes: bytes,
    file_name: str,
    mime_type: str,
    folder_id: Optional[str],
) -> str:
    target = folder_id or os.getenv("GDRIVE_FOLDER_ID")
    if not target:
        raise RuntimeError("folder_id or GDRIVE_FOLDER_ID is required")

    service = _drive_service()
    metadata = {"name": file_name, "parents": [target]}
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=False)
    created = (
        service.files()
        .create(body=metadata, media_body=media, fields="id", supportsAllDrives=True)
        .execute()
    )
    file_id = created.get("id")
    if not file_id:
        raise RuntimeError("Drive upload returned no file id")
    return str(file_id)


async def upload_file_to_gdrive(
    file_bytes: bytes,
    file_name: str,
    mime_type: str,
    folder_id: Optional[str] = None,
) -> str:
    """Upload bytes to Drive. Returns file id."""
    return await asyncio.to_thread(_upload_sync, file_bytes, file_name, mime_type, folder_id)
