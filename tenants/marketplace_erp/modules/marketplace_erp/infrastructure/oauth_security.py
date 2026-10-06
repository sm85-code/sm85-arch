"""Persistent, owner-session-bound, single-use Shopee authorization requests."""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException
from sqlalchemy import delete, update

from shared.config import origin_allowed
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import ShopeeOAuthRequest

TTL = timedelta(minutes=10)


def callback_url(base: str, akun_id: str, nonce: str, configured: str) -> str:
    """Nonce travels in the redirect path; no assumption that Shopee echoes OAuth state.

    Support the existing FE callback root and API callback root. Caller-provided
    destinations must use a configured CORS origin or the operator's exact URI.
    """
    parts = urlsplit(base)
    origin = f"{parts.scheme}://{parts.netloc}"
    if (
        parts.scheme not in {"https", "http"}
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or (base != configured and not origin_allowed(origin))
        or (parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1"})
    ):
        raise HTTPException(status_code=400, detail="URL callback Shopee tidak diizinkan")
    path = parts.path.rstrip("/")
    root = "/oauth/shopee/callback"
    if path.endswith(root):
        path += f"/{akun_id}"
    elif not path.endswith(f"{root}/{akun_id}"):
        raise HTTPException(status_code=400, detail="Path callback Shopee tidak valid")
    return urlunsplit((parts.scheme, parts.netloc, f"{path}/{nonce}", "", ""))


async def issue_nonce(session, akun_id: str, user) -> str:
    now = datetime.now(timezone.utc)
    await session.execute(delete(ShopeeOAuthRequest).where(ShopeeOAuthRequest.expires_at <= now))
    nonce = secrets.token_urlsafe(32)
    session.add(ShopeeOAuthRequest(
        nonce_hash=hashlib.sha256(nonce.encode()).hexdigest(),
        akun_id=akun_id,
        user_id=user.id,
        session_version=user.session_version,
        expires_at=now + TTL,
        consumed=False,
    ))
    await session.flush()
    return nonce


async def consume_nonce(session, akun_id: str, nonce: str | None, user) -> None:
    if not nonce or not re.fullmatch(r"[A-Za-z0-9_-]{43}", nonce):
        raise HTTPException(status_code=400, detail="Otorisasi Shopee tidak valid. Hubungkan ulang toko.")
    # One conditional UPDATE is the concurrency boundary on PostgreSQL/SQLite.
    result = await session.execute(
        update(ShopeeOAuthRequest).where(
            ShopeeOAuthRequest.nonce_hash == hashlib.sha256(nonce.encode()).hexdigest(),
            ShopeeOAuthRequest.akun_id == akun_id,
            ShopeeOAuthRequest.user_id == user.id,
            ShopeeOAuthRequest.session_version == user.session_version,
            ShopeeOAuthRequest.expires_at > datetime.now(timezone.utc),
            ShopeeOAuthRequest.consumed.is_(False),
        ).values(consumed=True).returning(ShopeeOAuthRequest.nonce_hash)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=400, detail="Otorisasi Shopee kedaluwarsa atau sudah digunakan. Hubungkan ulang toko.")
    # Persist before token exchange: a failed exchange cannot make this nonce reusable.
    await session.commit()
