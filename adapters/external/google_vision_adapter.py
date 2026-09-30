"""Google Cloud Vision OCR adapter.

Used to read printed documents (e.g. Kartu Keluarga photos) via the Vision
API's ``DOCUMENT_TEXT_DETECTION`` feature, which is tuned for dense printed
text (as opposed to ``TEXT_DETECTION``, meant for short/scene text).

Auth: a single Google Cloud API key (``GOOGLE_VISION_API_KEY``) restricted
to the Vision API, sent as a query param on a plain REST call. No service
account / OAuth needed here -- unlike gdrive_adapter.py, Vision's
``images:annotate`` endpoint has no per-user quota or file-ownership
concept, it's a stateless one-off image analysis call, so an API key is the
simplest correct auth mode (and the one Google itself recommends for this
endpoint).

Free tier: 1,000 units/month per feature, never expires; $1.50/1,000 units
beyond that (as of 2026). This module only ever calls one feature
(``DOCUMENT_TEXT_DETECTION``), so 1,000 KK photos/month are free.

This module returns raw OCR text only. Turning that text into structured
Kartu Keluarga fields (nama, NIK, tempat/tanggal lahir, ...) is NOT done by
Vision -- see ``tenants/madrasah/modules/madrasah/application/kk_parser.py``
for that logic.
"""
from __future__ import annotations

import asyncio
import base64
import os
from typing import Any, Optional

import requests

_ANNOTATE_URL = "https://vision.googleapis.com/v1/images:annotate"

# Same rationale as gdrive_adapter.py's _REQUEST_TIMEOUT_SECONDS: without an
# explicit timeout a stalled call hangs until the platform's gateway kills
# it with a bare 504, instead of failing fast with a clear error.
_REQUEST_TIMEOUT_SECONDS = 25


def _api_key() -> Optional[str]:
    return os.getenv("GOOGLE_VISION_API_KEY")


def is_configured() -> bool:
    return bool(_api_key())


def _annotate_sync(image_bytes: bytes) -> dict[str, Any]:
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("Set GOOGLE_VISION_API_KEY first")

    body = {
        "requests": [
            {
                "image": {"content": base64.b64encode(image_bytes).decode("ascii")},
                "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                "imageContext": {"languageHints": ["id"]},
            }
        ]
    }
    response = requests.post(
        _ANNOTATE_URL,
        params={"key": api_key},
        json=body,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    result = (payload.get("responses") or [{}])[0]
    if "error" in result:
        message = result["error"].get("message") or "Vision API returned an error"
        raise RuntimeError(f"Google Vision error: {message}")
    return result


async def detect_document_text(image_bytes: bytes) -> str:
    """OCRs a photo of a printed document (e.g. a Kartu Keluarga) and
    returns the full extracted text, or "" if nothing was detected."""
    result = await asyncio.to_thread(_annotate_sync, image_bytes)
    annotation = result.get("fullTextAnnotation") or {}
    return str(annotation.get("text") or "")
