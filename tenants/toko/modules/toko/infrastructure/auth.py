"""Authentication dependencies isolated to the toko module.

Mirrors tenants/madrasah/modules/madrasah/infrastructure/auth.py: reuses
only the generic JWT encode/decode primitives from shared.security, never
the BUMDes/madrasah cookie names, so a toko login/logout never touches
another tenant's session cookie in the same browser.
"""
from __future__ import annotations

from typing import Callable

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import COOKIE_PATH, COOKIE_SAMESITE, COOKIE_SECURE, JWT_EXPIRE_HOURS
from shared.security import create_access_token, decode_access_token
from tenants.toko.modules.toko.infrastructure.database import get_db_toko
from tenants.toko.modules.toko.infrastructure.models import UserToko

TOKO_COOKIE_NAME = "toko_token"

# Legacy full-access roles -- unchanged, unrestricted across BOTH the toko-web
# and marketplace-ERP capabilities, see docstrings on UserToko.role.
FULL_ACCESS_ROLES_TOKO = ("owner", "admin_toko")
# New, narrower staff roles (additive -- see PR description). admin_toko_web
# only gets toko-web admin access; admin_marketplace only gets marketplace
# ERP admin access, further restricted per-akun (see akun_ids_diizinkan).
ROLE_ADMIN_TOKO_WEB = "admin_toko_web"
ROLE_ADMIN_MARKETPLACE = "admin_marketplace"
STAFF_ROLES_TOKO = (ROLE_ADMIN_TOKO_WEB, ROLE_ADMIN_MARKETPLACE)


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


async def akun_ids_diizinkan(user: UserToko, session: AsyncSession) -> list[str] | None:
    """Returns None for unrestricted roles (owner, admin_toko -- and any
    other role besides admin_marketplace, though only admin_marketplace is
    ever meant to reach the marketplace endpoints that call this) meaning
    "no akun_id filtering needed, see everything". Returns a concrete
    (possibly empty) list of AkunMarketplace ids for admin_marketplace
    staff -- the ONLY AkunMarketplace rows (tenants/toko/modules/erp/
    infrastructure/models.py) this user is allowed to read/write, per
    toko_staff_akun (StaffAkunMarketplace). Imports the erp model lazily to
    avoid a module-load-time cross-module import cycle (toko <-> erp), same
    pattern already used in seeder.py."""
    if (user.role or "").strip().lower() != ROLE_ADMIN_MARKETPLACE:
        return None
    from tenants.toko.modules.erp.infrastructure.models import StaffAkunMarketplace

    rows = (
        await session.execute(select(StaffAkunMarketplace.akun_id).where(StaffAkunMarketplace.user_id == user.id))
    ).scalars().all()
    return list(rows)


async def pastikan_akses_akun(user: UserToko, session: AsyncSession, akun_id: str | None) -> None:
    """Raise 403 if `user` (when restricted, i.e. admin_marketplace) is not
    allowed to touch `akun_id`. A no-op for unrestricted roles. Used on
    single-object GET/PATCH/DELETE/send-message marketplace endpoints where
    the akun_id comes from the fetched row, not a query/body param."""
    allowed = await akun_ids_diizinkan(user, session)
    if allowed is None:
        return
    if akun_id is None or akun_id not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akses ditolak untuk akun marketplace ini")
