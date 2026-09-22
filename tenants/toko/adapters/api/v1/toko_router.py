"""HTTP surface for the isolated toko (online shop) module.

Mounted in main.py as prefix=/api/toko only. Does not touch BUMDes or
madrasah routers. Foundation scope: user/auth + product catalog. Cart,
orders, payment, shipping, and reporting land in follow-up work.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.toko.modules.toko.application import services
from tenants.toko.modules.toko.application.schemas import (
    AlamatIn,
    AlamatPatch,
    CekOngkirIn,
    GoogleLoginRequest,
    KategoriIn,
    KeranjangItemIn,
    KeranjangItemPatch,
    LoginRequest,
    PengirimanIn,
    PesanChatIn,
    ProdukIn,
    ProdukPatch,
    RegisterRequest,
    StatusPengirimanIn,
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
from tenants.toko.modules.toko.infrastructure.google_auth import verify_google_id_token
from tenants.toko.modules.toko.infrastructure.image_upload import upload_produk_photo
from tenants.toko.modules.toko.infrastructure.models import UserToko
from tenants.toko.modules.toko.infrastructure.payment_ipaymu import create_payment as ipaymu_create_payment
from tenants.toko.modules.toko.infrastructure.payment_ipaymu import parse_webhook as ipaymu_parse_webhook
from tenants.toko.modules.toko.infrastructure.seeder import seed_toko
from tenants.toko.modules.toko.infrastructure.shipping_biteship import cek_ongkir as biteship_cek_ongkir
from tenants.toko.modules.toko.infrastructure.shipping_biteship import parse_webhook as biteship_parse_webhook

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


@toko_router.post("/auth/google")
async def do_login_google(payload: GoogleLoginRequest, response: Response, session: AsyncSession = Depends(get_db_toko)):
    claims = verify_google_id_token(payload.id_token)
    user = await services.login_or_register_google(session, google_sub=claims["sub"], email=claims["email"], nama=claims["name"])
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


@toko_router.post("/admin/produk/{produk_id}/foto")
async def upload_foto_produk(
    produk_id: str,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    """Upload foto ke Google Drive (lihat infrastructure/image_upload.py --
    butuh GDRIVE_FOLDER_ID_TOKO + kredensial service account terisi) lalu
    simpan URL-nya ke produk.foto_url."""
    file_bytes = await file.read()
    foto_url = await upload_produk_photo(file_bytes, file.filename or produk_id, file.content_type or "")
    produk = await services.update_produk(session, produk_id, ProdukPatch(foto_url=foto_url))
    return services.produk_out(produk)


# --- Kategori --------------------------------------------------------------


@toko_router.get("/kategori")
async def get_kategori_list(session: AsyncSession = Depends(get_db_toko)):
    kategori = await services.list_kategori(session)
    return [services.kategori_out(k) for k in kategori]


@toko_router.post("/admin/kategori")
async def admin_create_kategori(
    payload: KategoriIn,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    kategori = await services.create_kategori(session, payload)
    return services.kategori_out(kategori)


@toko_router.delete("/admin/kategori/{kategori_id}")
async def admin_delete_kategori(
    kategori_id: str,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    await services.delete_kategori(session, kategori_id)
    return {"ok": True}


# --- Alamat (buku alamat pembeli) ------------------------------------------


@toko_router.get("/alamat")
async def get_alamat_list(
    session: AsyncSession = Depends(get_db_toko),
    user: UserToko = Depends(get_current_user_toko),
):
    alamat = await services.list_alamat(session, user.id)
    return [services.alamat_out(a) for a in alamat]


@toko_router.post("/alamat")
async def create_alamat(
    payload: AlamatIn,
    session: AsyncSession = Depends(get_db_toko),
    user: UserToko = Depends(get_current_user_toko),
):
    alamat = await services.create_alamat(session, user.id, payload)
    return services.alamat_out(alamat)


@toko_router.patch("/alamat/{alamat_id}")
async def patch_alamat(
    alamat_id: str,
    payload: AlamatPatch,
    session: AsyncSession = Depends(get_db_toko),
    user: UserToko = Depends(get_current_user_toko),
):
    alamat = await services.update_alamat(session, user.id, alamat_id, payload)
    return services.alamat_out(alamat)


@toko_router.delete("/alamat/{alamat_id}")
async def remove_alamat(
    alamat_id: str,
    session: AsyncSession = Depends(get_db_toko),
    user: UserToko = Depends(get_current_user_toko),
):
    await services.delete_alamat(session, user.id, alamat_id)
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


# --- Pembayaran (iPaymu) -----------------------------------------------
# Lihat infrastructure/payment_ipaymu.py -- BELUM terhubung ke API asli,
# menunggu verifikasi merchant selesai. Endpoint di bawah ini sudah
# terstruktur lengkap (validasi pesanan, catat metode di DB) supaya begitu
# adapter-nya diisi, cuma bagian pemanggilan API yang perlu ditambah.


@toko_router.post("/pesanan/{pesanan_id}/bayar")
async def mulai_pembayaran(
    pesanan_id: str,
    session: AsyncSession = Depends(get_db_toko),
    user: UserToko = Depends(get_current_user_toko),
):
    pesanan = await services.get_pesanan(session, pesanan_id)
    if pesanan.user_id != user.id:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Pesanan tidak ditemukan")
    if pesanan.status != "menunggu_pembayaran":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"Pesanan berstatus '{pesanan.status}', tidak bisa dibayar ulang",
        )

    result = await ipaymu_create_payment(
        pesanan_id=pesanan.id,
        total=str(pesanan.total),
        nama_pembeli=user.nama,
        email_pembeli=user.email,
        notify_url="/api/toko/payment/callback",
        return_url=f"/pesanan/{pesanan.id}",
    )
    await services.catat_metode_pembayaran(session, pesanan.id, metode="gateway", gateway_ref=result.gateway_ref)
    return {"checkout_url": result.checkout_url}


@toko_router.post("/payment/callback")
async def payment_callback(request: dict, session: AsyncSession = Depends(get_db_toko)):
    """Webhook dari iPaymu. Bentuk payload BELUM diverifikasi -- lihat
    infrastructure/payment_ipaymu.py::parse_webhook."""
    parsed = ipaymu_parse_webhook(request)
    if not parsed.get("gateway_ref"):
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Payload webhook tidak dikenal")
    pesanan = await services.tandai_dibayar_dari_webhook(session, parsed["gateway_ref"])
    if not pesanan:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Pesanan untuk gateway_ref ini tidak ditemukan")
    return {"ok": True}


# --- Pengiriman (Biteship) ----------------------------------------------
# Lihat infrastructure/shipping_biteship.py -- BELUM terhubung ke API asli.


@toko_router.post("/pengiriman/cek-ongkir")
async def cek_ongkir(payload: CekOngkirIn):
    options = await biteship_cek_ongkir(
        kode_pos_asal=payload.kode_pos_asal,
        kode_pos_tujuan=payload.kode_pos_tujuan,
        berat_gram=payload.berat_gram,
        nilai_barang=str(payload.nilai_barang),
    )
    return options


@toko_router.post("/admin/pesanan/{pesanan_id}/pengiriman")
async def admin_buat_pengiriman(
    pesanan_id: str,
    payload: PengirimanIn,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    pengiriman = await services.buat_pengiriman_lokal(session, pesanan_id, payload)
    return services.pengiriman_out(pengiriman)


@toko_router.post("/pesanan/{pesanan_id}/pengiriman")
async def isi_alamat_pengiriman(
    pesanan_id: str,
    payload: PengirimanIn,
    session: AsyncSession = Depends(get_db_toko),
    user: UserToko = Depends(get_current_user_toko),
):
    """Pembeli mengisi alamat tujuan untuk pesanannya sendiri (beda dari
    /admin/pesanan/{id}/pengiriman yang dipakai admin, misalnya untuk
    pesanan yang masuk lewat telepon/WhatsApp)."""
    pesanan = await services.get_pesanan(session, pesanan_id)
    if pesanan.user_id != user.id:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Pesanan tidak ditemukan")
    pengiriman = await services.buat_pengiriman_lokal(session, pesanan_id, payload)
    return services.pengiriman_out(pengiriman)


@toko_router.get("/pesanan/{pesanan_id}/pengiriman")
async def get_pengiriman(
    pesanan_id: str,
    session: AsyncSession = Depends(get_db_toko),
    user: UserToko = Depends(get_current_user_toko),
):
    pesanan = await services.get_pesanan(session, pesanan_id)
    if pesanan.user_id != user.id and (user.role or "").lower() not in ADMIN_ROLES:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Pesanan tidak ditemukan")
    pengiriman = await services.get_pengiriman(session, pesanan_id)
    return services.pengiriman_out(pengiriman)


@toko_router.patch("/admin/pesanan/{pesanan_id}/pengiriman/status")
async def admin_ubah_status_pengiriman(
    pesanan_id: str,
    payload: StatusPengirimanIn,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    pengiriman = await services.ubah_status_pengiriman(session, pesanan_id, payload.status, payload.tracking_id)
    return services.pengiriman_out(pengiriman)


@toko_router.post("/pengiriman/callback")
async def pengiriman_callback(payload: dict, session: AsyncSession = Depends(get_db_toko)):
    """Webhook dari Biteship. Bentuk payload BELUM diverifikasi -- lihat
    infrastructure/shipping_biteship.py::parse_webhook."""
    parsed = biteship_parse_webhook(payload)
    if not parsed.get("order_id"):
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Payload webhook tidak dikenal")
    return {"ok": True, "note": "Pemetaan order_id Biteship -> pesanan_id belum diimplementasi"}


# --- Laporan Penjualan ---------------------------------------------------


@toko_router.get("/admin/laporan/penjualan")
async def admin_laporan_penjualan(
    dari: date,
    sampai: date,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    return await services.laporan_penjualan(session, dari, sampai)


@toko_router.get("/admin/laporan/produk-terlaris")
async def admin_laporan_produk_terlaris(
    dari: date,
    sampai: date,
    limit: int = 10,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    return await services.laporan_produk_terlaris(session, dari, sampai, limit)


@toko_router.get("/admin/laporan/ringkasan-status")
async def admin_laporan_ringkasan_status(
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    return await services.laporan_ringkasan_status(session)


# --- Chat toko web (terpisah dari chat Shopee, yang belum dibangun) --------


@toko_router.get("/chat")
async def get_percakapan_saya(
    session: AsyncSession = Depends(get_db_toko),
    user: UserToko = Depends(get_current_user_toko),
):
    percakapan = await services.get_or_create_percakapan(session, user.id)
    percakapan = await services.get_percakapan(session, percakapan.id)
    await services.tandai_dibaca(session, percakapan.id, sebagai_admin=False)
    return services.percakapan_out(percakapan, dengan_pesan=True)


@toko_router.post("/chat")
async def kirim_pesan_saya(
    payload: PesanChatIn,
    session: AsyncSession = Depends(get_db_toko),
    user: UserToko = Depends(get_current_user_toko),
):
    percakapan = await services.get_or_create_percakapan(session, user.id)
    await services.kirim_pesan(session, percakapan.id, user, payload.isi)
    percakapan = await services.get_percakapan(session, percakapan.id)
    return services.percakapan_out(percakapan, dengan_pesan=True)


@toko_router.get("/admin/chat")
async def admin_list_percakapan(
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    percakapan = await services.list_percakapan_admin(session)
    return [services.percakapan_out(p) for p in percakapan]


@toko_router.get("/admin/chat/{percakapan_id}")
async def admin_get_percakapan(
    percakapan_id: str,
    session: AsyncSession = Depends(get_db_toko),
    _user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    percakapan = await services.get_percakapan(session, percakapan_id)
    await services.tandai_dibaca(session, percakapan_id, sebagai_admin=True)
    return services.percakapan_out(percakapan, dengan_pesan=True)


@toko_router.post("/admin/chat/{percakapan_id}")
async def admin_kirim_pesan(
    percakapan_id: str,
    payload: PesanChatIn,
    session: AsyncSession = Depends(get_db_toko),
    user: UserToko = Depends(require_roles_toko(*ADMIN_ROLES)),
):
    await services.kirim_pesan(session, percakapan_id, user, payload.isi)
    percakapan = await services.get_percakapan(session, percakapan_id)
    return services.percakapan_out(percakapan, dengan_pesan=True)
