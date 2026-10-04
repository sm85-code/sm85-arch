"""Authentication dependencies isolated to the marketplace_erp tenant.

Mirrors tenants/toko/modules/toko/infrastructure/auth.py: reuses only the
generic JWT encode/decode + cookie primitives from shared.security, with
its own cookie name so a login here never collides with siabumdes/
madrasah/toko sessions in the same browser.

Role model: "admin" (everything, including other users' usernames and roles) >
"owner" (everything else) > "staff" (scoped to the shops assigned to it).
"""
from __future__ import annotations

from typing import Callable

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import COOKIE_PATH, COOKIE_SAMESITE, COOKIE_SECURE, JWT_EXPIRE_HOURS
from shared.security import create_access_token, decode_access_token
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import UserMarketplaceErp

MARKETPLACE_ERP_COOKIE_NAME = "marketplace_erp_token"

FULL_ACCESS_ROLES = ("admin", "owner")


def issue_marketplace_erp_token(user: UserMarketplaceErp) -> str:
    return create_access_token(
        subject=user.id, role=user.role, session_version=0, tenant="marketplace_erp"
    )


def set_marketplace_erp_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=MARKETPLACE_ERP_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        max_age=JWT_EXPIRE_HOURS * 3600,
        path=COOKIE_PATH,
    )


def clear_marketplace_erp_cookie(response: Response) -> None:
    response.delete_cookie(
        key=MARKETPLACE_ERP_COOKIE_NAME,
        path=COOKIE_PATH,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
    )


def _token_from_request(request: Request) -> str:
    cookie = request.cookies.get(MARKETPLACE_ERP_COOKIE_NAME)
    if cookie:
        return cookie
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header.split(" ", 1)[1].strip()
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Tidak terautentikasi")


async def get_current_user_marketplace_erp(
    request: Request,
    session: AsyncSession = Depends(get_db_marketplace_erp),
) -> UserMarketplaceErp:
    payload = decode_access_token(
        _token_from_request(request), expected_tenant="marketplace_erp"
    )
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesi tidak valid")
    user = await session.get(UserMarketplaceErp, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesi tidak valid")
    return user


def require_roles_marketplace_erp(*roles: str) -> Callable:
    allowed = {r.strip().lower() for r in roles}

    async def _inner(user: UserMarketplaceErp = Depends(get_current_user_marketplace_erp)) -> UserMarketplaceErp:
        if (user.role or "").strip().lower() not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akses ditolak")
        return user

    return _inner


async def akun_ids_diizinkan(user: UserMarketplaceErp, session: AsyncSession) -> list[str] | None:
    """None for `admin`/`owner` (unrestricted, see everything). A concrete (possibly
    empty) list of AkunMarketplace ids for `staff` -- the only shops that
    role may read/write, per StaffAkunMarketplace. Imported lazily to avoid
    a module-load-time cycle between auth.py and models.py's own imports."""
    if (user.role or "").strip().lower() != "staff":
        return None
    from sqlalchemy import select

    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import StaffAkunMarketplace

    rows = (
        await session.execute(select(StaffAkunMarketplace.akun_id).where(StaffAkunMarketplace.user_id == user.id))
    ).scalars().all()
    return list(rows)


async def pastikan_akses_akun(user: UserMarketplaceErp, session: AsyncSession, akun_id: str | None) -> None:
    """Raise 403 if `user` (when restricted, i.e. staff) is not allowed to
    touch `akun_id`. No-op for owner. Used on single-object endpoints where
    akun_id comes from the fetched row, not a query/body param the caller
    could spoof."""
    allowed = await akun_ids_diizinkan(user, session)
    if allowed is None:
        return
    if akun_id is None or akun_id not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akses ditolak untuk akun marketplace ini")
