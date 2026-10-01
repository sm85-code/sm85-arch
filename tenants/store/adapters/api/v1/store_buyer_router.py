"""HTTP surface of the store buyer (storefront) sub-tenant.

Mounted in main.py at /api/store/buyer. Public: catalog + auth endpoints.
Everything else requires a buyer session (cookie ``store_buyer_token``);
admin sessions are rejected by audience/issuer, and a buyer can only ever
read or change their own cart, addresses, orders and chat.
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.store.modules.store.application import services
from tenants.store.modules.store.application.schemas import (
    AlamatIn,
    AlamatPatch,
    CekOngkirIn,
    GoogleLoginRequest,
    KeranjangItemIn,
    KeranjangItemPatch,
    LoginRequest,
    PengirimanIn,
    PesanChatIn,
    RegisterRequest,
)
from tenants.store.modules.store.infrastructure.auth import (
    clear_buyer_cookie,
    get_current_buyer,
    issue_buyer_token,
    set_buyer_cookie,
)
from tenants.store.modules.store.infrastructure.database import get_db_store
from tenants.store.modules.store.infrastructure.google_auth import verify_google_id_token
from tenants.store.modules.store.infrastructure.models import PembeliStore
from tenants.store.modules.store.infrastructure.payment_ipaymu import create_payment as ipaymu_create_payment
from tenants.store.modules.store.infrastructure.payment_ipaymu import verify_webhook as ipaymu_verify_webhook
from tenants.store.modules.store.infrastructure.shipping_biteship import cek_ongkir as biteship_cek_ongkir

store_buyer_router = APIRouter()


# --- Auth ----------------------------------------------------------------


@store_buyer_router.post("/auth/register")
async def do_register(payload: RegisterRequest, response: Response, session: AsyncSession = Depends(get_db_store)):
    user = await services.register(session, payload)
    set_buyer_cookie(response, issue_buyer_token(user))
    return services.pembeli_out(user)


@store_buyer_router.post("/auth/login")
async def do_login(payload: LoginRequest, response: Response, session: AsyncSession = Depends(get_db_store)):
    user = await services.authenticate_buyer(session, payload.email, payload.password)
    set_buyer_cookie(response, issue_buyer_token(user))
    return services.pembeli_out(user)


@store_buyer_router.post("/auth/google")
async def do_login_google(
    payload: GoogleLoginRequest, response: Response, session: AsyncSession = Depends(get_db_store)
):
    claims = verify_google_id_token(payload.id_token)
    user = await services.login_or_register_google(
        session, google_sub=claims["sub"], email=claims["email"], nama=claims["name"]
    )
    set_buyer_cookie(response, issue_buyer_token(user))
    return services.pembeli_out(user)


@store_buyer_router.post("/auth/logout")
async def do_logout(response: Response):
    clear_buyer_cookie(response)
    return {"ok": True}


@store_buyer_router.get("/auth/me")
async def me(user: PembeliStore = Depends(get_current_buyer)):
    return services.pembeli_out(user)


# --- Katalog (public) ------------------------------------------------------


@store_buyer_router.get("/produk")
async def list_produk(session: AsyncSession = Depends(get_db_store)):
    return [services.produk_out(p) for p in await services.list_produk_publik(session)]


@store_buyer_router.get("/produk/{ref}")
async def get_produk(ref: str, session: AsyncSession = Depends(get_db_store)):
    """ref is the product slug (SEO URL); the plain id is still accepted for old links."""
    produk = await services.get_produk_by_ref(session, ref)
    if not produk.aktif:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Produk tidak ditemukan")
    return services.produk_out(produk)


@store_buyer_router.get("/kategori")
async def list_kategori(session: AsyncSession = Depends(get_db_store)):
    return [services.kategori_out(k) for k in await services.list_kategori(session)]


# --- Alamat ----------------------------------------------------------------


@store_buyer_router.get("/alamat")
async def list_alamat(session: AsyncSession = Depends(get_db_store), user: PembeliStore = Depends(get_current_buyer)):
    return [services.alamat_out(a) for a in await services.list_alamat(session, user.id)]


@store_buyer_router.post("/alamat")
async def create_alamat(
    payload: AlamatIn, session: AsyncSession = Depends(get_db_store), user: PembeliStore = Depends(get_current_buyer)
):
    return services.alamat_out(await services.create_alamat(session, user.id, payload))


@store_buyer_router.patch("/alamat/{alamat_id}")
async def patch_alamat(
    alamat_id: str,
    payload: AlamatPatch,
    session: AsyncSession = Depends(get_db_store),
    user: PembeliStore = Depends(get_current_buyer),
):
    return services.alamat_out(await services.update_alamat(session, user.id, alamat_id, payload))


@store_buyer_router.delete("/alamat/{alamat_id}")
async def remove_alamat(
    alamat_id: str, session: AsyncSession = Depends(get_db_store), user: PembeliStore = Depends(get_current_buyer)
):
    await services.delete_alamat(session, user.id, alamat_id)
    return {"ok": True}


# --- Keranjang -------------------------------------------------------------


@store_buyer_router.get("/keranjang")
async def get_keranjang(session: AsyncSession = Depends(get_db_store), user: PembeliStore = Depends(get_current_buyer)):
    return [services.keranjang_item_out(it) for it in await services.get_keranjang(session, user.id)]


@store_buyer_router.post("/keranjang")
async def add_to_keranjang(
    payload: KeranjangItemIn,
    session: AsyncSession = Depends(get_db_store),
    user: PembeliStore = Depends(get_current_buyer),
):
    item = await services.tambah_ke_keranjang(session, user.id, payload.produk_id, payload.qty)
    return services.keranjang_item_out(item)


@store_buyer_router.patch("/keranjang/{produk_id}")
async def patch_keranjang(
    produk_id: str,
    payload: KeranjangItemPatch,
    session: AsyncSession = Depends(get_db_store),
    user: PembeliStore = Depends(get_current_buyer),
):
    item = await services.ubah_qty_keranjang(session, user.id, produk_id, payload.qty)
    return services.keranjang_item_out(item)


@store_buyer_router.delete("/keranjang/{produk_id}")
async def remove_from_keranjang(
    produk_id: str, session: AsyncSession = Depends(get_db_store), user: PembeliStore = Depends(get_current_buyer)
):
    await services.hapus_dari_keranjang(session, user.id, produk_id)
    return {"ok": True}


# --- Pesanan ---------------------------------------------------------------


async def _pesanan_milik(session: AsyncSession, pesanan_id: str, user: PembeliStore):
    pesanan = await services.get_pesanan(session, pesanan_id)
    if pesanan.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pesanan tidak ditemukan")
    return pesanan


@store_buyer_router.post("/pesanan/checkout")
async def checkout(session: AsyncSession = Depends(get_db_store), user: PembeliStore = Depends(get_current_buyer)):
    return services.pesanan_out(await services.checkout(session, user.id))


@store_buyer_router.get("/pesanan")
async def list_pesanan(session: AsyncSession = Depends(get_db_store), user: PembeliStore = Depends(get_current_buyer)):
    return [services.pesanan_out(p) for p in await services.list_pesanan_milik(session, user.id)]


@store_buyer_router.get("/pesanan/{pesanan_id}")
async def get_pesanan(
    pesanan_id: str, session: AsyncSession = Depends(get_db_store), user: PembeliStore = Depends(get_current_buyer)
):
    return services.pesanan_out(await _pesanan_milik(session, pesanan_id, user))


# --- Pembayaran (iPaymu -- placeholder, 501 until wired) --------------------


@store_buyer_router.post("/pesanan/{pesanan_id}/bayar")
async def mulai_pembayaran(
    pesanan_id: str, session: AsyncSession = Depends(get_db_store), user: PembeliStore = Depends(get_current_buyer)
):
    pesanan = await _pesanan_milik(session, pesanan_id, user)
    if pesanan.status != "menunggu_pembayaran":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Pesanan berstatus '{pesanan.status}', tidak bisa dibayar ulang",
        )
    result = await ipaymu_create_payment(
        pesanan_id=pesanan.id,
        total=str(pesanan.total),
        nama_pembeli=user.nama,
        email_pembeli=user.email,
        notify_url="/api/store/buyer/payment/callback",
        return_url=f"/pesanan/{pesanan.id}",
    )
    await services.catat_metode_pembayaran(session, pesanan.id, metode="gateway", gateway_ref=result.gateway_ref)
    return {"checkout_url": result.checkout_url}


@store_buyer_router.post("/payment/callback")
async def payment_callback(payload: dict, session: AsyncSession = Depends(get_db_store)):
    """iPaymu webhook. Fails closed: an order is only marked paid when the
    adapter has verified the notification, and until the adapter is wired
    nothing can be verified -- so an anonymous POST can never mark an order
    as paid."""
    gateway_ref = await ipaymu_verify_webhook(payload)
    if not gateway_ref:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Notifikasi pembayaran tidak dapat diverifikasi")
    pesanan = await services.tandai_dibayar_dari_webhook(session, gateway_ref)
    if not pesanan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pesanan tidak ditemukan")
    return {"ok": True}


# --- Pengiriman (Biteship -- placeholder, 501 until wired) ------------------


@store_buyer_router.post("/pengiriman/cek-ongkir")
async def cek_ongkir(payload: CekOngkirIn, _user: PembeliStore = Depends(get_current_buyer)):
    return await biteship_cek_ongkir(
        kode_pos_asal=payload.kode_pos_asal,
        kode_pos_tujuan=payload.kode_pos_tujuan,
        berat_gram=payload.berat_gram,
        nilai_barang=str(payload.nilai_barang),
    )


@store_buyer_router.post("/pesanan/{pesanan_id}/pengiriman")
async def isi_alamat_pengiriman(
    pesanan_id: str,
    payload: PengirimanIn,
    session: AsyncSession = Depends(get_db_store),
    user: PembeliStore = Depends(get_current_buyer),
):
    await _pesanan_milik(session, pesanan_id, user)
    # The buyer must not set their own shipping price: ongkir stays 0 here
    # and becomes server-computed once the Biteship adapter is live.
    payload = payload.model_copy(update={"ongkir": Decimal("0")})
    return services.pengiriman_out(await services.buat_pengiriman_lokal(session, pesanan_id, payload))


@store_buyer_router.get("/pesanan/{pesanan_id}/pengiriman")
async def get_pengiriman(
    pesanan_id: str, session: AsyncSession = Depends(get_db_store), user: PembeliStore = Depends(get_current_buyer)
):
    await _pesanan_milik(session, pesanan_id, user)
    return services.pengiriman_out(await services.get_pengiriman(session, pesanan_id))


# --- Chat ------------------------------------------------------------------


@store_buyer_router.get("/chat")
async def get_percakapan_saya(
    session: AsyncSession = Depends(get_db_store), user: PembeliStore = Depends(get_current_buyer)
):
    percakapan = await services.get_or_create_percakapan(session, user.id)
    percakapan = await services.get_percakapan(session, percakapan.id)
    await services.tandai_dibaca(session, percakapan.id, sebagai_admin=False)
    return services.percakapan_out(percakapan, dengan_pesan=True)


@store_buyer_router.post("/chat")
async def kirim_pesan_saya(
    payload: PesanChatIn,
    session: AsyncSession = Depends(get_db_store),
    user: PembeliStore = Depends(get_current_buyer),
):
    percakapan = await services.get_or_create_percakapan(session, user.id)
    await services.kirim_pesan(session, percakapan.id, user.id, payload.isi, sebagai_admin=False)
    percakapan = await services.get_percakapan(session, percakapan.id)
    return services.percakapan_out(percakapan, dengan_pesan=True)
