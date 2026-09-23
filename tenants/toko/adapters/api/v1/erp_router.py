"""HTTP surface for the toko-erp (marketplace ERP) submodule.

Mounted in main.py as prefix=/api/toko/marketplace -- a separate prefix from
/api/toko so nothing here overlaps or changes any existing toko-web route.
Covers Shopee/Lazada/Blibli order & chat inbox management plus the
ERP-catalog-to-web-catalog one-way copy. See
tenants/toko/modules/erp/infrastructure/erp_shopee.py, erp_lazada.py and
erp_blibli.py for the (not yet connected) platform sync adapters this
module will eventually call.

Auth: reuses require_roles_toko from the toko web module -- no separate
auth system for this submodule, same admin_toko/owner roles as the rest of
the toko admin surface.
"""
from __future__ import annotations

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
from tenants.toko.modules.toko.infrastructure.auth import require_roles_toko
from tenants.toko.modules.toko.infrastructure.database import get_db_toko
from tenants.toko.modules.toko.infrastructure.models import UserToko

erp_router = APIRouter()

ADMIN_ROLES = ("admin_toko", "owner")


# --- Akun Marketplace ---------------------------------------------------


@erp_router.get("/admin/akun")
async def admin_list_akun_marketplace(
    platform: str | None = None,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    akun = await services.list_akun_marketplace(session, platform=platform)
    return [services.akun_out(a) for a in akun]


@erp_router.post("/admin/akun")
async def admin_create_akun_marketplace(
    payload: AkunMarketplaceIn,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    akun = await services.create_akun_marketplace(session, payload)
    return services.akun_out(akun)


@erp_router.get("/admin/akun/{akun_id}")
async def admin_get_akun_marketplace(
    akun_id: str,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    akun = await services.get_akun_marketplace(session, akun_id)
    return services.akun_out(akun)


@erp_router.patch("/admin/akun/{akun_id}")
async def admin_patch_akun_marketplace(
    akun_id: str,
    payload: AkunMarketplacePatch,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    akun = await services.update_akun_marketplace(session, akun_id, payload)
    return services.akun_out(akun)


@erp_router.delete("/admin/akun/{akun_id}")
async def admin_delete_akun_marketplace(
    akun_id: str,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
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
    produk = await services.list_produk_erp(session, platform=platform, akun_id=akun_id)
    return [services.produk_erp_out(p) for p in produk]


@erp_router.get("/admin/erp/produk/{produk_id}")
async def admin_get_produk_erp(
    produk_id: str,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    produk = await services.get_produk_erp(session, produk_id)
    return services.produk_erp_out(produk)


@erp_router.post("/admin/erp/produk")
async def admin_create_produk_erp(
    payload: ProdukERPIn,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    produk = await services.create_produk_erp(session, payload)
    return services.produk_erp_out(produk)


@erp_router.patch("/admin/erp/produk/{produk_id}")
async def admin_patch_produk_erp(
    produk_id: str,
    payload: ProdukERPPatch,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    produk = await services.update_produk_erp(session, produk_id, payload)
    return services.produk_erp_out(produk)


@erp_router.delete("/admin/erp/produk/{produk_id}")
async def admin_delete_produk_erp(
    produk_id: str,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
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
    pesanan = await services.list_pesanan_erp(session, platform=platform, akun_id=akun_id)
    return [services.pesanan_erp_out(p) for p in pesanan]


@erp_router.get("/admin/erp/pesanan/{pesanan_id}")
async def admin_get_pesanan_erp(
    pesanan_id: str,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    pesanan = await services.get_pesanan_erp(session, pesanan_id)
    return services.pesanan_erp_out(pesanan)


@erp_router.patch("/admin/erp/pesanan/{pesanan_id}/status")
async def admin_ubah_status_pesanan_erp(
    pesanan_id: str,
    payload: StatusPesananERPIn,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
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
    percakapan = await services.list_percakapan_erp(session, platform=platform, akun_id=akun_id)
    return [services.percakapan_erp_out(p) for p in percakapan]


@erp_router.post("/admin/erp/chat")
async def admin_buka_percakapan_erp(
    payload: PercakapanERPIn,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    percakapan = await services.get_or_create_percakapan_erp(session, payload)
    return services.percakapan_erp_out(percakapan)


@erp_router.get("/admin/erp/chat/{percakapan_id}")
async def admin_get_percakapan_erp(
    percakapan_id: str,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    percakapan = await services.get_percakapan_erp(session, percakapan_id)
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
    await services.kirim_pesan_erp(session, percakapan_id, isi=payload.isi, pengirim_admin=True)
    percakapan = await services.get_percakapan_erp(session, percakapan_id)
    return services.percakapan_erp_out(percakapan, dengan_pesan=True)
