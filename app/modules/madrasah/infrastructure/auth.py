"""Authentication dependencies isolated to the madrasah module.

This file is new and does not modify anything under shared/, adapters/api/deps.py,
or adapters/api/scope.py (the BUMDes auth stack). It reuses only the generic
JWT encode/decode primitives from shared.security, but never the BUMDes-specific
cookie name (JWT_COOKIE_NAME/bumdes_token) or set_auth_cookie/clear_auth_cookie/
token_from_request helpers, since those are hardcoded to that cookie name and
would collide with the already-live BUMDes frontend session in the same browser.

Uses its own cookie name (MADRASAH_COOKIE_NAME = "madrasah_token") so a madrasah
login/logout never touches the BUMDes session cookie, and vice versa.
"""
from __future__ import annotations

from typing import Callable

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.madrasah.infrastructure.database import get_db_madrasah
from app.modules.madrasah.infrastructure.models import UserMadrasah
from shared.config import COOKIE_PATH, COOKIE_SAMESITE, COOKIE_SECURE, JWT_EXPIRE_HOURS
from shared.security import create_access_token, decode_access_token

MADRASAH_COOKIE_NAME = "madrasah_token"


def issue_madrasah_token(user: UserMadrasah) -> str:
    """Create a JWT for a madrasah user, reusing the shared encode primitive.

    session_version is fixed at 0 since UserMadrasah has no session_version
    column (unlike the BUMDes User model) -- there is currently no
    forced-logout-all-sessions feature for madrasah accounts.
    """
    return create_access_token(subject=user.id, role=user.role, session_version=0)


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
    return user


def require_roles_madrasah(*roles: str) -> Callable:
    allowed = {r.strip().lower() for r in roles}

    async def _inner(user: UserMadrasah = Depends(get_current_user_madrasah)) -> UserMadrasah:
        if (user.role or "").strip().lower() not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akses ditolak")
        return user

    return _inner
