"""Authentication for the two store sub-tenants (admin and buyer).

Each side has its own cookie name, its own JWT audience/issuer
(``store_admin`` / ``store_buyer``) and its own account table, so a token
minted for one side is rejected by the other even though both share one
database. Only the generic JWT primitives from shared.security are reused.
"""
from __future__ import annotations

from typing import Callable

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import COOKIE_PATH, COOKIE_SAMESITE, COOKIE_SECURE, JWT_EXPIRE_HOURS
from shared.security import create_access_token, decode_access_token
from tenants.store.modules.store.infrastructure.database import get_db_store
from tenants.store.modules.store.infrastructure.models import AdminStore, PembeliStore

TENANT_ADMIN = "store_admin"
TENANT_BUYER = "store_buyer"
ADMIN_COOKIE_NAME = "store_admin_token"
BUYER_COOKIE_NAME = "store_buyer_token"


def issue_admin_token(user: AdminStore) -> str:
    return create_access_token(subject=user.id, role=user.role, session_version=0, tenant=TENANT_ADMIN)


def issue_buyer_token(user: PembeliStore) -> str:
    return create_access_token(subject=user.id, role="pembeli", session_version=0, tenant=TENANT_BUYER)


def _set_cookie(response: Response, name: str, token: str) -> None:
    response.set_cookie(
        key=name,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        max_age=JWT_EXPIRE_HOURS * 3600,
        path=COOKIE_PATH,
    )


def _clear_cookie(response: Response, name: str) -> None:
    response.delete_cookie(key=name, path=COOKIE_PATH, secure=COOKIE_SECURE, samesite=COOKIE_SAMESITE)


def set_admin_cookie(response: Response, token: str) -> None:
    _set_cookie(response, ADMIN_COOKIE_NAME, token)


def set_buyer_cookie(response: Response, token: str) -> None:
    _set_cookie(response, BUYER_COOKIE_NAME, token)


def clear_admin_cookie(response: Response) -> None:
    _clear_cookie(response, ADMIN_COOKIE_NAME)


def clear_buyer_cookie(response: Response) -> None:
    _clear_cookie(response, BUYER_COOKIE_NAME)


def _token_from_request(request: Request, cookie_name: str) -> str:
    cookie = request.cookies.get(cookie_name)
    if cookie:
        return cookie
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header.split(" ", 1)[1].strip()
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Tidak terautentikasi")


def _subject(token: str, tenant: str) -> str:
    payload = decode_access_token(token, expected_tenant=tenant)
    user_id = payload.get("sub")
    # Unlike the legacy tenants, a store token without aud/iss is never
    # accepted: both claims are always minted by issue_*_token above.
    if not user_id or payload.get("aud") != tenant:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesi tidak valid")
    return user_id


async def get_current_admin(
    request: Request,
    session: AsyncSession = Depends(get_db_store),
) -> AdminStore:
    user_id = _subject(_token_from_request(request, ADMIN_COOKIE_NAME), TENANT_ADMIN)
    user = await session.get(AdminStore, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesi tidak valid")
    return user


async def get_current_buyer(
    request: Request,
    session: AsyncSession = Depends(get_db_store),
) -> PembeliStore:
    user_id = _subject(_token_from_request(request, BUYER_COOKIE_NAME), TENANT_BUYER)
    user = await session.get(PembeliStore, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesi tidak valid")
    return user


def require_admin_roles(*roles: str) -> Callable:
    allowed = {r.strip().lower() for r in roles}

    async def _inner(user: AdminStore = Depends(get_current_admin)) -> AdminStore:
        if (user.role or "").strip().lower() not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akses ditolak")
        return user

    return _inner
