"""HTTP surface for marketplace_erp -- Tahap 1 + 2 + 3.

Tahap 1: auth, akun, produk (SKU induk), listing.
Tahap 2: seed gate, stock reservation/ledger, OMS pesanan inbox,
         Shopee OAuth start/callback (+ stubs for other platforms).
Tahap 3: multi-gudang + transfer, staff-akun scoping, pengiriman (manual
         courier/AWB), settlement (manual payout reconciliation), laporan
         ringkas. All local-data features -- none of this needs a live
         marketplace API connection, see IDEAL_FOLLOWUPS.md.

Mounted in main.py as prefix=/api/marketplace-erp.
"""
from __future__ import annotations

import os
import secrets as pysecrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import (
    AkunMarketplaceIn,
    AkunMarketplaceOut,
    AkunMarketplacePatch,
    ChangePasswordIn,
    GudangIn,
    GudangOut,
    IklanCampaignIn,
    IklanCampaignOut,
    IklanCampaignPatch,
    IklanLaporanOut,
    IklanMetrikHarianIn,
    IklanMetrikHarianOut,
    LaporanRingkasOut,
    LoginIn,
    OAuthStartOut,
    PengirimanIn,
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
    SettlementIn,
    SettlementOut,
    SettlementPatch,
    StaffAkunIn,
    StaffAkunOut,
    StokAdjustIn,
    StokLedgerOut,
    StokTransferIn,
    UserCreateIn,
    UserOut,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import (
    akun_ids_diizinkan,
    clear_marketplace_erp_cookie,
    get_current_user_marketplace_erp,
    issue_marketplace_erp_token,
    pastikan_akses_akun,
    require_roles_marketplace_erp,
    set_marketplace_erp_cookie,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import UserMarketplaceErp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.seeder import seed_marketplace_erp

marketplace_erp_router = APIRouter()

OWNER_ONLY = ("owner",)
# Endpoints a scoped `staff` account may reach at all -- per-akun filtering
# still applies inside the handler via akun_ids_diizinkan/pastikan_akses_akun.
# Owner is always unrestricted.
OWNER_OR_STAFF = ("owner", "staff")


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


def _public_register_enabled() -> bool:
    """Public self-registration is OFF unless the operator opts in.

    It used to be open to anyone and always created role=owner, i.e. a full
    admin takeover of the tenant. Accounts are now created by an owner via
    POST /users; the very first owner comes from /seed-now (seed secret).
    """
    return (os.getenv("MARKETPLACE_ERP_ALLOW_REGISTER") or "").strip().lower() in {"1", "true", "yes", "on"}


@marketplace_erp_router.post("/auth/register", response_model=UserOut)
async def register(payload: RegisterIn, session: AsyncSession = Depends(get_db_marketplace_erp)):
    if not _public_register_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Registrasi publik dinonaktifkan. Minta owner membuatkan akun "
                "(POST /api/marketplace-erp/users)."
            ),
        )
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


@marketplace_erp_router.post("/auth/change-password", response_model=UserOut)
async def change_password(
    payload: ChangePasswordIn,
    response: Response,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(get_current_user_marketplace_erp),
):
    """Change the logged-in user's own password (any role).

    Requires the current password; the new one must be >= 8 chars and differ
    from the current one. Clears must_change_password and re-issues the
    session cookie.
    """
    user = await services.change_password(session, user, payload)
    set_marketplace_erp_cookie(response, issue_marketplace_erp_token(user))
    return user


# --- Users (owner-only account management) ----------------------------------


@marketplace_erp_router.get("/users", response_model=list[UserOut])
async def list_users(
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.list_users(session)


@marketplace_erp_router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreateIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    """Owner creates a staff/owner account with a temporary password; the new
    account gets must_change_password=true."""
    return await services.create_user(session, payload)


# --- Akun Marketplace ------------------------------------------------------


@marketplace_erp_router.get("/akun", response_model=list[AkunMarketplaceOut])
async def list_akun(
    platform: str | None = None,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_OR_STAFF)),
):
    rows = await services.list_akun_marketplace(session, platform=platform)
    allowed = await akun_ids_diizinkan(user, session)
    if allowed is None:
        return rows
    return [r for r in rows if r.id in allowed]


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
    user: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_OR_STAFF)),
):
    akun = await services.get_akun_marketplace(session, akun_id)
    await pastikan_akses_akun(user, session, akun.id)
    return akun


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


@marketplace_erp_router.post("/gudang", response_model=GudangOut, status_code=status.HTTP_201_CREATED)
async def create_gudang(
    payload: GudangIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.create_gudang(session, payload)


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


@marketplace_erp_router.post("/stok/transfer", response_model=ProdukOut)
async def transfer_stok(
    payload: StokTransferIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.transfer_stok(session, payload)


# --- Tahap 3: Staff-akun scoping ----------------------------------------------


@marketplace_erp_router.get("/staff-akun", response_model=list[StaffAkunOut])
async def list_staff_akun(
    user_id: str | None = None,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.list_staff_akun(session, user_id=user_id)


@marketplace_erp_router.post("/staff-akun", response_model=StaffAkunOut, status_code=status.HTTP_201_CREATED)
async def assign_staff_akun(
    payload: StaffAkunIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.assign_staff_akun(session, payload)


@marketplace_erp_router.delete("/staff-akun/{staff_akun_id}")
async def remove_staff_akun(
    staff_akun_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    await services.remove_staff_akun(session, staff_akun_id)
    return {"ok": True}


# --- Tahap 2: Orders OMS -----------------------------------------------------


@marketplace_erp_router.get("/pesanan", response_model=list[PesananOut])
async def list_pesanan(
    platform: str | None = None,
    akun_id: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_OR_STAFF)),
):
    if akun_id:
        await pastikan_akses_akun(user, session, akun_id)
    rows = await services.list_pesanan(session, platform=platform, akun_id=akun_id, status_filter=status_filter)
    allowed = await akun_ids_diizinkan(user, session)
    if allowed is None:
        return rows
    return [r for r in rows if r.akun_id in allowed]


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
    user: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_OR_STAFF)),
):
    pesanan = await services.get_pesanan(session, pesanan_id)
    await pastikan_akses_akun(user, session, pesanan.akun_id)
    return pesanan


@marketplace_erp_router.post("/pesanan/{pesanan_id}/status", response_model=PesananOut)
async def ubah_status_pesanan(
    pesanan_id: str,
    payload: PesananStatusIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_OR_STAFF)),
):
    pesanan = await services.get_pesanan(session, pesanan_id)
    await pastikan_akses_akun(user, session, pesanan.akun_id)
    return await services.ubah_status_pesanan(session, pesanan_id, payload.status)


@marketplace_erp_router.post("/pesanan/{pesanan_id}/pengiriman", response_model=PesananOut)
async def set_pengiriman(
    pesanan_id: str,
    payload: PengirimanIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_OR_STAFF)),
):
    pesanan = await services.get_pesanan(session, pesanan_id)
    await pastikan_akses_akun(user, session, pesanan.akun_id)
    return await services.set_pengiriman(session, pesanan_id, payload)


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


# --- Tahap 3: Settlement -------------------------------------------------------


@marketplace_erp_router.get("/settlement", response_model=list[SettlementOut])
async def list_settlement(
    akun_id: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.list_settlement(session, akun_id=akun_id, status_filter=status_filter)


@marketplace_erp_router.post("/settlement", response_model=SettlementOut, status_code=status.HTTP_201_CREATED)
async def create_settlement(
    payload: SettlementIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.create_settlement(session, payload)


@marketplace_erp_router.get("/settlement/{settlement_id}", response_model=SettlementOut)
async def get_settlement(
    settlement_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.get_settlement(session, settlement_id)


@marketplace_erp_router.patch("/settlement/{settlement_id}", response_model=SettlementOut)
async def update_settlement(
    settlement_id: str,
    payload: SettlementPatch,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.update_settlement(session, settlement_id, payload)


# --- Tahap 3: Laporan ringkas --------------------------------------------------


@marketplace_erp_router.get("/laporan/ringkas", response_model=LaporanRingkasOut)
async def laporan_ringkas(
    dari: datetime = Query(...),
    sampai: datetime = Query(...),
    batas_stok_kritis: int = Query(5, ge=0, le=100000),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    if sampai < dari:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="sampai sebelum dari")
    return await services.laporan_ringkas(session, dari=dari, sampai=sampai, batas_stok_kritis=batas_stok_kritis)


# --- Tahap 4: Iklan (ads) -------------------------------------------------------


@marketplace_erp_router.get("/iklan", response_model=list[IklanCampaignOut])
async def list_campaign(
    akun_id: str | None = None,
    platform: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.list_campaign(session, akun_id=akun_id, platform=platform, status_filter=status_filter)


@marketplace_erp_router.post("/iklan", response_model=IklanCampaignOut, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    payload: IklanCampaignIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.create_campaign(session, payload)


@marketplace_erp_router.get("/iklan/{campaign_id}", response_model=IklanCampaignOut)
async def get_campaign(
    campaign_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.get_campaign(session, campaign_id)


@marketplace_erp_router.patch("/iklan/{campaign_id}", response_model=IklanCampaignOut)
async def update_campaign(
    campaign_id: str,
    payload: IklanCampaignPatch,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.update_campaign(session, campaign_id, payload)


@marketplace_erp_router.delete("/iklan/{campaign_id}")
async def delete_campaign(
    campaign_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    await services.delete_campaign(session, campaign_id)
    return {"ok": True}


@marketplace_erp_router.post("/iklan/{campaign_id}/metrik", response_model=IklanMetrikHarianOut)
async def record_metrik_harian(
    campaign_id: str,
    payload: IklanMetrikHarianIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.record_metrik_harian(session, campaign_id, payload)


@marketplace_erp_router.get("/iklan/{campaign_id}/metrik", response_model=list[IklanMetrikHarianOut])
async def list_metrik_harian(
    campaign_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    return await services.list_metrik_harian(session, campaign_id)


@marketplace_erp_router.get("/iklan/{campaign_id}/laporan", response_model=IklanLaporanOut)
async def laporan_iklan(
    campaign_id: str,
    dari: datetime = Query(...),
    sampai: datetime = Query(...),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    if sampai < dari:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="sampai sebelum dari")
    return await services.laporan_iklan(session, campaign_id, dari=dari, sampai=sampai)
