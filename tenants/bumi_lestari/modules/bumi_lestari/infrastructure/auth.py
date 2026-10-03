"""Auth dependencies isolated to the bumi_lestari tenant (own cookie, own JWT aud)."""
from __future__ import annotations

from typing import Callable

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import COOKIE_PATH, COOKIE_SAMESITE, COOKIE_SECURE, JWT_EXPIRE_HOURS, JWT_TENANT_BUMI_LESTARI
from shared.security import create_access_token, decode_access_token
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser

BUMI_LESTARI_COOKIE_NAME = "bumi_lestari_token"


def issue_bumi_lestari_token(user: BlUser) -> str:
    return create_access_token(subject=user.id, role=user.role, session_version=0, tenant=JWT_TENANT_BUMI_LESTARI)


def set_bumi_lestari_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=BUMI_LESTARI_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        max_age=JWT_EXPIRE_HOURS * 3600,
        path=COOKIE_PATH,
    )


def clear_bumi_lestari_cookie(response: Response) -> None:
    response.delete_cookie(
        key=BUMI_LESTARI_COOKIE_NAME, path=COOKIE_PATH, secure=COOKIE_SECURE, samesite=COOKIE_SAMESITE
    )


def _token_from_request(request: Request) -> str:
    cookie = request.cookies.get(BUMI_LESTARI_COOKIE_NAME)
    if cookie:
        return cookie
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header.split(" ", 1)[1].strip()
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Tidak terautentikasi")


async def get_current_user_bumi_lestari(
    request: Request, session: AsyncSession = Depends(get_db_bumi_lestari)
) -> BlUser:
    payload = decode_access_token(_token_from_request(request), expected_tenant=JWT_TENANT_BUMI_LESTARI)
    user_id = payload.get("sub")
    user = await session.get(BlUser, user_id) if user_id else None
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesi tidak valid")
    return user


def require_roles_bumi_lestari(*roles: str) -> Callable:
    allowed = {r.strip().lower() for r in roles}

    async def _inner(user: BlUser = Depends(get_current_user_bumi_lestari)) -> BlUser:
        if (user.role or "").strip().lower() not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akses ditolak")
        return user

    return _inner
