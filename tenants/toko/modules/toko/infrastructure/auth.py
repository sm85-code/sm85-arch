"""Authentication dependencies isolated to the toko module.

Mirrors tenants/madrasah/modules/madrasah/infrastructure/auth.py: reuses
only the generic JWT encode/decode primitives from shared.security, never
the BUMDes/madrasah cookie names, so a toko login/logout never touches
another tenant's session cookie in the same browser.
"""
from __future__ import annotations

from typing import Callable

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import COOKIE_PATH, COOKIE_SAMESITE, COOKIE_SECURE, JWT_EXPIRE_HOURS
from shared.security import create_access_token, decode_access_token
from tenants.toko.modules.toko.infrastructure.database import get_db_toko
from tenants.toko.modules.toko.infrastructure.models import UserToko

TOKO_COOKIE_NAME = "toko_token"


def issue_toko_token(user: UserToko) -> str:
    return create_access_token(subject=user.id, role=user.role, session_version=0)


def set_toko_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=TOKO_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        max_age=JWT_EXPIRE_HOURS * 3600,
        path=COOKIE_PATH,
    )


def clear_toko_cookie(response: Response) -> None:
    response.delete_cookie(
        key=TOKO_COOKIE_NAME,
        path=COOKIE_PATH,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
    )


def _token_from_request(request: Request) -> str:
    cookie = request.cookies.get(TOKO_COOKIE_NAME)
    if cookie:
        return cookie
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header.split(" ", 1)[1].strip()
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Tidak terautentikasi")


async def get_current_user_toko(
    request: Request,
    session: AsyncSession = Depends(get_db_toko),
) -> UserToko:
    payload = decode_access_token(_token_from_request(request))
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesi tidak valid")
    user = await session.get(UserToko, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesi tidak valid")
    return user


def require_roles_toko(*roles: str) -> Callable:
    allowed = {r.strip().lower() for r in roles}

    async def _inner(user: UserToko = Depends(get_current_user_toko)) -> UserToko:
        if (user.role or "").strip().lower() not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akses ditolak")
        return user

    return _inner
