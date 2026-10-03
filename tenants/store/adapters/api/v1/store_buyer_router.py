"""HTTP surface of the store buyer (storefront) sub-tenant.

Mounted in main.py at /api/store/buyer. Public: catalog + auth endpoints.
Everything else requires a buyer session (cookie ``store_buyer_token``);
admin sessions are rejected by audience/issuer, and a buyer can only ever
read or change their own cart, addresses, orders and chat.
"""
from __future__ import annotations

import os
from dataclasses import asdict
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.store.modules.store.application import services
from tenants.store.modules.store.application.schemas import (
    AlamatIn,
    AlamatPatch,
    CekOngkirIn,
    CheckoutIn,
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
from tenants.store.modules.store.infrastructure.media_storage import upload_chat_media
from tenants.store.modules.store.infrastructure.models import PembeliStore
from tenants.store.modules.store.infrastructure.payment_ipaymu import ItemBayar
from tenants.store.modules.store.infrastructure.payment_ipaymu import create_payment as ipaymu_create_payment
from tenants.store.modules.store.infrastructure.payment_ipaymu import verify_webhook as ipaymu_verify_webhook
from tenants.store.modules.store.infrastructure import shipping_biteship
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
    item = await services.tambah_ke_keranjang(session, user.id, payload.produk_id, payload.qty, payload.varian_id)
    return services.keranjang_item_out(item)


@store_buyer_router.patch("/keranjang/{ref}")
async def patch_keranjang(
    ref: str,
    payload: KeranjangItemPatch,
    session: AsyncSession = Depends(get_db_store),
    user: PembeliStore = Depends(get_current_buyer),
):
    item = await services.ubah_qty_keranjang(session, user.id, ref, payload.qty)
    return services.keranjang_item_out(item)


@store_buyer_router.delete("/keranjang/{ref}")
async def remove_from_keranjang(
    ref: str, session: AsyncSession = Depends(get_db_store), user: PembeliStore = Depends(get_current_buyer)
):
    await services.hapus_dari_keranjang(session, user.id, ref)
    return {"ok": True}


# --- Pesanan ---------------------------------------------------------------


async def _pesanan_milik(session: AsyncSession, pesanan_id: str, user: PembeliStore):
    pesanan = await services.get_pesanan(session, pesanan_id)
    if pesanan.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pesanan tidak ditemukan")
    return pesanan


@store_buyer_router.post("/pesanan/checkout")
async def checkout(
    payload: CheckoutIn | None = None,
    session: AsyncSession = Depends(get_db_store),
    user: PembeliStore = Depends(get_current_buyer),
):
    return services.pesanan_out(await services.checkout(session, user.id, cod=bool(payload and payload.cod)))


@store_buyer_router.get("/pesanan")
async def list_pesanan(session: AsyncSession = Depends(get_db_store), user: PembeliStore = Depends(get_current_buyer)):
    return [services.pesanan_out(p) for p in await services.list_pesanan_milik(session, user.id)]


@store_buyer_router.get("/pesanan/{pesanan_id}")
async def get_pesanan(
    pesanan_id: str, session: AsyncSession = Depends(get_db_store), user: PembeliStore = Depends(get_current_buyer)
):
    return services.pesanan_out(await _pesanan_milik(session, pesanan_id, user))


# --- Pembayaran (iPaymu -- 501 until IPAYMU_VA / IPAYMU_API_KEY are set) ----


def _url_publik(env: str, default: str) -> str:
    return (os.getenv(env) or default).strip().rstrip("/")


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
    if shipping_biteship.aktif():
        await services.get_pengiriman(session, pesanan.id)  # 404 until the buyer has chosen a courier
    api = _url_publik("API_PUBLIC_URL", "https://api.ampelkuning.com")
    situs = _url_publik("SITE_PUBLIC_URL", "https://ampelkuning.com")
    result = await ipaymu_create_payment(
        pesanan_id=pesanan.id,
        items=[
            ItemBayar(
                nama=f"{it.nama_produk} ({it.nama_varian})" if it.nama_varian else it.nama_produk,
                qty=it.qty,
                harga=str(it.harga_satuan),
            )
            for it in pesanan.items
        ],
        total=str(pesanan.total),
        nama_pembeli=user.nama,
        email_pembeli=user.email,
        notify_url=f"{api}/api/store/buyer/payment/callback",
        return_url=f"{situs}/pesanan/{pesanan.id}",
        cancel_url=f"{situs}/pesanan/{pesanan.id}",
    )
    await services.catat_metode_pembayaran(session, pesanan.id, metode="gateway", gateway_ref=result.gateway_ref)
    return {"checkout_url": result.checkout_url}


async def proses_notifikasi_pembayaran(payload: dict, session: AsyncSession) -> dict:
    """Handle an iPaymu notify. Fails closed: the notify itself is never trusted -- the adapter asks iPaymu what
    the transaction is, and only a verified, paid transaction for the full amount marks the order paid."""
    verified = await ipaymu_verify_webhook(payload)
    if verified is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Notifikasi pembayaran tidak dapat diverifikasi")
    if not verified.lunas:
        return {"ok": True, "status": "belum_lunas"}  # pending / expired: nothing to do, and no retry needed
    pesanan = await services.tandai_dibayar_pesanan(session, verified.reference_id, verified.jumlah)
    if not pesanan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pesanan tidak ditemukan")
    return {"ok": True}


async def _baca_payload(request: Request) -> dict:
    """iPaymu sends JSON or form fields, depending on the dashboard setting."""
    try:
        if "json" in request.headers.get("content-type", "").lower():
            data = await request.json()
        else:
            data = dict(await request.form())
    except Exception:  # noqa: BLE001 -- an unreadable body is simply an unverifiable notify
        return {}
    return data if isinstance(data, dict) else {}


@store_buyer_router.post("/payment/callback")
async def payment_callback(request: Request, session: AsyncSession = Depends(get_db_store)):
    return await proses_notifikasi_pembayaran(await _baca_payload(request), session)


# --- Pengiriman (Biteship -- placeholder, 501 until wired) ------------------


@store_buyer_router.post("/pengiriman/cek-ongkir")
async def cek_ongkir(
    payload: CekOngkirIn, session: AsyncSession = Depends(get_db_store), user: PembeliStore = Depends(get_current_buyer)
):
    """Shipping options for what is in the buyer's cart, to the given postal code. With ``cod`` only the couriers
    that can collect the payment on delivery are offered, with their COD fee."""
    cod_nilai = 0
    if payload.cod:
        cod_nilai = int(services.cek_syarat_cod(await services.get_keranjang(session, user.id)))
    options = await biteship_cek_ongkir(
        kode_pos_tujuan=payload.kode_pos_tujuan,
        items=await services.item_kirim_keranjang(session, user.id),
        cod_nilai=cod_nilai,
    )
    return [asdict(o) for o in options]


@store_buyer_router.post("/pesanan/{pesanan_id}/pengiriman")
async def isi_alamat_pengiriman(
    pesanan_id: str,
    payload: PengirimanIn,
    session: AsyncSession = Depends(get_db_store),
    user: PembeliStore = Depends(get_current_buyer),
):
    pesanan = await _pesanan_milik(session, pesanan_id, user)
    # The buyer must not set their own shipping price: it is always worked out here.
    payload = payload.model_copy(update={"ongkir": Decimal("0"), "layanan_nama": "", "biaya_cod": Decimal("0")})
    if shipping_biteship.aktif():
        cod = pesanan.metode_pembayaran == "cod"
        options = await biteship_cek_ongkir(
            kode_pos_tujuan=payload.kode_pos_tujuan,
            items=await services.item_kirim_pesanan(session, pesanan),
            cod_nilai=int(sum((it.subtotal for it in pesanan.items), Decimal("0"))) if cod else 0,
        )
        pilihan = next((o for o in options if o.kurir == payload.kurir.lower() and o.layanan == payload.layanan), None)
        if pilihan is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Layanan pengiriman yang dipilih tidak tersedia. Pilih ulang."
            )
        payload = payload.model_copy(
            update={
                "kurir": pilihan.kurir,
                "layanan": pilihan.layanan,
                "layanan_nama": pilihan.layanan_nama,
                "ongkir": Decimal(pilihan.ongkir),
                "biaya_cod": Decimal(pilihan.biaya_cod),
            }
        )
    return services.pengiriman_out(await services.buat_pengiriman_lokal(session, pesanan_id, payload))


@store_buyer_router.get("/pesanan/{pesanan_id}/pengiriman/lacak")
async def lacak_pengiriman(
    pesanan_id: str, session: AsyncSession = Depends(get_db_store), user: PembeliStore = Depends(get_current_buyer)
):
    await _pesanan_milik(session, pesanan_id, user)
    return await services.lacak_pengiriman(session, pesanan_id)


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
    await services.kirim_pesan(
        session, percakapan.id, user.id, payload.isi, sebagai_admin=False, produk_id=payload.produk_id
    )
    percakapan = await services.get_percakapan(session, percakapan.id)
    return services.percakapan_out(percakapan, dengan_pesan=True)


@store_buyer_router.post("/chat/lampiran")
async def kirim_lampiran_saya(
    file: UploadFile = File(...),
    isi: str = Form("", max_length=2000),
    session: AsyncSession = Depends(get_db_store),
    user: PembeliStore = Depends(get_current_buyer),
):
    """Send a photo or short video (optionally with a caption) in the buyer's chat."""
    key, jenis = await upload_chat_media(await file.read(), file.content_type or "")
    percakapan = await services.get_or_create_percakapan(session, user.id)
    await services.kirim_pesan(
        session, percakapan.id, user.id, isi.strip(), sebagai_admin=False, lampiran_key=key, lampiran_jenis=jenis
    )
    percakapan = await services.get_percakapan(session, percakapan.id)
    return services.percakapan_out(percakapan, dengan_pesan=True)
