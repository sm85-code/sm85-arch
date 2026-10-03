"""HTTP surface of the store admin sub-tenant.

Mounted in main.py at /api/store/admin. Every route except /auth/login,
/auth/logout and /seed-now requires an admin session (cookie
``store_admin_token``); buyer sessions are rejected by audience/issuer.
"""
from __future__ import annotations

import os
from dataclasses import asdict
import secrets as pysecrets
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.store.modules.store.application import services
from tenants.store.modules.store.application.schemas import (
    ChangePasswordIn,
    KategoriIn,
    LoginRequest,
    PengaturanPatch,
    KurirIn,
    PengirimanIn,
    PesanChatIn,
    ProdukIn,
    FotoUrutanIn,
    ProdukPatch,
    VarianListIn,
    StaffIn,
    StaffPatch,
    StatusPengirimanIn,
    StatusPesananIn,
)
from tenants.store.modules.store.infrastructure.auth import (
    clear_admin_cookie,
    get_current_admin,
    issue_admin_token,
    require_admin_roles,
    set_admin_cookie,
)
from tenants.store.modules.store.infrastructure.database import get_db_store
from tenants.store.modules.store.infrastructure.media_storage import delete_foto, upload_chat_media, upload_produk_photo
from tenants.store.modules.store.infrastructure.models import ADMIN_ROLES_STORE, ROLE_OWNER, AdminStore
from tenants.store.modules.store.infrastructure.seeder import seed_store

store_admin_router = APIRouter()

ADMIN_ROLES = ADMIN_ROLES_STORE
# Managing other admin accounts is the owner's job only.
STAFF_MANAGE_ROLES = (ROLE_OWNER,)

admin_only = require_admin_roles(*ADMIN_ROLES)


def _seed_secret_ok(request: Request) -> bool:
    expected = (os.getenv("STORE_SEED_SECRET") or "").strip()
    provided = (request.headers.get("X-Store-Seed-Secret") or request.headers.get("X-Seed-Secret") or "").strip()
    return bool(expected and provided) and pysecrets.compare_digest(provided, expected)


async def authorize_store_seed(
    request: Request,
    session: AsyncSession = Depends(get_db_store),
) -> AdminStore | None:
    """/seed-now needs the seed secret header OR a logged-in owner -- never
    anonymous access, in any environment."""
    if _seed_secret_ok(request):
        return None
    user = await get_current_admin(request, session)
    if (user.role or "").strip().lower() != ROLE_OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akses ditolak")
    return user


@store_admin_router.get("/seed-now")
async def seed_now(
    session: AsyncSession = Depends(get_db_store),
    _: AdminStore | None = Depends(authorize_store_seed),
):
    try:
        return await seed_store(session)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)) from exc


# --- Auth ----------------------------------------------------------------


@store_admin_router.post("/auth/login")
async def do_login(payload: LoginRequest, response: Response, session: AsyncSession = Depends(get_db_store)):
    user = await services.authenticate_admin(session, payload.email, payload.password)
    set_admin_cookie(response, issue_admin_token(user))
    return services.admin_out(user)


@store_admin_router.post("/auth/logout")
async def do_logout(response: Response):
    clear_admin_cookie(response)
    return {"ok": True}


@store_admin_router.get("/auth/me")
async def me(user: AdminStore = Depends(get_current_admin)):
    return services.admin_out(user)


@store_admin_router.post("/auth/ganti-password")
async def ganti_password(
    payload: ChangePasswordIn,
    session: AsyncSession = Depends(get_db_store),
    user: AdminStore = Depends(get_current_admin),
):
    """Any admin changes their own password; the current one is required."""
    await services.change_admin_password(session, user, payload.current_password, payload.new_password)
    return {"ok": True}


# --- Produk & kategori -----------------------------------------------------


@store_admin_router.get("/produk")
async def list_produk(session: AsyncSession = Depends(get_db_store), _user: AdminStore = Depends(admin_only)):
    return [services.produk_out(p) for p in await services.list_produk(session)]


@store_admin_router.post("/produk")
async def create_produk(
    payload: ProdukIn, session: AsyncSession = Depends(get_db_store), _user: AdminStore = Depends(admin_only)
):
    return services.produk_out(await services.create_produk(session, payload))


@store_admin_router.patch("/produk/{produk_id}")
async def patch_produk(
    produk_id: str,
    payload: ProdukPatch,
    session: AsyncSession = Depends(get_db_store),
    _user: AdminStore = Depends(admin_only),
):
    return services.produk_out(await services.update_produk(session, produk_id, payload))


@store_admin_router.delete("/produk/{produk_id}")
async def remove_produk(
    produk_id: str, session: AsyncSession = Depends(get_db_store), _user: AdminStore = Depends(admin_only)
):
    kunci = await services.delete_produk(session, produk_id)
    # Remove the objects only once the delete is committed, and only if
    # nothing else shares them.
    await session.commit()
    for k in set(kunci):
        if not await services.foto_masih_dipakai(session, k):
            await delete_foto(k)
    return {"ok": True}


@store_admin_router.post("/produk/{produk_id}/foto")
async def upload_foto_produk(
    produk_id: str,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db_store),
    _user: AdminStore = Depends(admin_only),
):
    """Append a photo to the product gallery (max 7). Uploads to the media
    bucket (Cloudflare R2; 501 until the R2_* env vars are set); the first
    photo is the cover."""
    # Check product and gallery size first: a full gallery must not leave an orphan upload behind.
    await services.pastikan_bisa_tambah_foto(session, produk_id)
    foto_key = await upload_produk_photo(await file.read(), file.content_type or "")
    return services.produk_out(await services.tambah_foto(session, produk_id, foto_key))


@store_admin_router.delete("/produk/{produk_id}/foto/{foto_id}")
async def hapus_foto_produk(
    produk_id: str,
    foto_id: str,
    session: AsyncSession = Depends(get_db_store),
    _user: AdminStore = Depends(admin_only),
):
    produk, kunci = await services.hapus_foto(session, produk_id, foto_id)
    hasil = services.produk_out(produk)
    await session.commit()
    if not await services.foto_masih_dipakai(session, kunci):
        await delete_foto(kunci)
    return hasil


@store_admin_router.put("/produk/{produk_id}/foto/urutan")
async def urutkan_foto_produk(
    produk_id: str,
    payload: FotoUrutanIn,
    session: AsyncSession = Depends(get_db_store),
    _user: AdminStore = Depends(admin_only),
):
    return services.produk_out(await services.urutkan_foto(session, produk_id, payload.ids))


@store_admin_router.put("/produk/{produk_id}/varian")
async def ganti_varian_produk(
    produk_id: str,
    payload: VarianListIn,
    session: AsyncSession = Depends(get_db_store),
    _user: AdminStore = Depends(admin_only),
):
    return services.produk_out(await services.ganti_varian(session, produk_id, payload.varian))


@store_admin_router.get("/kategori")
async def list_kategori(session: AsyncSession = Depends(get_db_store), _user: AdminStore = Depends(admin_only)):
    return [services.kategori_out(k) for k in await services.list_kategori(session)]


@store_admin_router.post("/kategori")
async def create_kategori(
    payload: KategoriIn, session: AsyncSession = Depends(get_db_store), _user: AdminStore = Depends(admin_only)
):
    return services.kategori_out(await services.create_kategori(session, payload))


@store_admin_router.delete("/kategori/{kategori_id}")
async def remove_kategori(
    kategori_id: str, session: AsyncSession = Depends(get_db_store), _user: AdminStore = Depends(admin_only)
):
    await services.delete_kategori(session, kategori_id)
    return {"ok": True}


# --- Pesanan & pengiriman --------------------------------------------------


@store_admin_router.get("/pesanan")
async def list_pesanan(session: AsyncSession = Depends(get_db_store), _user: AdminStore = Depends(admin_only)):
    return [services.pesanan_out(p) for p in await services.list_semua_pesanan(session)]


@store_admin_router.get("/pesanan/{pesanan_id}")
async def get_pesanan(
    pesanan_id: str, session: AsyncSession = Depends(get_db_store), _user: AdminStore = Depends(admin_only)
):
    return services.pesanan_out(await services.get_pesanan(session, pesanan_id))


@store_admin_router.patch("/pesanan/{pesanan_id}/status")
async def ubah_status_pesanan(
    pesanan_id: str,
    payload: StatusPesananIn,
    session: AsyncSession = Depends(get_db_store),
    _user: AdminStore = Depends(admin_only),
):
    return services.pesanan_out(await services.ubah_status_pesanan(session, pesanan_id, payload.status))


@store_admin_router.post("/pesanan/{pesanan_id}/pengiriman")
async def buat_pengiriman(
    pesanan_id: str,
    payload: PengirimanIn,
    session: AsyncSession = Depends(get_db_store),
    _user: AdminStore = Depends(admin_only),
):
    return services.pengiriman_out(await services.buat_pengiriman_lokal(session, pesanan_id, payload))


@store_admin_router.get("/pesanan/{pesanan_id}/pengiriman")
async def get_pengiriman(
    pesanan_id: str, session: AsyncSession = Depends(get_db_store), _user: AdminStore = Depends(admin_only)
):
    return services.pengiriman_out(await services.get_pengiriman(session, pesanan_id))


@store_admin_router.get("/pesanan/{pesanan_id}/pengiriman/opsi-kurir")
async def opsi_kurir(
    pesanan_id: str, session: AsyncSession = Depends(get_db_store), _user: AdminStore = Depends(admin_only)
):
    return [asdict(o) for o in await services.opsi_kurir_pesanan(session, pesanan_id)]


@store_admin_router.put("/pesanan/{pesanan_id}/pengiriman/kurir")
async def ganti_kurir(
    pesanan_id: str,
    payload: KurirIn,
    session: AsyncSession = Depends(get_db_store),
    _user: AdminStore = Depends(admin_only),
):
    return services.pengiriman_out(await services.ganti_kurir(session, pesanan_id, payload.kurir, payload.layanan))


@store_admin_router.post("/pesanan/{pesanan_id}/pengiriman/biteship")
async def buat_pengiriman_biteship(
    pesanan_id: str, session: AsyncSession = Depends(get_db_store), _user: AdminStore = Depends(admin_only)
):
    return services.pengiriman_out(await services.buat_order_biteship(session, pesanan_id))


@store_admin_router.get("/pesanan/{pesanan_id}/pengiriman/lacak")
async def lacak_pengiriman(
    pesanan_id: str, session: AsyncSession = Depends(get_db_store), _user: AdminStore = Depends(admin_only)
):
    return await services.lacak_pengiriman(session, pesanan_id)


@store_admin_router.patch("/pesanan/{pesanan_id}/pengiriman/status")
async def ubah_status_pengiriman(
    pesanan_id: str,
    payload: StatusPengirimanIn,
    session: AsyncSession = Depends(get_db_store),
    _user: AdminStore = Depends(admin_only),
):
    pengiriman = await services.ubah_status_pengiriman(session, pesanan_id, payload.status, payload.tracking_id)
    return services.pengiriman_out(pengiriman)


# --- Laporan ---------------------------------------------------------------


@store_admin_router.get("/laporan/penjualan")
async def laporan_penjualan(
    dari: date,
    sampai: date,
    session: AsyncSession = Depends(get_db_store),
    _user: AdminStore = Depends(admin_only),
):
    return await services.laporan_penjualan(session, dari, sampai)


@store_admin_router.get("/laporan/produk-terlaris")
async def laporan_produk_terlaris(
    dari: date,
    sampai: date,
    limit: int = 10,
    session: AsyncSession = Depends(get_db_store),
    _user: AdminStore = Depends(admin_only),
):
    return await services.laporan_produk_terlaris(session, dari, sampai, limit)


@store_admin_router.get("/laporan/ringkasan-status")
async def laporan_ringkasan_status(
    session: AsyncSession = Depends(get_db_store), _user: AdminStore = Depends(admin_only)
):
    return await services.laporan_ringkasan_status(session)


# --- Chat ------------------------------------------------------------------


@store_admin_router.get("/chat")
async def list_percakapan(session: AsyncSession = Depends(get_db_store), _user: AdminStore = Depends(admin_only)):
    return [services.percakapan_out(p) for p in await services.list_percakapan_admin(session)]


@store_admin_router.get("/chat/{percakapan_id}")
async def get_percakapan(
    percakapan_id: str, session: AsyncSession = Depends(get_db_store), _user: AdminStore = Depends(admin_only)
):
    percakapan = await services.get_percakapan(session, percakapan_id)
    await services.tandai_dibaca(session, percakapan_id, sebagai_admin=True)
    return services.percakapan_out(percakapan, dengan_pesan=True)


@store_admin_router.post("/chat/{percakapan_id}")
async def kirim_pesan(
    percakapan_id: str,
    payload: PesanChatIn,
    session: AsyncSession = Depends(get_db_store),
    user: AdminStore = Depends(admin_only),
):
    await services.kirim_pesan(
        session, percakapan_id, user.id, payload.isi, sebagai_admin=True, produk_id=payload.produk_id
    )
    percakapan = await services.get_percakapan(session, percakapan_id)
    return services.percakapan_out(percakapan, dengan_pesan=True)


@store_admin_router.post("/chat/{percakapan_id}/lampiran")
async def kirim_lampiran(
    percakapan_id: str,
    file: UploadFile = File(...),
    isi: str = Form("", max_length=2000),
    session: AsyncSession = Depends(get_db_store),
    user: AdminStore = Depends(admin_only),
):
    """Send a photo or short video (optionally with a caption) to a buyer."""
    await services.get_percakapan(session, percakapan_id)  # unknown thread: 404 before anything is uploaded
    key, jenis = await upload_chat_media(await file.read(), file.content_type or "")
    await services.kirim_pesan(
        session, percakapan_id, user.id, isi.strip(), sebagai_admin=True, lampiran_key=key, lampiran_jenis=jenis
    )
    percakapan = await services.get_percakapan(session, percakapan_id)
    return services.percakapan_out(percakapan, dengan_pesan=True)


# --- Pengaturan ------------------------------------------------------------


@store_admin_router.get("/pengaturan")
async def get_pengaturan(session: AsyncSession = Depends(get_db_store), _user: AdminStore = Depends(admin_only)):
    return services.pengaturan_out(await services.get_or_create_pengaturan(session))


@store_admin_router.patch("/pengaturan")
async def patch_pengaturan(
    payload: PengaturanPatch, session: AsyncSession = Depends(get_db_store), _user: AdminStore = Depends(admin_only)
):
    return services.pengaturan_out(await services.update_pengaturan(session, payload))


# --- Staff (owner only) ----------------------------------------------------


@store_admin_router.get("/staff")
async def list_staff(
    session: AsyncSession = Depends(get_db_store),
    _user: AdminStore = Depends(require_admin_roles(*STAFF_MANAGE_ROLES)),
):
    return [services.staff_out(u) for u in await services.list_staff(session)]


@store_admin_router.post("/staff")
async def create_staff(
    payload: StaffIn,
    session: AsyncSession = Depends(get_db_store),
    _user: AdminStore = Depends(require_admin_roles(*STAFF_MANAGE_ROLES)),
):
    return services.staff_out(await services.create_staff(session, payload))


@store_admin_router.patch("/staff/{staff_id}")
async def patch_staff(
    staff_id: str,
    payload: StaffPatch,
    session: AsyncSession = Depends(get_db_store),
    _user: AdminStore = Depends(require_admin_roles(*STAFF_MANAGE_ROLES)),
):
    return services.staff_out(await services.update_staff(session, staff_id, payload))


@store_admin_router.delete("/staff/{staff_id}")
async def remove_staff(
    staff_id: str,
    session: AsyncSession = Depends(get_db_store),
    _user: AdminStore = Depends(require_admin_roles(*STAFF_MANAGE_ROLES)),
):
    await services.delete_staff(session, staff_id)
    return {"ok": True}
