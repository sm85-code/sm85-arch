"""Copy a product photo from a public https URL (e.g. a marketplace CDN)
into the media bucket.

The URL comes from another system's data, so it is treated as untrusted:
https only, no redirects, host must resolve only to public addresses (no
loopback/private/link-local -- blocks SSRF to internal services), short
timeout and a hard size cap while streaming. Anything wrong returns None --
a missing photo must never block publishing a product.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlsplit

import requests

from tenants.store.modules.store.infrastructure.media_storage import upload_produk_photo

logger = logging.getLogger(__name__)

_MAX_BYTES = 5 * 1024 * 1024
_TIMEOUT = (3, 8)  # connect, read seconds


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return True


def _download_sync(url: str) -> tuple[bytes, str] | None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    if parts.port not in (None, 443) or not _public_host(parts.hostname):
        return None
    with requests.get(url, stream=True, timeout=_TIMEOUT, allow_redirects=False) as resp:
        if resp.status_code != 200:
            return None
        content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        data = bytearray()
        for chunk in resp.iter_content(64 * 1024):
            data.extend(chunk)
            if len(data) > _MAX_BYTES:
                return None
    return bytes(data), content_type


async def import_foto_dari_url(url: str | None) -> str | None:
    """Returns the new object key, or None when the photo could not be copied."""
    if not url:
        return None
    try:
        downloaded = await asyncio.to_thread(_download_sync, url)
        if downloaded is None:
            return None
        data, content_type = downloaded
        key = await upload_produk_photo(data, content_type, deduplicate=True)
        from tenants.store.modules.store.application.media_cleanup import record_upload
        await record_upload(key)
        return key
    except Exception:  # noqa: BLE001 -- best effort by design, details go to the log
        logger.warning("photo import skipped for url host=%s", urlsplit(url).hostname, exc_info=True)
        return None
