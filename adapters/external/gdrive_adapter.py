"""Google Drive adapter using a shared service account (no user OAuth)."""
from __future__ import annotations

import asyncio
import io
import json
import os
from typing import Any, Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

_SCOPES = ["https://www.googleapis.com/auth/drive"]


def is_configured() -> bool:
    raw = os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON")
    path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    folder = os.getenv("GDRIVE_FOLDER_ID")
    return bool(folder and (raw or (path and os.path.isfile(path))))


def _drive_service() -> Any:
    raw = os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON")
    path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if raw:
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
    elif path and os.path.isfile(path):
        creds = service_account.Credentials.from_service_account_file(path, scopes=_SCOPES)
    else:
        raise RuntimeError("Set GDRIVE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _upload_sync(
    file_bytes: bytes,
    file_name: str,
    mime_type: str,
    folder_id: Optional[str],
) -> dict[str, str]:
    target = folder_id or os.getenv("GDRIVE_FOLDER_ID")
    if not target:
        raise RuntimeError("folder_id or GDRIVE_FOLDER_ID is required")

    service = _drive_service()
    metadata = {"name": file_name, "parents": [target]}
    media = MediaIoBaseUpload(
        io.BytesIO(file_bytes),
        mimetype=mime_type or "application/octet-stream",
        resumable=False,
    )
    created = (
        service.files()
        .create(
            body=metadata,
            media_body=media,
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )
    file_id = created.get("id")
    if not file_id:
        raise RuntimeError("Drive upload returned no file id")
    return {
        "id": str(file_id),
        "name": str(created.get("name") or file_name),
        "webViewLink": str(created.get("webViewLink") or ""),
    }


def _delete_sync(file_id: str) -> None:
    service = _drive_service()
    try:
        service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
    except HttpError as exc:
        if getattr(exc, "status_code", None) == 404 or "404" in str(exc):
            return
        raise


def _exists_sync(file_id: str) -> bool:
    service = _drive_service()
    try:
        service.files().get(fileId=file_id, fields="id", supportsAllDrives=True).execute()
        return True
    except HttpError:
        return False


async def upload_file_to_gdrive(
    file_bytes: bytes,
    file_name: str,
    mime_type: str,
    folder_id: Optional[str] = None,
) -> dict[str, str]:
    """Upload bytes to the shared folder. Returns {id, name, webViewLink}."""
    return await asyncio.to_thread(_upload_sync, file_bytes, file_name, mime_type, folder_id)


async def delete_file_from_gdrive(file_id: str) -> None:
    await asyncio.to_thread(_delete_sync, file_id)


async def gdrive_file_exists(file_id: str) -> bool:
    return await asyncio.to_thread(_exists_sync, file_id)
