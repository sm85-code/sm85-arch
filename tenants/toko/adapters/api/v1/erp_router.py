"""HTTP surface for the toko-erp (marketplace ERP) submodule.

Mounted in main.py as prefix=/api/toko/marketplace -- a separate prefix from
/api/toko so nothing here overlaps or changes any existing toko-web route.
Covers Shopee/Lazada/Blibli order & chat inbox management plus the
ERP-catalog-to-web-catalog one-way copy. See
tenants/toko/modules/erp/infrastructure/erp_shopee.py, erp_lazada.py and
erp_blibli.py for the (not yet connected) platform sync adapters this
module will eventually call.

Auth: reuses require_roles_toko from the toko web module -- no separate
auth system for this submodule. Three roles can reach the marketplace admin
endpoints below: the legacy full-access `owner`/`admin_toko` (unrestricted,
see everything) and the newer `admin_marketplace` staff role, which is
further restricted to only the AkunMarketplace (shop) rows assigned to that
user via toko_staff_akun -- see infrastructure/auth.py::akun_ids_diizinkan
and ::pastikan_akses_akun, applied consistently below. The `/admin/akun`
CRUD endpoints (create/edit/delete shop accounts + credentials) stay
owner/admin_toko-only -- admin_marketplace staff work WITHIN an assigned
akun, they never manage which akun exist or their credentials.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.toko.modules.erp.application import services
from tenants.toko.modules.erp.application.schemas import (
    AkunMarketplaceIn,
    AkunMarketplacePatch,
    PercakapanERPIn,
    PesanChatERPIn,
    ProdukERPIn,
    ProdukERPPatch,
    StatusPesananERPIn,
)
from tenants.toko.modules.toko.application import services as toko_services
from tenants.toko.modules.toko.infrastructure.auth import (
    FULL_ACCESS_ROLES_TOKO,
    ROLE_ADMIN_MARKETPLACE,
    akun_ids_diizinkan,
    pastikan_akses_akun,
    require_roles_toko,
)
from tenants.toko.modules.toko.infrastructure.database import get_db_toko
from tenants.toko.modules.toko.infrastructure.models import UserToko

erp_router = APIRouter()

# Marketplace admin surface (produk/pesanan/chat/laporan): legacy full-access
# roles + the new per-akun-scoped staff role. Scoping itself is enforced
# per-endpoint below via akun_ids_diizinkan/pastikan_akses_akun, NOT by this
# tuple -- this tuple only decides who gets past the door at all.
ADMIN_ROLES = (*FULL_ACCESS_ROLES_TOKO, ROLE_ADMIN_MARKETPLACE)

# AkunMarketplace CRUD (create/edit/delete shop accounts + credentials):
# legacy full-access roles ONLY -- admin_marketplace staff must not manage
# which shops exist or their credentials, only work within ones assigned to
# them.
AKUN_MANAGE_ROLES = FULL_ACCESS_ROLES_TOKO


# --- Akun Marketplace ---------------------------------------------------


@erp_router.get("/admin/akun")
async def admin_list_akun_marketplace(
    platform: str | None = None,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*AKUN_MANAGE_ROLES)),
):
    akun = await services.list_akun_marketplace(session, platform=platform)
    return [services.akun_out(a) for a in akun]


@erp_router.post("/admin/akun")
async def admin_create_akun_marketplace(
    payload: AkunMarketplaceIn,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*AKUN_MANAGE_ROLES)),
):
    akun = await services.create_akun_marketplace(session, payload)
    return services.akun_out(akun)


@erp_router.get("/admin/akun/{akun_id}")
async def admin_get_akun_marketplace(
    akun_id: str,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*AKUN_MANAGE_ROLES)),
):
    akun = await services.get_akun_marketplace(session, akun_id)
    return services.akun_out(akun)


@erp_router.patch("/admin/akun/{akun_id}")
async def admin_patch_akun_marketplace(
    akun_id: str,
    payload: AkunMarketplacePatch,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*AKUN_MANAGE_ROLES)),
):
    akun = await services.update_akun_marketplace(session, akun_id, payload)
    return services.akun_out(akun)


@erp_router.delete("/admin/akun/{akun_id}")
async def admin_delete_akun_marketplace(
    akun_id: str,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*AKUN_MANAGE_ROLES)),
):
    await services.delete_akun_marketplace(session, akun_id)
    return {"ok": True}


# --- Produk ERP --------------------------------------------------------------


@erp_router.get("/admin/erp/produk")
async def admin_list_produk_erp(
    platform: str | None = None,
    akun_id: str | None = None,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    allowed = await akun_ids_diizinkan(_user, session)
    if allowed is not None:
        if akun_id is not None:
            await pastikan_akses_akun(_user, session, akun_id)
            produk = await services.list_produk_erp(session, platform=platform, akun_id=akun_id)
        else:
            produk = await services.list_produk_erp(session, platform=platform, akun_ids=allowed)
    else:
        produk = await services.list_produk_erp(session, platform=platform, akun_id=akun_id)
    return [services.produk_erp_out(p) for p in produk]


@erp_router.get("/admin/erp/produk/{produk_id}")
async def admin_get_produk_erp(
    produk_id: str,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    produk = await services.get_produk_erp(session, produk_id)
    await pastikan_akses_akun(_user, session, produk.akun_id)
    return services.produk_erp_out(produk)


@erp_router.post("/admin/erp/produk")
async def admin_create_produk_erp(
    payload: ProdukERPIn,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    await pastikan_akses_akun(_user, session, payload.akun_id)
    produk = await services.create_produk_erp(session, payload)
    return services.produk_erp_out(produk)


@erp_router.patch("/admin/erp/produk/{produk_id}")
async def admin_patch_produk_erp(
    produk_id: str,
    payload: ProdukERPPatch,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    produk = await services.get_produk_erp(session, produk_id)
    await pastikan_akses_akun(_user, session, produk.akun_id)
    produk = await services.update_produk_erp(session, produk_id, payload)
    return services.produk_erp_out(produk)


@erp_router.delete("/admin/erp/produk/{produk_id}")
async def admin_delete_produk_erp(
    produk_id: str,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    produk = await services.get_produk_erp(session, produk_id)
    await pastikan_akses_akun(_user, session, produk.akun_id)
    await services.delete_produk_erp(session, produk_id)
    return {"ok": True}


@erp_router.post("/admin/erp/produk/{produk_id}/copy-ke-web")
async def admin_copy_produk_ke_web(
    produk_id: str,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    """Copy satu produk ERP ke katalog toko web sebagai baris baru
    (snapshot, bukan referensi live) -- lihat
    erp.application.services.copy_produk_ke_web."""
    produk = await services.get_produk_erp(session, produk_id)
    await pastikan_akses_akun(_user, session, produk.akun_id)
    produk_toko = await services.copy_produk_ke_web(session, produk_id)
    return toko_services.produk_out(produk_toko)


# --- Pesanan ERP ---------------------------------------------------------


@erp_router.get("/admin/erp/pesanan")
async def admin_list_pesanan_erp(
    platform: str | None = None,
    akun_id: str | None = None,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    allowed = await akun_ids_diizinkan(_user, session)
    if allowed is not None:
        if akun_id is not None:
            await pastikan_akses_akun(_user, session, akun_id)
            pesanan = await services.list_pesanan_erp(session, platform=platform, akun_id=akun_id)
        else:
            pesanan = await services.list_pesanan_erp(session, platform=platform, akun_ids=allowed)
    else:
        pesanan = await services.list_pesanan_erp(session, platform=platform, akun_id=akun_id)
    return [services.pesanan_erp_out(p) for p in pesanan]


@erp_router.get("/admin/erp/pesanan/{pesanan_id}")
async def admin_get_pesanan_erp(
    pesanan_id: str,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    pesanan = await services.get_pesanan_erp(session, pesanan_id)
    await pastikan_akses_akun(_user, session, pesanan.akun_id)
    return services.pesanan_erp_out(pesanan)


@erp_router.patch("/admin/erp/pesanan/{pesanan_id}/status")
async def admin_ubah_status_pesanan_erp(
    pesanan_id: str,
    payload: StatusPesananERPIn,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    pesanan = await services.get_pesanan_erp(session, pesanan_id)
    await pastikan_akses_akun(_user, session, pesanan.akun_id)
    pesanan = await services.ubah_status_pesanan_erp(session, pesanan_id, payload.status)
    return services.pesanan_erp_out(pesanan)


# --- Chat ERP (inbox gabungan Shopee/Lazada/Blibli) -----------------------


@erp_router.get("/admin/erp/chat")
async def admin_list_percakapan_erp(
    platform: str | None = None,
    akun_id: str | None = None,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    allowed = await akun_ids_diizinkan(_user, session)
    if allowed is not None:
        if akun_id is not None:
            await pastikan_akses_akun(_user, session, akun_id)
            percakapan = await services.list_percakapan_erp(session, platform=platform, akun_id=akun_id)
        else:
            percakapan = await services.list_percakapan_erp(session, platform=platform, akun_ids=allowed)
    else:
        percakapan = await services.list_percakapan_erp(session, platform=platform, akun_id=akun_id)
    return [services.percakapan_erp_out(p) for p in percakapan]


@erp_router.post("/admin/erp/chat")
async def admin_buka_percakapan_erp(
    payload: PercakapanERPIn,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    await pastikan_akses_akun(_user, session, payload.akun_id)
    percakapan = await services.get_or_create_percakapan_erp(session, payload)
    return services.percakapan_erp_out(percakapan)


@erp_router.get("/admin/erp/chat/{percakapan_id}")
async def admin_get_percakapan_erp(
    percakapan_id: str,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    percakapan = await services.get_percakapan_erp(session, percakapan_id)
    await pastikan_akses_akun(_user, session, percakapan.akun_id)
    return services.percakapan_erp_out(percakapan, dengan_pesan=True)


@erp_router.post("/admin/erp/chat/{percakapan_id}")
async def admin_kirim_pesan_erp(
    percakapan_id: str,
    payload: PesanChatERPIn,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    """Admin membalas pembeli marketplace. Pengiriman balik ke platform asli
    (Shopee/Lazada/Blibli) BELUM terhubung -- lihat infrastructure/
    erp_<platform>.py::sync_chat -- untuk sekarang pesan hanya tersimpan di
    inbox internal ini."""
    percakapan = await services.get_percakapan_erp(session, percakapan_id)
    await pastikan_akses_akun(_user, session, percakapan.akun_id)
    await services.kirim_pesan_erp(session, percakapan_id, isi=payload.isi, pengirim_admin=True)
    percakapan = await services.get_percakapan_erp(session, percakapan_id)
    return services.percakapan_erp_out(percakapan, dengan_pesan=True)


# --- Laporan Penjualan ERP ---------------------------------------------------


@erp_router.get("/admin/laporan/penjualan")
async def admin_laporan_penjualan_erp(
    dari: date,
    sampai: date,
    platform: str | None = None,
    akun_id: str | None = None,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    allowed = await akun_ids_diizinkan(_user, session)
    if allowed is not None:
        if akun_id is not None:
            await pastikan_akses_akun(_user, session, akun_id)
            return await services.laporan_penjualan_erp(session, dari, sampai, platform=platform, akun_id=akun_id)
        return await services.laporan_penjualan_erp(session, dari, sampai, platform=platform, akun_ids=allowed)
    return await services.laporan_penjualan_erp(session, dari, sampai, platform=platform, akun_id=akun_id)


@erp_router.get("/admin/laporan/produk-terlaris")
async def admin_laporan_produk_terlaris_erp(
    dari: date,
    sampai: date,
    limit: int = 10,
    platform: str | None = None,
    akun_id: str | None = None,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    allowed = await akun_ids_diizinkan(_user, session)
    if allowed is not None:
        if akun_id is not None:
            await pastikan_akses_akun(_user, session, akun_id)
            return await services.laporan_produk_terlaris_erp(
                session, dari, sampai, limit, platform=platform, akun_id=akun_id
            )
        return await services.laporan_produk_terlaris_erp(
            session, dari, sampai, limit, platform=platform, akun_ids=allowed
        )
    return await services.laporan_produk_terlaris_erp(session, dari, sampai, limit, platform=platform, akun_id=akun_id)


@erp_router.get("/admin/laporan/ringkasan-status")
async def admin_laporan_ringkasan_status_erp(
    platform: str | None = None,
    akun_id: str | None = None,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    allowed = await akun_ids_diizinkan(_user, session)
    if allowed is not None:
        if akun_id is not None:
            await pastikan_akses_akun(_user, session, akun_id)
            return await services.laporan_ringkasan_status_erp(session, platform=platform, akun_id=akun_id)
        return await services.laporan_ringkasan_status_erp(session, platform=platform, akun_ids=allowed)
    return await services.laporan_ringkasan_status_erp(session, platform=platform, akun_id=akun_id)


@erp_router.get("/admin/laporan/per-akun")
async def admin_laporan_per_akun(
    dari: date,
    sampai: date,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    allowed = await akun_ids_diizinkan(_user, session)
    return await services.laporan_per_akun(session, dari, sampai, akun_ids=allowed)
