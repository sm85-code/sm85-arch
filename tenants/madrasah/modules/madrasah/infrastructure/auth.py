"""Authentication dependencies isolated to the madrasah module.

This file is new and does not modify anything under shared/, modules/siabumdes/adapters/api/deps.py,
or modules/siabumdes/adapters/api/scope.py (the BUMDes auth stack). It reuses only the generic
JWT encode/decode primitives from shared.security, but never the BUMDes-specific
cookie name (JWT_COOKIE_NAME/bumdes_token) or set_auth_cookie/clear_auth_cookie/
token_from_request helpers, since those are hardcoded to that cookie name and
would collide with the already-live BUMDes frontend session in the same browser.

Uses its own cookie name (MADRASAH_COOKIE_NAME = "madrasah_token") so a madrasah
login/logout never touches the BUMDes session cookie, and vice versa.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Callable

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.madrasah.modules.madrasah.infrastructure.database import get_db_madrasah
from tenants.madrasah.modules.madrasah.infrastructure.models import UserMadrasah
from shared.config import COOKIE_PATH, COOKIE_SAMESITE, COOKIE_SECURE, JWT_EXPIRE_HOURS
from shared.security import create_access_token, decode_access_token

MADRASAH_COOKIE_NAME = "madrasah_token"

# In-memory login throttle, isolated to this module (single-process deploy,
# see Procfile). Keyed by "ip:no_hp" so a brute-force run against one account
# from one source is capped without needing a new dependency (Redis/slowapi).
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300
_login_attempts: dict[str, list[float]] = defaultdict(list)


def _login_throttle_key(request: Request, no_hp: str) -> str:
    return f"{request.client.host if request.client else 'unknown'}:{no_hp.strip()}"


def check_login_rate_limit(request: Request, no_hp: str) -> None:
    key = _login_throttle_key(request, no_hp)
    now = time.monotonic()
    attempts = [t for t in _login_attempts[key] if now - t < LOGIN_LOCKOUT_SECONDS]
    _login_attempts[key] = attempts
    if len(attempts) >= MAX_LOGIN_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Terlalu banyak percobaan login. Coba lagi beberapa menit.",
        )


def record_failed_login(request: Request, no_hp: str) -> None:
    key = _login_throttle_key(request, no_hp)
    _login_attempts[key].append(time.monotonic())


def reset_login_attempts(request: Request, no_hp: str) -> None:
    _login_attempts.pop(_login_throttle_key(request, no_hp), None)


def issue_madrasah_token(user: UserMadrasah) -> str:
    """Create a JWT for a madrasah user, reusing the shared encode primitive.

    session_version is now taken from UserMadrasah.session_version (mirrors
    the BUMDes User model). Bumping that column -- done in
    services.patch_guru whenever a password or role changes -- invalidates
    every JWT issued before the bump on its next request, without needing a
    token blacklist table.
    """
    return create_access_token(subject=user.id, role=user.role, session_version=user.session_version)


def set_madrasah_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=MADRASAH_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        max_age=JWT_EXPIRE_HOURS * 3600,
        path=COOKIE_PATH,
    )


def clear_madrasah_cookie(response: Response) -> None:
    response.delete_cookie(
        key=MADRASAH_COOKIE_NAME,
        path=COOKIE_PATH,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
    )


def _token_from_request(request: Request) -> str:
    cookie = request.cookies.get(MADRASAH_COOKIE_NAME)
    if cookie:
        return cookie
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header.split(" ", 1)[1].strip()
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Tidak terautentikasi")


async def get_current_user_madrasah(
    request: Request,
    session: AsyncSession = Depends(get_db_madrasah),
) -> UserMadrasah:
    payload = decode_access_token(_token_from_request(request))
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesi tidak valid")
    user = await session.get(UserMadrasah, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesi tidak valid")
    # Token dikeluarkan sebelum password/role terakhir diubah (sv lama) --
    # tolak, supaya token yang bocor/lama tidak bisa dipakai lagi setelah
    # pemilik akun mengganti password. Lihat services.patch_guru.
    if int(payload.get("sv", 0)) != user.session_version:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesi tidak valid, silakan login ulang")
    return user


def require_roles_madrasah(*roles: str) -> Callable:
    allowed = {r.strip().lower() for r in roles}

    async def _inner(user: UserMadrasah = Depends(get_current_user_madrasah)) -> UserMadrasah:
        if (user.role or "").strip().lower() not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akses ditolak")
        return user

    return _inner
