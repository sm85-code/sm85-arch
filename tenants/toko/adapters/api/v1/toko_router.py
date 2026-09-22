"""HTTP surface for the isolated toko (online shop) module.

Mounted in main.py as prefix=/api/toko only. Does not touch BUMDes or
madrasah routers. Foundation scope: user/auth + product catalog. Cart,
orders, payment, shipping, and reporting land in follow-up work.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.toko.modules.toko.application import services
from tenants.toko.modules.toko.application.schemas import (
    KeranjangItemIn,
    KeranjangItemPatch,
    LoginRequest,
    ProdukIn,
    ProdukPatch,
    RegisterRequest,
    StatusPesananIn,
)
from tenants.toko.modules.toko.infrastructure.auth import (
    clear_toko_cookie,
    get_current_user_toko,
    issue_toko_token,
    require_roles_toko,
    set_toko_cookie,
)
from tenants.toko.modules.toko.infrastructure.database import get_db_toko
from tenants.toko.modules.toko.infrastructure.models import UserToko
from tenants.toko.modules.toko.infrastructure.seeder import seed_toko

toko_router = APIRouter()

ADMIN_ROLES = ("admin_toko", "owner")


@toko_router.get("/seed-now")
async def seed_now(session: AsyncSession = Depends(get_db_toko)):
    return await seed_toko(session)


@toko_router.post("/auth/register")
async def do_register(payload: RegisterRequest, response: Response, session: AsyncSession = Depends(get_db_toko)):
    user = await services.register(session, payload)
    token = issue_toko_token(user)
    set_toko_cookie(response, token)
    return services.user_out(user)


@toko_router.post("/auth/login")
async def do_login(payload: LoginRequest, response: Response, session: AsyncSession = Depends(get_db_toko)):
    user = await services.authenticate(session, payload.email, payload.password)
    token = issue_toko_token(user)
    set_toko_cookie(response, token)
    return services.user_out(user)


@toko_router.post("/auth/logout")
async def do_logout(response: Response):
    clear_toko_cookie(response)
    return {"ok": True}


@toko_router.get("/auth/me")
async def me(user: UserToko = Depends(get_current_user_toko)):
    return services.user_out(user)


@toko_router.get("/produk")
async def get_produk_list(session: AsyncSession = Depends(get_db_toko)):
    produk = await services.list_produk(session, hanya_aktif=True)
    return [services.produk_out(p) for p in produk]


@toko_router.get("/produk/{produk_id}")
async def get_produk_detail(produk_id: str, session: AsyncSession = Depends(get_db_toko)):
    produk = await services.get_produk(session, produk_id)
    return services.produk_out(produk)


@toko_router.post("/admin/produk")
async def create_produk(
    payload: ProdukIn,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    produk = await services.create_produk(session, payload)
    return services.produk_out(produk)


@toko_router.get("/admin/produk")
async def admin_list_produk(
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    produk = await services.list_produk(session)
    return [services.produk_out(p) for p in produk]


@toko_router.patch("/admin/produk/{produk_id}")
async def patch_produk(
    produk_id: str,
    payload: ProdukPatch,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    produk = await services.update_produk(session, produk_id, payload)
    return services.produk_out(produk)


@toko_router.delete("/admin/produk/{produk_id}")
async def remove_produk(
    produk_id: str,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    await services.delete_produk(session, produk_id)
    return {"ok": True}


@toko_router.get("/keranjang")
async def get_keranjang(
    session: AsyncSession = Depends(get_db_toko),
    user: UserToko = Depends(get_current_user_toko),
):
    items = await services.get_keranjang(session, user.id)
    return [services.keranjang_item_out(it) for it in items]


@toko_router.post("/keranjang")
async def add_to_keranjang(
    payload: KeranjangItemIn,
    session: AsyncSession = Depends(get_db_toko),
    user: UserToko = Depends(get_current_user_toko),
):
    item = await services.tambah_ke_keranjang(session, user.id, payload.produk_id, payload.qty)
    return services.keranjang_item_out(item)


@toko_router.patch("/keranjang/{produk_id}")
async def patch_keranjang(
    produk_id: str,
    payload: KeranjangItemPatch,
    session: AsyncSession = Depends(get_db_toko),
    user: UserToko = Depends(get_current_user_toko),
):
    item = await services.ubah_qty_keranjang(session, user.id, produk_id, payload.qty)
    return services.keranjang_item_out(item)


@toko_router.delete("/keranjang/{produk_id}")
async def remove_from_keranjang(
    produk_id: str,
    session: AsyncSession = Depends(get_db_toko),
    user: UserToko = Depends(get_current_user_toko),
):
    await services.hapus_dari_keranjang(session, user.id, produk_id)
    return {"ok": True}


@toko_router.post("/pesanan/checkout")
async def checkout(
    session: AsyncSession = Depends(get_db_toko),
    user: UserToko = Depends(get_current_user_toko),
):
    pesanan = await services.checkout(session, user.id)
    return services.pesanan_out(pesanan)


@toko_router.get("/pesanan")
async def list_pesanan(
    session: AsyncSession = Depends(get_db_toko),
    user: UserToko = Depends(get_current_user_toko),
):
    pesanan = await services.list_pesanan_milik(session, user.id)
    return [services.pesanan_out(p) for p in pesanan]


@toko_router.get("/pesanan/{pesanan_id}")
async def get_pesanan_detail(
    pesanan_id: str,
    session: AsyncSession = Depends(get_db_toko),
    user: UserToko = Depends(get_current_user_toko),
):
    pesanan = await services.get_pesanan(session, pesanan_id)
    if pesanan.user_id != user.id and (user.role or "").lower() not in ADMIN_ROLES:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Pesanan tidak ditemukan")
    return services.pesanan_out(pesanan)


@toko_router.get("/admin/pesanan")
async def admin_list_pesanan(
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    pesanan = await services.list_semua_pesanan(session)
    return [services.pesanan_out(p) for p in pesanan]


@toko_router.patch("/admin/pesanan/{pesanan_id}/status")
async def admin_ubah_status_pesanan(
    pesanan_id: str,
    payload: StatusPesananIn,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    pesanan = await services.ubah_status_pesanan(session, pesanan_id, payload.status)
    return services.pesanan_out(pesanan)
