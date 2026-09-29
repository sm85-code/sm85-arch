"""HTTP surface for marketplace_erp -- Tahap 1 + Tahap 2.

Tahap 1: auth, akun, produk (SKU induk), listing.
Tahap 2: seed gate, stock reservation/ledger, OMS pesanan inbox,
         Shopee OAuth start/callback (+ stubs for other platforms).

Mounted in main.py as prefix=/api/marketplace-erp.
"""
from __future__ import annotations

import os
import secrets as pysecrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import (
    AkunMarketplaceIn,
    AkunMarketplaceOut,
    AkunMarketplacePatch,
    GudangOut,
    LoginIn,
    OAuthStartOut,
    PesananIn,
    PesananOut,
    PesananStatusIn,
    ProdukIn,
    ProdukListingIn,
    ProdukListingOut,
    ProdukListingPatch,
    ProdukOut,
    ProdukPatch,
    RegisterIn,
    StokAdjustIn,
    StokLedgerOut,
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


def _is_production_env() -> bool:
    return os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "")).strip().lower() in {"production", "prod"}


def _marketplace_erp_seed_secret_ok(request: Request) -> bool:
    expected = (os.getenv("MARKETPLACE_ERP_SEED_SECRET") or "").strip()
    if not expected:
        return False
    provided = (
        request.headers.get("X-Marketplace-Erp-Seed-Secret")
        or request.headers.get("X-Seed-Secret")
        or ""
    ).strip()
    if not provided:
        return False
    return pysecrets.compare_digest(provided, expected)


async def authorize_marketplace_erp_seed(
    request: Request,
    session: AsyncSession = Depends(get_db_marketplace_erp),
) -> UserMarketplaceErp | None:
    """Gate /seed-now like madrasah: secret header OR authenticated owner.

    In production, anonymous callers without a matching
    MARKETPLACE_ERP_SEED_SECRET are rejected (403).
    """
    if _marketplace_erp_seed_secret_ok(request):
        return None
    try:
        user = await get_current_user_marketplace_erp(request, session)
    except HTTPException:
        if _is_production_env():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "seed-now dinonaktifkan untuk publik di production. "
                    "Login sebagai owner, atau set header X-Marketplace-Erp-Seed-Secret "
                    "yang cocok dengan env MARKETPLACE_ERP_SEED_SECRET."
                ),
            )
        raise
    if (user.role or "").strip().lower() != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akses ditolak")
    return user


@marketplace_erp_router.get("/seed-now")
async def seed_now(
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp | None = Depends(authorize_marketplace_erp_seed),
):
    try:
        return await seed_marketplace_erp(session)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


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


# --- Tahap 2: Stock ----------------------------------------------------------


@marketplace_erp_router.get("/gudang", response_model=list[GudangOut])
async def list_gudang(
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.list_gudang(session)


@marketplace_erp_router.get("/stok/ledger", response_model=list[StokLedgerOut])
async def list_stok_ledger(
    produk_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.list_stok_ledger(session, produk_id=produk_id, limit=limit)


@marketplace_erp_router.post("/stok/adjust", response_model=ProdukOut)
async def adjust_stok(
    payload: StokAdjustIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.adjust_stok(session, payload)


# --- Tahap 2: Orders OMS -----------------------------------------------------


@marketplace_erp_router.get("/pesanan", response_model=list[PesananOut])
async def list_pesanan(
    platform: str | None = None,
    akun_id: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.list_pesanan(
        session, platform=platform, akun_id=akun_id, status_filter=status_filter
    )


@marketplace_erp_router.post("/pesanan", response_model=PesananOut)
async def create_pesanan(
    payload: PesananIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.create_pesanan(session, payload)


@marketplace_erp_router.get("/pesanan/{pesanan_id}", response_model=PesananOut)
async def get_pesanan(
    pesanan_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.get_pesanan(session, pesanan_id)


@marketplace_erp_router.post("/pesanan/{pesanan_id}/status", response_model=PesananOut)
async def ubah_status_pesanan(
    pesanan_id: str,
    payload: PesananStatusIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.ubah_status_pesanan(session, pesanan_id, payload.status)


@marketplace_erp_router.delete("/pesanan/{pesanan_id}")
async def delete_pesanan(
    pesanan_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    await services.delete_pesanan(session, pesanan_id)
    return {"ok": True}


# --- Tahap 2: OAuth (Shopee first) -------------------------------------------


@marketplace_erp_router.get("/oauth/shopee/start", response_model=OAuthStartOut)
async def oauth_shopee_start(
    akun_id: str = Query(...),
    redirect_uri: str | None = None,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    """Return the Shopee authorize URL. FE should redirect the browser there.

    ``redirect_uri`` should land on /oauth/shopee/callback/{akun_id} (or an
    equivalent FE proxy). Partner credentials come from env only.
    """
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    akun = await services.get_akun_marketplace(session, akun_id)
    if akun.platform != "shopee":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Akun bukan platform shopee")
    # Prefer caller-supplied redirect; else env; append akun_id path if using API callback.
    base_redirect = (redirect_uri or os.getenv("SHOPEE_REDIRECT_URI") or "").strip()
    if not base_redirect:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SHOPEE_REDIRECT_URI belum diisi",
        )
    # If redirect points at our callback root, ensure akun_id is in the path.
    if base_redirect.rstrip("/").endswith("/oauth/shopee/callback"):
        base_redirect = f"{base_redirect.rstrip('/')}/{akun_id}"
    try:
        url = erp_shopee.build_authorize_url(redirect_uri=base_redirect)
    except HTTPException:
        raise
    return OAuthStartOut(platform="shopee", akun_id=akun_id, authorize_url=url)


@marketplace_erp_router.get("/oauth/shopee/callback/{akun_id}")
async def oauth_shopee_callback(
    akun_id: str,
    code: str = Query(...),
    shop_id: str = Query(...),
    session: AsyncSession = Depends(get_db_marketplace_erp),
):
    """Exchange OAuth code for tokens and persist on AkunMarketplace.

    Public callback (Shopee redirects here). akun_id in the path binds the
    shop to the pending local row created before /oauth/shopee/start.
    """
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    akun = await services.get_akun_marketplace(session, akun_id)
    if akun.platform != "shopee":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Akun bukan platform shopee")
    # Reject binding a shop_id already owned by another row.
    await services._cek_duplikat_id_toko_eksternal(
        session, platform="shopee", id_toko_eksternal=str(shop_id), exclude_id=akun.id
    )
    payload = await erp_shopee.exchange_token(code=code, shop_id=str(shop_id))
    erp_shopee.apply_token_payload(akun, payload, shop_id=str(shop_id))
    await session.flush()
    return {
        "ok": True,
        "akun_id": akun.id,
        "status": akun.status,
        "id_toko_eksternal": akun.id_toko_eksternal,
        "token_kedaluwarsa": akun.token_kedaluwarsa.isoformat() if akun.token_kedaluwarsa else None,
    }


@marketplace_erp_router.post("/akun/{akun_id}/sync/pesanan")
async def sync_pesanan_akun(
    akun_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    """Trigger platform order pull. Live Shopee sync gated by SHOPEE_LIVE_SYNC."""
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    akun = await services.get_akun_marketplace(session, akun_id)
    if akun.platform == "shopee":
        try:
            rows = await erp_shopee.sync_pesanan(akun)
        except NotImplementedError as exc:
            raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)) from exc
        return {"ok": True, "pulled": len(rows)}
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=f"Sync pesanan untuk platform '{akun.platform}' belum tersedia (Shopee first)",
    )


@marketplace_erp_router.post("/akun/{akun_id}/sync/produk")
async def sync_produk_akun(
    akun_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    akun = await services.get_akun_marketplace(session, akun_id)
    if akun.platform == "shopee":
        try:
            rows = await erp_shopee.sync_produk(akun)
        except NotImplementedError as exc:
            raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)) from exc
        return {"ok": True, "pulled": len(rows)}
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=f"Sync produk untuk platform '{akun.platform}' belum tersedia (Shopee first)",
    )


@marketplace_erp_router.get("/oauth/lazada/start")
@marketplace_erp_router.get("/oauth/tiktokshop/start")
@marketplace_erp_router.get("/oauth/blibli/start")
async def oauth_other_placeholder(
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="OAuth platform ini masih placeholder -- Shopee first (Tahap 2)",
    )
