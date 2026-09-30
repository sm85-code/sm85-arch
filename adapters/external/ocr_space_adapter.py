"""OCR.space adapter.

Used for OCR instead of Google Cloud Vision because Google mandates a
verified credit/debit card + billing account on the project even for
free-tier usage, which this customer couldn't/didn't want to set up.
OCR.space's free tier needs only an email address -- no card, no billing
account -- for 25,000 requests/month, never expires.

Auth: a single API key (OCR_SPACE_API_KEY) from https://ocr.space/ocrapi,
sent as a form field on a plain multipart REST call.

OCREngine=2 is used because it's OCR.space's more accurate engine for dense
structured documents (vs the default engine 1, tuned for short/simple
text) -- the same reasoning DOCUMENT_TEXT_DETECTION was picked over
TEXT_DETECTION on the Vision adapter.

This module returns raw OCR text only. Turning that text into structured
Kartu Keluarga fields is NOT done here -- see
tenants/madrasah/modules/madrasah/application/kk_parser.py, which is
engine-agnostic (same parser is used for both adapters' output).
"""
from __future__ import annotations

import asyncio
import os
from typing import Optional

import requests

_PARSE_URL = "https://api.ocr.space/parse/image"

# Same rationale as gdrive_adapter.py's _REQUEST_TIMEOUT_SECONDS: without an
# explicit timeout a stalled call hangs until the platform's gateway kills
# it with a bare 504, instead of failing fast with a clear error.
_REQUEST_TIMEOUT_SECONDS = 25


def _api_key() -> Optional[str]:
    return os.getenv("OCR_SPACE_API_KEY")


def is_configured() -> bool:
    return bool(_api_key())


def _parse_sync(image_bytes: bytes, file_name: str) -> str:
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("Set OCR_SPACE_API_KEY first")

    response = requests.post(
        _PARSE_URL,
        data={
            "apikey": api_key,
            # OCR.space rejects "ind" (Indonesian isn't in OCREngine=2's
            # supported language list -- confirmed via their API, error
            # "E201: Value for parameter 'language' is invalid"). "auto"
            # works fine for Indonesian's Latin script.
            "language": "auto",
            "OCREngine": 2,
            "isOverlayRequired": False,
            "scale": True,
        },
        files={"file": (file_name or "document.jpg", image_bytes)},
        timeout=(10, _REQUEST_TIMEOUT_SECONDS),
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("IsErroredOnProcessing"):
        message = payload.get("ErrorMessage") or payload.get("ErrorDetails") or "OCR.space returned an error"
        if isinstance(message, list):
            message = "; ".join(str(m) for m in message)
        raise RuntimeError(f"OCR.space error: {message}")

    results = payload.get("ParsedResults") or []
    return "\n".join(str(r.get("ParsedText") or "") for r in results).strip()


async def detect_document_text(image_bytes: bytes, file_name: str = "document.jpg") -> str:
    """OCRs a photo of a printed document (e.g. a Kartu Keluarga) and
    returns the full extracted text, or "" if nothing was detected."""
    return await asyncio.to_thread(_parse_sync, image_bytes, file_name)
