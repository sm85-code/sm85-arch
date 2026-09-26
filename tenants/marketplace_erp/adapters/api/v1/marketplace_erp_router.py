"""HTTP surface for the marketplace_erp tenant -- Tahap 1: auth, akun
marketplace (shop connections) and produk (SKU induk) + listing mapping.

Mounted in main.py as prefix=/api/marketplace-erp. Stok, pesanan,
pengiriman, chat, keuangan, iklan and laporan are separate follow-up
modules layered on top of the Produk/AkunMarketplace foundation here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import (
    AkunMarketplaceIn,
    AkunMarketplaceOut,
    AkunMarketplacePatch,
    LoginIn,
    ProdukIn,
    ProdukListingIn,
    ProdukListingOut,
    ProdukListingPatch,
    ProdukOut,
    ProdukPatch,
    RegisterIn,
    UserOut,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import (
    clear_marketplace_erp_cookie,
    get_current_user_marketplace_erp,
    issue_marketplace_erp_token,
    require_roles_marketplace_erp,
    set_marketplace_erp_cookie,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import UserMarketplaceErp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.seeder import seed_marketplace_erp

marketplace_erp_router = APIRouter()

OWNER_ONLY = ("owner",)


@marketplace_erp_router.get("/seed-now")
async def seed_now(session: AsyncSession = Depends(get_db_marketplace_erp)):
    return await seed_marketplace_erp(session)


# --- Auth ----------------------------------------------------------------


@marketplace_erp_router.post("/auth/register", response_model=UserOut)
async def register(payload: RegisterIn, session: AsyncSession = Depends(get_db_marketplace_erp)):
    user = await services.register_user(session, payload)
    return user


@marketplace_erp_router.post("/auth/login", response_model=UserOut)
async def login(payload: LoginIn, response: Response, session: AsyncSession = Depends(get_db_marketplace_erp)):
    user = await services.authenticate_user(session, payload)
    token = issue_marketplace_erp_token(user)
    set_marketplace_erp_cookie(response, token)
    return user


@marketplace_erp_router.post("/auth/logout")
async def logout(response: Response):
    clear_marketplace_erp_cookie(response)
    return {"ok": True}


@marketplace_erp_router.get("/auth/me", response_model=UserOut)
async def me(user: UserMarketplaceErp = Depends(get_current_user_marketplace_erp)):
    return user


# --- Akun Marketplace ------------------------------------------------------


@marketplace_erp_router.get("/akun", response_model=list[AkunMarketplaceOut])
async def list_akun(
    platform: str | None = None,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.list_akun_marketplace(session, platform=platform)


@marketplace_erp_router.post("/akun", response_model=AkunMarketplaceOut)
async def create_akun(
    payload: AkunMarketplaceIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.create_akun_marketplace(session, payload)


@marketplace_erp_router.get("/akun/{akun_id}", response_model=AkunMarketplaceOut)
async def get_akun(
    akun_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.get_akun_marketplace(session, akun_id)


@marketplace_erp_router.patch("/akun/{akun_id}", response_model=AkunMarketplaceOut)
async def update_akun(
    akun_id: str,
    payload: AkunMarketplacePatch,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.update_akun_marketplace(session, akun_id, payload)


@marketplace_erp_router.delete("/akun/{akun_id}")
async def delete_akun(
    akun_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    await services.delete_akun_marketplace(session, akun_id)
    return {"ok": True}


# --- Produk (SKU induk) -----------------------------------------------------


@marketplace_erp_router.get("/produk", response_model=list[ProdukOut])
async def list_produk(
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.list_produk(session)


@marketplace_erp_router.post("/produk", response_model=ProdukOut)
async def create_produk(
    payload: ProdukIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.create_produk(session, payload)


@marketplace_erp_router.get("/produk/{produk_id}", response_model=ProdukOut)
async def get_produk(
    produk_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.get_produk(session, produk_id)


@marketplace_erp_router.patch("/produk/{produk_id}", response_model=ProdukOut)
async def update_produk(
    produk_id: str,
    payload: ProdukPatch,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.update_produk(session, produk_id, payload)


@marketplace_erp_router.delete("/produk/{produk_id}")
async def delete_produk(
    produk_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    await services.delete_produk(session, produk_id)
    return {"ok": True}


# --- Produk Listing ----------------------------------------------------------


@marketplace_erp_router.get("/listing", response_model=list[ProdukListingOut])
async def list_listing(
    produk_id: str | None = None,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.list_listing(session, produk_id=produk_id)


@marketplace_erp_router.post("/listing", response_model=ProdukListingOut)
async def create_listing(
    payload: ProdukListingIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.create_listing(session, payload)


@marketplace_erp_router.patch("/listing/{listing_id}", response_model=ProdukListingOut)
async def update_listing(
    listing_id: str,
    payload: ProdukListingPatch,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.update_listing(session, listing_id, payload)


@marketplace_erp_router.delete("/listing/{listing_id}")
async def delete_listing(
    listing_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    await services.delete_listing(session, listing_id)
    return {"ok": True}
