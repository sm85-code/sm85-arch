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

import logging
import os
import secrets as pysecrets
from types import SimpleNamespace
from datetime import date, datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import (
    KatalogKirimIn,
    BatalkanPesananIn,
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
    ProsesMassalIn,
    ResiGabunganIn,
    ResiMassalIn,
    TandaiResiIn,
    ProdukIn,
    ProdukListingIn,
    ProdukListingOut,
    ProdukListingPatch,
    ProdukOut,
    ProdukPatch,
    PublishTokoIn,
    RegisterIn,
    SettlementIn,
    SettlementOut,
    SettlementPatch,
    StaffAkunIn,
    StaffAkunOut,
    StokAdjustIn,
    StokLedgerOut,
    StokTransferIn,
    ProfilUpdateIn,
    UserCreateIn,
    UserUpdateIn,
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
from tenants.store.modules.store.application import services as store_services
from tenants.store.modules.store.infrastructure import database as store_database
from tenants.store.modules.store.infrastructure.media_import import import_foto_dari_url

logger = logging.getLogger(__name__)

marketplace_erp_router = APIRouter()

# Roles by level: ``OWNER_ONLY`` means owner level and above (admin inherits everything an owner can do);
# ``ADMIN_ONLY`` is for what only an admin may do (other users' usernames and roles, and everything under Iklan).
ADMIN_ONLY = ("admin",)
OWNER_ONLY = ("admin", "owner")
# Endpoints a scoped `staff` account may reach at all -- per-akun filtering
# still applies inside the handler via akun_ids_diizinkan/pastikan_akses_akun.
# Owner is always unrestricted.
OWNER_OR_STAFF = ("admin", "owner", "staff")


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
    if (user.role or "").strip().lower() not in OWNER_ONLY:
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
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)) from exc


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


@marketplace_erp_router.patch("/auth/profil", response_model=UserOut)
async def update_profil(
    payload: ProfilUpdateIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(get_current_user_marketplace_erp),
):
    """Any account edits its own display name. The username (email) and role cannot be changed here."""
    return await services.update_profil(session, user, payload)


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
    """Create an account with a temporary password (must_change_password=true). An admin may create any role;
    an owner only staff."""
    services.pastikan_boleh_membuat_peran(_.role, payload.role)
    return await services.create_user(session, payload)


@marketplace_erp_router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: str,
    payload: UserUpdateIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    """Admin only: change another account's username (login email), name or role."""
    return await services.update_user(session, user_id, payload)


@marketplace_erp_router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    admin: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    """Admin only: delete another account (and its shop assignments). Not yourself, not the last admin."""
    await services.hapus_user(session, user_id, admin)


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
    bersama_pesanan: bool = False,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    """``bersama_pesanan=true`` also deletes the shop's orders (for clearing test data)."""
    return {"ok": True, **await services.delete_akun_marketplace(session, akun_id, hapus_pesanan=bersama_pesanan)}


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


async def _store_db():
    """Session on the store database, for the ERP -> store publish. 501 when
    the store database is not configured in this deployment."""
    if store_database.SessionLocal is None:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Database toko belum dikonfigurasi")
    async with store_database.SessionLocal() as store_session:
        try:
            yield store_session
            await store_session.commit()
        except Exception:
            await store_session.rollback()
            raise


@marketplace_erp_router.post("/produk/{produk_id}/publish-toko")
async def publish_produk_ke_toko(
    produk_id: str,
    payload: PublishTokoIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    store_session: AsyncSession = Depends(_store_db),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    """Copy (or refresh) one ERP product in the online store. Idempotent per
    ERP product: republishing updates name/description/price but keeps the
    store's own stock, and a photo only changes when a new one was copied."""
    produk = await services.get_produk(session, produk_id)
    listings = await services.list_listing(session, produk_id=produk.id)
    platform_asal = next((li.platform for li in listings if li.aktif), None) or (
        listings[0].platform if listings else None
    )
    foto_key = await import_foto_dari_url(produk.foto_url) if payload.salin_foto else None
    toko_produk, dibuat = await store_services.upsert_produk_dari_erp(
        store_session,
        erp_produk_id=produk.id,
        nama=produk.nama,
        deskripsi=produk.deskripsi,
        harga=payload.harga if payload.harga is not None else produk.harga_dasar,
        stok=payload.stok if payload.stok is not None else produk.stok,
        platform_asal=platform_asal,
        foto_key=foto_key,
        aktif=payload.aktif,
        berat_gram=produk.berat_gram,
        panjang_cm=produk.panjang_cm,
        lebar_cm=produk.lebar_cm,
        tinggi_cm=produk.tinggi_cm,
        preorder=produk.preorder,
        hari_proses=produk.hari_proses,
    )
    return {
        "dibuat": dibuat,
        "foto_disalin": foto_key is not None,
        "produk": store_services.produk_out(toko_produk),
    }


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


@marketplace_erp_router.get("/pesanan/daftar")
async def daftar_pesanan(
    akun_id: str | None = None,
    tahap: str | None = Query(None, description="belum_bayar | perlu_diproses | menunggu_kurir | dikirim | selesai | dibatalkan"),
    resi: str | None = Query(None, description="belum | sudah (resi dicetak), hanya pesanan yang resinya bisa dicetak"),
    q: str | None = Query(None, max_length=100, description="nomor pesanan, pembeli, nomor resi atau nama produk"),
    dari: datetime | None = None,
    sampai: datetime | None = None,
    urut: str = Query("tanggal:desc", description="<kolom>:asc|desc, kolom: tanggal nomor toko status total kurir"),
    halaman: int = Query(1, ge=1),
    per_halaman: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_OR_STAFF)),
):
    """Orders for the Pesanan page: filtered, sorted and paged on the server (response: total + one page)."""
    if akun_id:
        await pastikan_akses_akun(user, session, akun_id)
    hasil = await services.daftar_pesanan(
        session,
        akun_id=akun_id, akun_diizinkan=await akun_ids_diizinkan(user, session), tahap=tahap, resi=resi, q=q,
        dari=dari, sampai=sampai, urut=urut, halaman=halaman, per_halaman=per_halaman,
    )
    import json
    await services.lengkapi_foto_item(session, hasil["items"])
    baris = []
    for p in hasil["items"]:
        data = PesananOut.model_validate(p).model_dump()
        data.update({k: v for k, v in json.loads(getattr(p, "detail_json", None) or "{}").items() if v not in (None, "")})
        baris.append(data)
    for data in baris:
        for item in data.get("items") or []:
            if not item.get("model_name") and " - " in (item.get("nama_produk") or ""):
                nama, model = item["nama_produk"].rsplit(" - ", 1)
                item["nama_produk"] = nama
                item["model_name"] = model
    hasil["items"] = baris
    return hasil


@marketplace_erp_router.get("/pesanan/ringkasan")
async def ringkasan_pesanan(
    akun_id: str | None = None,
    tahap: str | None = None,
    q: str | None = Query(None, max_length=100),
    dari: datetime | None = None,
    sampai: datetime | None = None,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_OR_STAFF)),
):
    """Counts behind the status and shop filter chips (each row ignores its own filter)."""
    if akun_id:
        await pastikan_akses_akun(user, session, akun_id)
    return await services.ringkasan_pesanan(
        session, akun_id=akun_id, tahap=tahap, akun_diizinkan=await akun_ids_diizinkan(user, session), q=q, dari=dari, sampai=sampai
    )


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
    await services.lengkapi_foto_item(session, [pesanan])
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


@marketplace_erp_router.post("/pesanan/sinkron")
async def sinkron_pesanan_otomatis(
    paksa: bool = Query(False, description="true = refresh now (still at most once per few seconds per shop)"),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_OR_STAFF)),
):
    """Pull new/changed orders for every connected Shopee shop the user may see.

    Meant to be called whenever the order page is opened or refreshed. Each shop is throttled
    (default once a minute), so calling it often is cheap; it returns right away with
    ``aktif: false`` when live sync is switched off.
    """
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    if not erp_shopee.live_sync_enabled():
        return {"aktif": False, "jumlah_baru": 0, "jumlah_diperbarui": 0, "toko": []}
    diizinkan = await akun_ids_diizinkan(user, session)
    akun_list = [
        a
        for a in await services.list_akun_marketplace(session, platform="shopee")
        if a.access_token and a.id_toko_eksternal and (diizinkan is None or a.id in diizinkan)
    ]
    toko = await services.sinkron_semua_pesanan(session, akun_list, jeda_detik=5 if paksa else 60)
    return {
        "aktif": True,
        "jumlah_baru": sum(t["baru"] for t in toko),
        "jumlah_diperbarui": sum(t["diperbarui"] for t in toko),
        "toko": toko,
    }


def _nama_pengguna(user) -> str | None:
    """Who to record on a printed label: the user's name, else the email."""
    return getattr(user, "nama", None) or getattr(user, "email", None)


@marketplace_erp_router.post("/pesanan/{pesanan_id}/resi/tandai", response_model=PesananOut)
async def tandai_resi_dicetak(
    pesanan_id: str,
    payload: TandaiResiIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_OR_STAFF)),
):
    """Mark the label as printed (default) or clear the mark by hand."""
    pesanan = await services.get_pesanan(session, pesanan_id)
    await pastikan_akses_akun(user, session, pesanan.akun_id)
    return await services.tandai_resi_pesanan(session, pesanan_id, payload.dicetak, _nama_pengguna(user))


@marketplace_erp_router.post("/pesanan/resi-massal")
async def cetak_resi_massal(
    payload: ResiMassalIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_OR_STAFF)),
):
    """One PDF with Shopee's labels for several processed orders (same shop and courier, up to 50)."""
    for pesanan_id in dict.fromkeys(payload.pesanan_ids):
        pesanan = await services.get_pesanan(session, pesanan_id)
        await pastikan_akses_akun(user, session, pesanan.akun_id)
    pdf, nama_file = await services.unduh_resi_massal(session, payload.pesanan_ids, payload.tipe, _nama_pengguna(user))
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{nama_file}"'})


@marketplace_erp_router.post("/pesanan/resi-gabungan")
async def cetak_resi_gabungan(
    payload: ResiGabunganIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_OR_STAFF)),
):
    """Resi untuk pilihan campur (beberapa toko dan kurir) dalam SATU pdf. Pesanan yang ditolak Shopee dilaporkan
    beserta alasannya dan tidak menghalangi yang lain. Hasil berupa JSON (pdf base64) supaya daftar yang gagal ikut."""
    import base64

    for pesanan_id in dict.fromkeys(payload.pesanan_ids):
        pesanan = await services.get_pesanan(session, pesanan_id)
        await pastikan_akses_akun(user, session, pesanan.akun_id)
    hasil = await services.unduh_resi_gabungan(session, payload.pesanan_ids, payload.tipe, _nama_pengguna(user))
    return {**hasil, "pdf": base64.b64encode(hasil["pdf"]).decode("ascii")}


@marketplace_erp_router.post("/pesanan/proses-massal")
async def proses_massal_pesanan(
    payload: ProsesMassalIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_OR_STAFF)),
):
    """Arrange shipment for several Shopee orders. Every order is tried; a failure is reported, not fatal."""
    hasil = []
    # A rollback after one failed order expires loaded objects (including `user`); keep plain values.
    pengguna = SimpleNamespace(role=user.role, id=user.id)
    for pesanan_id in dict.fromkeys(payload.pesanan_ids):
        info = {"id": pesanan_id, "id_eksternal": None, "ok": False, "pesan": None}
        hasil.append(info)
        try:
            pesanan = await services.get_pesanan(session, pesanan_id)
            info["id_eksternal"] = pesanan.id_eksternal
            await pastikan_akses_akun(pengguna, session, pesanan.akun_id)
            await services.proses_pesanan_marketplace(session, pesanan_id)
            await session.commit()  # Shopee already acted: keep this order even if a later one fails
            info["ok"] = True
        except HTTPException as exc:
            await session.rollback()
            info["pesan"] = str(exc.detail)
        except Exception:  # noqa: BLE001 -- report and carry on with the remaining orders
            await session.rollback()
            logger.exception("proses massal gagal untuk pesanan %s", pesanan_id)
            info["pesan"] = "Kesalahan tak terduga."
    return {
        "berhasil": sum(1 for h in hasil if h["ok"]),
        "gagal": sum(1 for h in hasil if not h["ok"]),
        "hasil": hasil,
    }


@marketplace_erp_router.post("/pesanan/{pesanan_id}/proses", response_model=PesananOut)
async def proses_pesanan_marketplace(
    pesanan_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_OR_STAFF)),
):
    """Arrange shipment on the marketplace (courier pickup). Shopee orders pulled by sync only."""
    pesanan = await services.get_pesanan(session, pesanan_id)
    await pastikan_akses_akun(user, session, pesanan.akun_id)
    return await services.proses_pesanan_marketplace(session, pesanan_id)


@marketplace_erp_router.post("/pesanan/{pesanan_id}/batalkan", response_model=PesananOut)
async def batalkan_pesanan_marketplace(
    pesanan_id: str,
    payload: BatalkanPesananIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_OR_STAFF)),
):
    """Cancel a Shopee order on Shopee (before shipment) and release its reserved stock."""
    pesanan = await services.get_pesanan(session, pesanan_id)
    await pastikan_akses_akun(user, session, pesanan.akun_id)
    return await services.batalkan_pesanan_marketplace(session, pesanan_id, payload.alasan)


@marketplace_erp_router.get("/pesanan/{pesanan_id}/resi")
async def cetak_resi_pesanan(
    pesanan_id: str,
    tipe: str | None = Query(None, pattern="^(THERMAL_AIR_WAYBILL|NORMAL_AIR_WAYBILL)$", description="default: thermal (A6)"),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_OR_STAFF)),
):
    """The marketplace's own shipping label (PDF) for an order that was already processed.

    ``tipe``: THERMAL_AIR_WAYBILL (100x150 mm, about A6; the default when offered) or NORMAL_AIR_WAYBILL (A4).
    """
    pesanan = await services.get_pesanan(session, pesanan_id)
    await pastikan_akses_akun(user, session, pesanan.akun_id)
    pdf, nama_file = await services.unduh_resi_pesanan(session, pesanan_id, tipe, _nama_pengguna(user))
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{nama_file}"'})


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
    user: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    """Return the Shopee authorize URL. FE should redirect the browser there.

    ``redirect_uri`` should land on /oauth/shopee/callback/{akun_id} (or an
    equivalent FE proxy). Partner credentials come from env only.
    """
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    akun = await services.get_akun_marketplace(session, akun_id)
    if akun.platform != "shopee":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Akun bukan platform shopee")
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import oauth_security
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.token_crypto import keyring

    keyring()  # do not authorize a shop if token storage cannot encrypt its credentials
    configured = (os.getenv("SHOPEE_REDIRECT_URI") or "").strip()
    base_redirect = (redirect_uri or configured).strip()
    if not base_redirect:
        raise HTTPException(status_code=501, detail="SHOPEE_REDIRECT_URI belum diisi")
    nonce = await oauth_security.issue_nonce(session, akun_id, user)
    target = oauth_security.callback_url(base_redirect, akun_id, nonce, configured)
    url = erp_shopee.build_authorize_url(redirect_uri=target)
    return OAuthStartOut(platform="shopee", akun_id=akun_id, authorize_url=url)


@marketplace_erp_router.get("/oauth/shopee/callback/{akun_id}")
@marketplace_erp_router.get("/oauth/shopee/callback/{akun_id}/{nonce}")
async def oauth_shopee_callback(
    akun_id: str,
    code: str = Query(...),
    shop_id: str | None = Query(None),
    main_account_id: str | None = Query(None),
    nonce: str | None = None,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    """Exchange OAuth code for tokens and persist on AkunMarketplace.

    Requires the initiating owner session and its unused, unexpired nonce.
    The nonce is carried inside Shopee's redirect path.
    Shopee returns ``shop_id`` when a shop account authorised, or ``main_account_id``
    when a main account authorised (possibly several shops at once).
    """
    import asyncio

    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    if bool(shop_id) == bool(main_account_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Callback Shopee harus berisi shop_id atau main_account_id",
        )
    akun = await services.get_akun_marketplace(session, akun_id)
    if akun.platform != "shopee":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Akun bukan platform shopee")

    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import oauth_security
    await oauth_security.consume_nonce(session, akun_id, nonce, user)

    if main_account_id:
        payload = await erp_shopee.exchange_token(code=code, main_account_id=str(main_account_id))
        # Shop names make the new rows recognisable; failures just fall back to "Shopee <id>".
        shop_ids = [str(sid) for sid in payload.get("shop_id_list") or []]
        names = await asyncio.gather(
            *(erp_shopee.get_shop_name(str(payload.get("access_token") or ""), sid) for sid in shop_ids)
        )
        toko = await services.hubungkan_shopee_akun_utama(
            session, akun, payload, {sid: n for sid, n in zip(shop_ids, names) if n}
        )
        return {"ok": True, "akun_id": toko[0]["akun_id"], "status": "terhubung", "toko": toko}

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
        "toko": [
            {"akun_id": akun.id, "id_toko_eksternal": akun.id_toko_eksternal, "nama_toko": akun.nama_toko, "baru": False}
        ],
    }


@marketplace_erp_router.post("/akun/{akun_id}/sync/pesanan")
async def sync_pesanan_akun(
    akun_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    """Trigger platform order pull. Live Shopee sync gated by SHOPEE_LIVE_SYNC."""
    akun = await services.get_akun_marketplace(session, akun_id)
    if akun.platform == "shopee":
        try:
            hasil = await services.sinkron_pesanan_akun(session, akun, penuh=True)  # explicit click: full window
        except NotImplementedError as exc:
            raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)) from exc
        return {"ok": True, **hasil}
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=f"Sync pesanan untuk platform '{akun.platform}' belum tersedia (Shopee first)",
    )


async def _proses_push_di_latar(shop_id: str) -> None:
    """After the answer is sent (Shopee waits 3 s only): pull that shop's order changes."""
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import database as erp_database

    if erp_database.SessionLocal is None:
        return
    try:
        async with erp_database.SessionLocal() as session:
            await services.sinkron_karena_push(session, shop_id)
            await session.commit()
    except Exception:  # noqa: BLE001 -- the periodic sync still catches the change
        logger.exception("sinkron karena push gagal (toko %s)", shop_id)


@marketplace_erp_router.get("/shopee/push")
async def cek_penerima_push_shopee():
    """Reachability check for the callback URL (open it in a browser): says nothing about keys or data."""
    return {"ok": True, "pesan": "Penerima push Shopee aktif"}


@marketplace_erp_router.post("/shopee/push")
async def terima_push_shopee(
    request: Request,
    latar: BackgroundTasks,
    session: AsyncSession = Depends(get_db_marketplace_erp),
):
    """Shopee Push Mechanism callback (no login: the Authorization signature proves it is Shopee). A valid order push
    makes the ERP pull that shop's changes at once; every push, accepted or not, is written to the push log."""
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import shopee_push

    badan = await request.body()
    push = shopee_push.urai(badan)
    ringkas = shopee_push.ringkas(push or {})
    urls = shopee_push.kandidat_url(str(request.url), {k.lower(): v for k, v in request.headers.items()})
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    valid, diagnosis = shopee_push.verifikasi(
        shopee_push.push_key(),
        urls,
        badan,
        request.headers.get("authorization"),
        kunci_lain={"partner_key": erp_shopee.SHOPEE_PARTNER_KEY},
    )
    if not ringkas["kode"]:
        # Not a push: an empty/odd body, or code 0 = the test push of Open Platform's "Verify" button (real pushes have
        # code 1 and up). It changes nothing, so it is answered 200 (the URL is reachable) and logged with how its
        # signature compared, which shows how Shopee signs.
        import json as _json

        await services.catat_push(session, valid=False, ringkas=ringkas, hasil="probe", catatan=_json.dumps(diagnosis), badan=badan[:500])
        await session.commit()
        # Verify only passes on 2xx with an empty body (developer guide 18).
        return Response(status_code=status.HTTP_200_OK)
    if not valid:
        import json as _json

        hasil = "key_belum_diatur" if not diagnosis["ada_key"] else "tanda_tangan_salah"
        await services.catat_push(session, valid=False, ringkas=ringkas, hasil=hasil, catatan=_json.dumps(diagnosis), badan=badan)
        await session.commit()  # the log must survive the 401
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Tanda tangan push tidak valid")
    try:
        fresh = await shopee_push.claim_push(session, badan, push)
    except ValueError:
        await services.catat_push(session, valid=False, ringkas=ringkas, hasil="timestamp_tidak_valid")
        await session.commit()
        raise HTTPException(status_code=401, detail="Waktu push tidak valid") from None
    if not fresh:
        return Response(status_code=status.HTTP_200_OK)
    if ringkas["kode"] in shopee_push.KODE_PESANAN and ringkas["shop_id"]:
        await services.catat_push(session, valid=True, ringkas=ringkas, hasil="diproses", badan=badan)
        await session.commit()  # logged before the background pull starts
        latar.add_task(_proses_push_di_latar, ringkas["shop_id"])
    else:
        await services.catat_push(session, valid=True, ringkas=ringkas, hasil="dicatat", badan=badan)
    return Response(status_code=status.HTTP_200_OK)


@marketplace_erp_router.get("/shopee/push/pengaturan")
async def baca_pengaturan_push(_: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY))):
    """Callback URL yang disimpan dan pengaturan push di Shopee (setelah Verify)."""
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import shopee_push
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    try:
        jarak = await erp_shopee.baca_push()
    except Exception as exc:  # noqa: BLE001
        jarak = {"error": str(exc)}
    return {"callback_url": shopee_push.CALLBACK_URL, "nyala": shopee_push.PUSH_AKTIF, "shopee": jarak}


@marketplace_erp_router.post("/shopee/push/pengaturan")
async def simpan_pengaturan_push(_: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY))):
    """Simpan callback URL yang sudah Verify dan nyalakan push pesanan/otorisasi."""
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import shopee_push
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    hasil = await erp_shopee.atur_push(callback_url=shopee_push.CALLBACK_URL, nyala=shopee_push.PUSH_AKTIF)
    return {"ok": True, "callback_url": shopee_push.CALLBACK_URL, "nyala": shopee_push.PUSH_AKTIF, "shopee": hasil}


@marketplace_erp_router.get("/shopee/push/log")
async def log_push_shopee(
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    """The latest pushes Shopee sent (newest first): what arrived, from which shop, and what the ERP did with it."""
    return await services.list_push(session, limit)



@marketplace_erp_router.get("/akun/{akun_id}/iklan/saran")
async def saran_iklan_akun(
    akun_id: str,
    item_id: int,
    kata: str | None = Query(None, max_length=100),
    bidding: str = Query("auto", pattern="^(auto|manual)$"),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    """Saran Shopee sebelum membuat iklan: target ROAS, anggaran harian, dan kata kunci (volume + bid saran)."""
    akun = await services.get_akun_marketplace(session, akun_id)
    return await services.saran_iklan(session, akun, item_id, kata, bidding)


@marketplace_erp_router.get("/akun/{akun_id}/iklan/kampanye")
async def daftar_kampanye_iklan_akun(
    akun_id: str,
    hari: int = Query(7, ge=2, le=28),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    """Kampanye iklan produk toko ini: pengaturan, kata kunci, performa ``hari`` hari terakhir, dan saldo iklan."""
    akun = await services.get_akun_marketplace(session, akun_id)
    return await services.daftar_kampanye_iklan(session, akun, hari)


@marketplace_erp_router.post("/akun/{akun_id}/iklan/saran-ai")
async def saran_ai_iklan_akun(
    akun_id: str,
    hari: int = Query(7, ge=2, le=28),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    pengguna: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    """Analisis AI atas kampanye toko ini. Hanya memberi saran; tidak mengubah apa pun di Shopee."""
    akun = await services.get_akun_marketplace(session, akun_id)
    hasil = await services.saran_ai_iklan(session, akun, pengguna, hari)
    await session.commit()  # the usage row must survive even if the response is lost
    return hasil


@marketplace_erp_router.put("/akun/{akun_id}/iklan/modal/{item_id}")
async def simpan_modal_produk_akun(
    akun_id: str,
    item_id: str,
    payload: dict = Body(),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    pengguna: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    """Modal (harga pokok) satu produk Shopee: `modal_rp` ATAU `modal_persen`; keduanya kosong = hapus."""
    akun = await services.get_akun_marketplace(session, akun_id)
    hasil = await services.simpan_modal_produk(session, akun, item_id, payload.get("modal_rp"), payload.get("modal_persen"), pengguna)
    await session.commit()
    return hasil


@marketplace_erp_router.post("/akun/{akun_id}/iklan/kampanye")
async def buat_kampanye_iklan_akun(
    akun_id: str,
    payload: dict = Body(),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    pengguna: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    """Buat iklan produk di Shopee (GMV Max otomatis atau manual). Memakai uang sungguhan."""
    akun = await services.get_akun_marketplace(session, akun_id)
    return await services.ubah_iklan_shopee(session, akun, "buat", None, payload, pengguna)


@marketplace_erp_router.post("/akun/{akun_id}/iklan/kampanye/{campaign_id}/aksi")
async def aksi_kampanye_iklan_akun(
    akun_id: str,
    campaign_id: int,
    payload: dict = Body(),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    pengguna: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    """Jeda, lanjutkan, hentikan, hapus, ubah anggaran atau target ROAS satu kampanye."""
    akun = await services.get_akun_marketplace(session, akun_id)
    return await services.ubah_iklan_shopee(session, akun, "aksi", campaign_id, payload, pengguna)


@marketplace_erp_router.post("/akun/{akun_id}/iklan/kampanye/{campaign_id}/kata-kunci")
async def kata_kunci_kampanye_iklan_akun(
    akun_id: str,
    campaign_id: int,
    payload: dict = Body(),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    pengguna: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    """Tambah, hapus, atau ubah bid dan tipe kata kunci satu kampanye manual."""
    akun = await services.get_akun_marketplace(session, akun_id)
    return await services.ubah_iklan_shopee(session, akun, "kata_kunci", campaign_id, payload, pengguna)


@marketplace_erp_router.post("/akun/{akun_id}/iklan/shopee/{aksi}")
async def iklan_shopee(
    akun_id: str,
    aksi: str,
    payload: dict = Body(default={}),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    """Semua API iklan Shopee yang terdokumentasi. `params` untuk GET, `body` untuk POST."""
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import shopee_iklan

    akun = await services.get_akun_marketplace(session, akun_id)
    try:
        return await shopee_iklan.panggil(
            session, akun, aksi, params=payload.get("params") or None, body=payload.get("body") or None
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@marketplace_erp_router.get("/iklan/shopee/aksi")
async def daftar_aksi_iklan(_: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY))):
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import shopee_iklan
    return [{"aksi": k, "method": v[0], "path": v[1]} for k, v in shopee_iklan.IKLAN_API.items()]


@marketplace_erp_router.post("/akun/{akun_id}/sync/iklan")
async def sync_iklan_akun(
    akun_id: str,
    hari: int = Query(30, ge=1, le=180, description="berapa hari ke belakang (Shopee menyimpan 6 bulan)"),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    """Pull this shop's Shopee Ads performance per day and its ads balance."""
    akun = await services.get_akun_marketplace(session, akun_id)
    if akun.platform == "shopee":
        try:
            hasil = await services.sinkron_iklan_akun(session, akun, hari)
        except NotImplementedError as exc:
            raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)) from exc
        return {"ok": True, **hasil}
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=f"Iklan untuk platform '{akun.platform}' belum tersedia (Shopee first)",
    )


@marketplace_erp_router.get("/iklan-toko/ringkasan")
async def ringkasan_iklan_toko(
    dari: date | None = None,
    sampai: date | None = None,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    """Shopee Ads totals per shop for the period (dates are Shopee/WIB days), with each shop's latest ads balance."""
    return await services.ringkasan_iklan_toko(session, dari=dari, sampai=sampai)


@marketplace_erp_router.get("/iklan-toko/harian")
async def list_iklan_harian_toko(
    akun_id: str | None = Query(None, description="kosong = semua toko"),
    dari: date | None = None,
    sampai: date | None = None,
    urut: str = Query("tanggal:desc", description="<kolom>:asc|desc, kolom: tanggal toko biaya tayang klik pesanan gmv roas"),
    halaman: int = Query(1, ge=1),
    per_halaman: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    """Shopee Ads performance per shop per day, with the shop name on every row."""
    return await services.list_iklan_harian_toko(
        session, akun_id=akun_id, dari=dari, sampai=sampai, urut=urut, halaman=halaman, per_halaman=per_halaman
    )


@marketplace_erp_router.post("/akun/{akun_id}/sync/settlement")
async def sync_settlement_akun(
    akun_id: str,
    hari: int = Query(15, ge=1, le=90, description="berapa hari ke belakang"),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    """Pull what Shopee released to this shop (money per order). Already stored orders are not read again;
    ``sisa`` > 0 means more is waiting: pull again."""
    akun = await services.get_akun_marketplace(session, akun_id)
    if akun.platform == "shopee":
        try:
            hasil = await services.sinkron_settlement_akun(session, akun, hari)
        except NotImplementedError as exc:
            raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)) from exc
        return {"ok": True, **hasil}
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=f"Settlement untuk platform '{akun.platform}' belum tersedia (Shopee first)",
    )


@marketplace_erp_router.post("/akun/{akun_id}/sync/produk")
async def sync_produk_akun(
    akun_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    """Pull the shop catalogue and link entries to Produk by SKU (no stock/price is changed)."""
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    akun = await services.get_akun_marketplace(session, akun_id)
    if akun.platform == "shopee":
        try:
            mentah = await erp_shopee.ambil_item_mentah(session, akun)
        except NotImplementedError as exc:
            raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)) from exc
        rows = [r for it, mr in mentah for r in erp_shopee.normalisasi_item(it, mr)]
        hasil = await services.impor_listing_marketplace(session, akun, rows)
        katalog = await services.simpan_katalog_shopee(
            session, akun, [erp_shopee.normalisasi_katalog(it, mr) for it, mr in mentah]
        )
        return {"ok": True, "pulled": len(rows), **hasil, **katalog}
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=f"Sync produk untuk platform '{akun.platform}' belum tersedia (Shopee first)",
    )


@marketplace_erp_router.get("/katalog-shopee")
async def list_katalog_shopee(
    akun_id: str | None = Query(None, description="kosong = semua toko"),
    q: str | None = Query(None, max_length=100),
    status: str | None = Query(None, description="status Shopee: NORMAL (aktif), UNLIST (tidak aktif), BANNED (diblokir), REVIEWING (ditinjau); kosong = semua"),
    belum_dikirim: bool = Query(False),
    urut: str = Query("toko:asc", description="<kolom>:asc|desc, kolom: toko nama sku harga stok berat status dikirim diambil"),
    halaman: int = Query(1, ge=1),
    per_halaman: int = Query(48, ge=1, le=100),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_OR_STAFF)),
):
    """Products pulled from the Shopee shops, each row tagged with its shop. Never merged across shops."""
    return await services.list_katalog_shopee(
        session, akun_id=akun_id, q=q, status=status, belum_dikirim=belum_dikirim, urut=urut, halaman=halaman, per_halaman=per_halaman
    )


@marketplace_erp_router.get("/katalog-shopee/ringkasan")
async def ringkasan_katalog_shopee(
    status: str | None = Query(None, description="hitung toko hanya untuk status ini (kosong = semua)"),
    akun_id: str | None = Query(None, description="hitung status hanya untuk toko ini (kosong = semua)"),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_OR_STAFF)),
):
    """Filter options: products per Shopee shop (within ``status``) and per Shopee status (within ``akun_id``);
    shops never synced show 0."""
    jumlah = await services.jumlah_katalog_per_toko(session, status)
    toko = [a for a in await services.list_akun_marketplace(session, platform="shopee") if a.id_toko_eksternal]
    return {
        "total": sum(jumlah.values()),
        "toko": [{"akun_id": a.id, "nama_toko": a.nama_toko, "jumlah": jumlah.get(a.id, 0)} for a in toko],
        "status": await services.jumlah_katalog_per_status(session, akun_id),
    }


@marketplace_erp_router.get("/katalog-shopee/{katalog_id}")
async def get_katalog_shopee(
    katalog_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_OR_STAFF)),
):
    k, nama_toko = await services.get_katalog_shopee(session, katalog_id)
    return services.katalog_out(k, nama_toko, lengkap=True)


@marketplace_erp_router.post("/katalog-shopee/kirim-toko")
async def kirim_katalog_ke_toko(
    payload: KatalogKirimIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    store_session: AsyncSession = Depends(_store_db),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    """Copy chosen Shopee products to the online store: one-way, stock 0, draft unless ``aktif``.

    Photos (up to 7) are copied into the store's own bucket. Variants come along with stock 0. Nothing is
    written to Shopee, and later edits in the store admin never flow back here.
    """
    from decimal import Decimal

    from tenants.store.modules.store.application.schemas import MAKS_FOTO_PRODUK, VarianIn

    hasil = []
    for katalog_id in dict.fromkeys(payload.ids):
        k, nama_toko = await services.get_katalog_shopee(session, katalog_id)
        info = {"id": k.id, "nama": k.nama, "nama_toko": nama_toko}
        if k.dikirim_toko_id and not payload.timpa:
            hasil.append({**info, "hasil": "dilewati", "pesan": "Sudah dikirim ke toko web"})
            continue
        data = services.katalog_out(k, nama_toko, lengkap=True)
        baru = k.dikirim_toko_id is None
        urls = data["foto"][:MAKS_FOTO_PRODUK] if baru else []
        keys = [key for key in [await import_foto_dari_url(u) for u in urls] if key]
        toko_produk, dibuat = await store_services.upsert_produk_dari_erp(
            store_session,
            erp_produk_id=f"shopee:{k.id}",
            nama=k.nama,
            deskripsi=k.deskripsi,
            harga=k.harga_min if k.harga_min is not None else Decimal("0"),
            stok=0,
            platform_asal="shopee",
            foto_key=keys[0] if keys else None,
            aktif=payload.aktif,
            berat_gram=k.berat_gram,
            panjang_cm=k.panjang_cm,
            lebar_cm=k.lebar_cm,
            tinggi_cm=k.tinggi_cm,
        )
        for key in keys[1:]:
            await store_services._tambah_foto_ke_galeri(store_session, toko_produk, key, melewati_batas=False)
        if dibuat and data["varian"]:
            dipakai: set[str] = set()
            daftar = []
            for i, v in enumerate(data["varian"], 1):
                nama_v = (v["nama"] or f"Varian {i}").strip()[:110]
                if nama_v.lower() in dipakai:
                    nama_v = f"{nama_v} ({i})"
                dipakai.add(nama_v.lower())
                daftar.append(
                    VarianIn(nama=nama_v, sku=(v["sku"] or "")[:64], harga=Decimal(v["harga"]) if v["harga"] else None, stok=0)
                )
            await store_services.ganti_varian(store_session, toko_produk.id, daftar)
        k.dikirim_toko_id = toko_produk.id
        k.dikirim_at = datetime.now(timezone.utc)
        await session.flush()
        hasil.append(
            {**info, "hasil": "dibuat" if dibuat else "diperbarui", "produk_toko_id": toko_produk.id, "foto": len(keys)}
        )
    return {"ok": True, "hasil": hasil}


@marketplace_erp_router.post("/akun/{akun_id}/push/stok-harga")
async def push_stok_harga_akun(
    akun_id: str,
    dry_run: bool = Query(True, description="true = only show what would be sent (default)"),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    """Send ERP stock and price of this shop's active listings to the marketplace.

    This OVERWRITES the marketplace's stock/price, so it only sends when ``dry_run=false`` is passed
    explicitly; the default just previews the rows.
    """
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    akun = await services.get_akun_marketplace(session, akun_id)
    if akun.platform != "shopee":
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"Push stok/harga untuk platform '{akun.platform}' belum tersedia (Shopee first)",
        )
    rows = await services.baris_push_listing(session, akun)
    if dry_run:
        return {"ok": True, "dry_run": True, "jumlah": len(rows), "rows": rows}
    hasil = await erp_shopee.kirim_stok_harga(session, akun, rows)
    return {"ok": True, "dry_run": False, "jumlah": len(rows), **hasil}


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


@marketplace_erp_router.get("/settlement-pesanan/ringkasan")
async def ringkasan_settlement_pesanan(
    dari: datetime | None = None,
    sampai: datetime | None = None,
    q: str | None = Query(None, max_length=64),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    """Money released by Shopee, totals per shop for the period (by release date)."""
    return await services.ringkasan_settlement_pesanan(session, dari=dari, sampai=sampai, q=q)


@marketplace_erp_router.get("/settlement-pesanan")
async def list_settlement_pesanan(
    akun_id: str | None = Query(None, description="kosong = semua toko"),
    dari: datetime | None = None,
    sampai: datetime | None = None,
    q: str | None = Query(None, max_length=64),
    urut: str = Query("dirilis:desc", description="<kolom>:asc|desc, kolom: dirilis pesanan toko penjualan komisi layanan ongkir cair"),
    halaman: int = Query(1, ge=1),
    per_halaman: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    """Money released by Shopee per order, with the shop name on every row."""
    return await services.list_settlement_pesanan(
        session, akun_id=akun_id, dari=dari, sampai=sampai, q=q, urut=urut, halaman=halaman, per_halaman=per_halaman
    )


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


@marketplace_erp_router.get("/laporan/dashboard")
async def laporan_dashboard(
    dari: datetime = Query(...),
    sampai: datetime = Query(...),
    batas_stok_kritis: int = Query(5, ge=0, le=100000),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*OWNER_ONLY)),
):
    """The tables of the dashboard (per shop, per stage, best sellers, per day, low stock) for one period."""
    if sampai < dari:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="sampai sebelum dari")
    return await services.laporan_dashboard(session, dari=dari, sampai=sampai, batas_stok_kritis=batas_stok_kritis)


# --- Tahap 4: Iklan (ads) -------------------------------------------------------


@marketplace_erp_router.get("/iklan", response_model=list[IklanCampaignOut])
async def list_campaign(
    akun_id: str | None = None,
    platform: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    return await services.list_campaign(session, akun_id=akun_id, platform=platform, status_filter=status_filter)


@marketplace_erp_router.post("/iklan", response_model=IklanCampaignOut, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    payload: IklanCampaignIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    return await services.create_campaign(session, payload)


@marketplace_erp_router.get("/iklan/{campaign_id}", response_model=IklanCampaignOut)
async def get_campaign(
    campaign_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    return await services.get_campaign(session, campaign_id)


@marketplace_erp_router.patch("/iklan/{campaign_id}", response_model=IklanCampaignOut)
async def update_campaign(
    campaign_id: str,
    payload: IklanCampaignPatch,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    return await services.update_campaign(session, campaign_id, payload)


@marketplace_erp_router.delete("/iklan/{campaign_id}")
async def delete_campaign(
    campaign_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    await services.delete_campaign(session, campaign_id)
    return {"ok": True}


@marketplace_erp_router.post("/iklan/{campaign_id}/metrik", response_model=IklanMetrikHarianOut)
async def record_metrik_harian(
    campaign_id: str,
    payload: IklanMetrikHarianIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    return await services.record_metrik_harian(session, campaign_id, payload)


@marketplace_erp_router.get("/iklan/{campaign_id}/metrik", response_model=list[IklanMetrikHarianOut])
async def list_metrik_harian(
    campaign_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    return await services.list_metrik_harian(session, campaign_id)


@marketplace_erp_router.get("/iklan/{campaign_id}/laporan", response_model=IklanLaporanOut)
async def laporan_iklan(
    campaign_id: str,
    dari: datetime = Query(...),
    sampai: datetime = Query(...),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _: UserMarketplaceErp = Depends(require_roles_marketplace_erp(*ADMIN_ONLY)),
):
    if sampai < dari:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="sampai sebelum dari")
    return await services.laporan_iklan(session, campaign_id, dari=dari, sampai=sampai)
