from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import and_, case, delete, exists, func, literal, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.security import hash_password, verify_password
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import (
    AkunMarketplaceIn,
    normalisasi_proses,
    AkunMarketplacePatch,
    ChangePasswordIn,
    GudangIn,
    IklanCampaignIn,
    IklanCampaignPatch,
    IklanMetrikHarianIn,
    ItemPesananIn,
    LoginIn,
    PengirimanIn,
    PesananIn,
    ProdukIn,
    ProdukListingIn,
    ProdukListingPatch,
    ProdukPatch,
    RegisterIn,
    SettlementIn,
    SettlementPatch,
    StaffAkunIn,
    StokAdjustIn,
    StokTransferIn,
    UserCreateIn,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    DEFAULT_GUDANG_KODE,
    PLATFORM_MARKETPLACE,
    REASON_STOK_LEDGER,
    STATUS_IKLAN,
    STATUS_PESANAN,
    AkunMarketplace,
    Gudang,
    IklanCampaign,
    IklanMetrikHarian,
    ItemPesanan,
    KatalogShopee,
    Pesanan,
    Produk,
    ProdukListing,
    Settlement,
    SettlementPesanan,
    StaffAkunMarketplace,
    StokLedger,
    StokReservasi,
    UserMarketplaceErp,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.seeder import (
    DEFAULT_PASSWORD,
    basis_username,
    username_unik,
)

# Orders counted as real sales for reporting -- "unpaid" isn't money yet and
# "cancelled" clearly isn't a sale, same convention as tenants/toko/modules/erp.
_STATUS_TERHITUNG_PENJUALAN = ("to_ship", "shipped", "completed")

# Default "low stock" threshold for the ringkas report when the caller
# doesn't pass batas_stok explicitly.
_DEFAULT_BATAS_STOK_KRITIS = 5

# Linear OMS pipeline (confirm → process → ship stubs). No return path in T2.
_TRANSISI_STATUS = {
    "unpaid": {"to_ship", "cancelled"},
    "to_ship": {"shipped", "cancelled"},
    "shipped": {"completed"},
    "completed": set(),
    "cancelled": set(),
}


def _validate_platform(platform: str) -> str:
    platform = (platform or "").strip().lower()
    if platform not in PLATFORM_MARKETPLACE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Platform tidak dikenal")
    return platform


# --- Auth --------------------------------------------------------------------


async def _cek_email_belum_terdaftar(session: AsyncSession, email: str, *, kecuali_id: str | None = None) -> None:
    stmt = select(UserMarketplaceErp).where(func.lower(UserMarketplaceErp.email) == email.lower())
    if kecuali_id:
        stmt = stmt.where(UserMarketplaceErp.id != kecuali_id)
    if (await session.execute(stmt)).scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email sudah terdaftar")


async def _cek_username_belum_dipakai(session: AsyncSession, username: str, *, kecuali_id: str | None = None) -> None:
    stmt = select(UserMarketplaceErp).where(UserMarketplaceErp.username == username)
    if kecuali_id:
        stmt = stmt.where(UserMarketplaceErp.id != kecuali_id)
    if (await session.execute(stmt)).scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username sudah dipakai")


def _periksa_email_sync(email: str) -> str:
    """Syntax + DNS check (the domain must exist and be able to receive mail). A DNS timeout is not held against the
    user. Returns the normalised email."""
    from email_validator import EmailNotValidError, EmailUndeliverableError, validate_email

    try:
        return validate_email(email, check_deliverability=True, timeout=4).normalized.lower()
    except EmailUndeliverableError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Email tidak valid: domain tidak ditemukan atau tidak bisa menerima email ({exc})",
        ) from exc
    except EmailNotValidError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Format email tidak valid: {exc}") from exc


async def periksa_email(email: str) -> str:
    import asyncio

    return await asyncio.to_thread(_periksa_email_sync, email)


async def register_user(session: AsyncSession, payload: RegisterIn) -> UserMarketplaceErp:
    """Legacy self-registration (always role=owner).

    Only reachable through POST /auth/register when the operator explicitly
    sets MARKETPLACE_ERP_ALLOW_REGISTER=true -- see the router gate.
    """
    await _cek_email_belum_terdaftar(session, payload.email)
    user = UserMarketplaceErp(
        nama=payload.nama,
        username=await username_unik(session, basis_username(str(payload.email).split("@")[0])),
        email=payload.email,
        password_hash=hash_password(payload.password),
        role="owner",
        must_change_password=False,
    )
    session.add(user)
    await session.flush()
    return user


async def create_user(session: AsyncSession, payload: UserCreateIn) -> UserMarketplaceErp:
    """Owner-only account creation. The owner picks a temporary password, so
    the new account must change it on first login."""
    await _cek_username_belum_dipakai(session, payload.username)
    if payload.email:
        await _cek_email_belum_terdaftar(session, str(payload.email))
    user = UserMarketplaceErp(
        nama=payload.nama.strip(),
        username=payload.username,
        email=str(payload.email).lower() if payload.email else None,
        password_hash=hash_password(payload.password),
        role=payload.role,
        must_change_password=True,
    )
    session.add(user)
    await session.flush()
    return user


# Which roles an account of a given role may create (admin: anyone; owner: staff only).
_PERAN_YANG_BOLEH_DIBUAT = {"admin": {"admin", "owner", "staff"}, "owner": {"staff"}}


def pastikan_boleh_membuat_peran(pembuat_role: str | None, peran_baru: str) -> None:
    boleh = _PERAN_YANG_BOLEH_DIBUAT.get((pembuat_role or "").strip().lower(), set())
    if peran_baru not in boleh:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=f"Peran Anda tidak boleh membuat akun dengan peran {peran_baru}"
        )


async def update_user(session: AsyncSession, user_id: str, payload) -> UserMarketplaceErp:
    """Admin-only: change an account's username, display name or role. The last admin cannot be demoted, so the
    ERP never ends up without one."""
    user = await session.get(UserMarketplaceErp, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pengguna tidak ditemukan")
    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "username" in data and data["username"] != user.username:
        await _cek_username_belum_dipakai(session, data["username"], kecuali_id=user.id)
        user.username = data["username"]
    if "nama" in data:
        user.nama = data["nama"].strip()
    if "role" in data and data["role"] != user.role:
        if user.role == "admin":
            jumlah_admin = (
                await session.execute(select(func.count()).select_from(UserMarketplaceErp).where(UserMarketplaceErp.role == "admin"))
            ).scalar_one()
            if jumlah_admin <= 1:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="Admin terakhir tidak bisa diturunkan perannya"
                )
        user.role = data["role"]
    await session.flush()
    return user


async def hapus_user(session: AsyncSession, user_id: str, oleh: UserMarketplaceErp) -> None:
    """Admin-only: delete an account together with its shop assignments. Nobody can delete their own account
    (they would lock themselves out) and the last admin always stays."""
    user = await session.get(UserMarketplaceErp, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pengguna tidak ditemukan")
    if user.id == oleh.id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Akun Anda sendiri tidak bisa dihapus")
    if user.role == "admin":
        jumlah_admin = (
            await session.execute(select(func.count()).select_from(UserMarketplaceErp).where(UserMarketplaceErp.role == "admin"))
        ).scalar_one()
        if jumlah_admin <= 1:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Admin terakhir tidak bisa dihapus")
    await session.execute(delete(StaffAkunMarketplace).where(StaffAkunMarketplace.user_id == user.id))
    await session.delete(user)
    await session.flush()


async def update_profil(session: AsyncSession, user: UserMarketplaceErp, payload) -> UserMarketplaceErp:
    """Every account may change its own display name and contact email (never its username or role). A new email
    must pass the validity check (syntax + the domain can receive mail) and not belong to someone else; an empty
    one removes it. Only the fields that were sent change."""
    kirim = payload.model_fields_set
    if "nama" in kirim and payload.nama:
        user.nama = payload.nama.strip()
    if "email" in kirim:
        if payload.email is None:
            user.email = None
        elif payload.email != (user.email or "").lower():
            email = await periksa_email(payload.email)
            await _cek_email_belum_terdaftar(session, email, kecuali_id=user.id)
            user.email = email
    await session.flush()
    return user


async def list_users(session: AsyncSession) -> list[UserMarketplaceErp]:
    rows = await session.execute(select(UserMarketplaceErp).order_by(UserMarketplaceErp.created_at))
    return list(rows.scalars())


async def change_password(
    session: AsyncSession, user: UserMarketplaceErp, payload: ChangePasswordIn
) -> UserMarketplaceErp:
    # 400 (not 401) on a wrong current password: the caller IS logged in,
    # and a 401 would make the FE treat it as an expired session.
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password saat ini salah")
    if payload.new_password == payload.current_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password baru harus berbeda dari password saat ini",
        )
    if payload.new_password == DEFAULT_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password baru tidak boleh sama dengan password bawaan",
        )
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    await session.flush()
    return user


async def authenticate_user(session: AsyncSession, payload: LoginIn) -> UserMarketplaceErp:
    ident = payload.identitas
    user = (
        await session.execute(
            select(UserMarketplaceErp).where(
                or_(func.lower(UserMarketplaceErp.username) == ident, func.lower(UserMarketplaceErp.email) == ident)
            )
        )
    ).scalars().first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Username/email atau password salah")
    return user


# --- Akun Marketplace ----------------------------------------------------------


async def _cek_duplikat_id_toko_eksternal(
    session: AsyncSession, *, platform: str, id_toko_eksternal: str | None, exclude_id: str | None = None
) -> None:
    if not id_toko_eksternal:
        return
    stmt = select(AkunMarketplace).where(
        AkunMarketplace.platform == platform, AkunMarketplace.id_toko_eksternal == id_toko_eksternal
    )
    if exclude_id:
        stmt = stmt.where(AkunMarketplace.id != exclude_id)
    if (await session.execute(stmt)).scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Toko dengan id_toko_eksternal ini sudah terdaftar untuk platform tsb",
        )


async def list_akun_marketplace(session: AsyncSession, *, platform: str | None = None) -> list[AkunMarketplace]:
    stmt = select(AkunMarketplace).order_by(AkunMarketplace.created_at.desc())
    if platform:
        stmt = stmt.where(AkunMarketplace.platform == _validate_platform(platform))
    return list((await session.execute(stmt)).scalars().all())


async def get_akun_marketplace(session: AsyncSession, akun_id: str) -> AkunMarketplace:
    akun = await session.get(AkunMarketplace, akun_id)
    if not akun:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Akun marketplace tidak ditemukan")
    return akun


async def create_akun_marketplace(session: AsyncSession, payload: AkunMarketplaceIn) -> AkunMarketplace:
    platform = _validate_platform(payload.platform)
    await _cek_duplikat_id_toko_eksternal(session, platform=platform, id_toko_eksternal=payload.id_toko_eksternal)
    akun = AkunMarketplace(
        platform=platform,
        nama_toko=payload.nama_toko,
        id_toko_eksternal=payload.id_toko_eksternal,
        catatan=payload.catatan,
    )
    session.add(akun)
    await session.flush()
    return akun


async def update_akun_marketplace(session: AsyncSession, akun_id: str, payload: AkunMarketplacePatch) -> AkunMarketplace:
    akun = await get_akun_marketplace(session, akun_id)
    data = payload.model_dump(exclude_unset=True)
    if "id_toko_eksternal" in data:
        await _cek_duplikat_id_toko_eksternal(
            session, platform=akun.platform, id_toko_eksternal=data["id_toko_eksternal"], exclude_id=akun.id
        )
    for field, value in data.items():
        setattr(akun, field, value)
    await session.flush()
    return akun


async def delete_akun_marketplace(session: AsyncSession, akun_id: str, *, hapus_pesanan: bool = False) -> dict:
    """Remove a shop and its listings. By default its orders stay (detached from the shop, as before);
    with ``hapus_pesanan`` they are deleted too, with their items and stock reservations. That is meant for
    clearing test data before a real shop is connected. Returns how many orders / listings went."""
    akun = await get_akun_marketplace(session, akun_id)
    jumlah_pesanan = 0
    if hapus_pesanan:
        ids = select(Pesanan.id).where(Pesanan.akun_id == akun_id)
        jumlah_pesanan = int(
            (await session.execute(select(func.count()).select_from(Pesanan).where(Pesanan.akun_id == akun_id))).scalar_one()
        )
        await session.execute(delete(ItemPesanan).where(ItemPesanan.pesanan_id.in_(ids)))
        await session.execute(delete(StokReservasi).where(StokReservasi.pesanan_id.in_(ids)))
        await session.execute(delete(Pesanan).where(Pesanan.akun_id == akun_id))
    jumlah_listing = int(
        (await session.execute(select(func.count()).select_from(ProdukListing).where(ProdukListing.akun_id == akun_id))).scalar_one()
    )
    await session.execute(delete(ProdukListing).where(ProdukListing.akun_id == akun_id))
    await session.execute(delete(KatalogShopee).where(KatalogShopee.akun_id == akun_id))
    await session.execute(delete(SettlementPesanan).where(SettlementPesanan.akun_id == akun_id))
    await session.refresh(akun)
    await session.delete(akun)
    await session.flush()
    return {"pesanan_dihapus": jumlah_pesanan, "listing_dihapus": jumlah_listing}


async def hubungkan_shopee_akun_utama(
    session: AsyncSession,
    akun: AkunMarketplace,
    payload: dict,
    nama_toko: dict[str, str] | None = None,
) -> list[dict]:
    """Bind every shop authorised from a Shopee main account (token payload carries shop_id_list).

    - A shop already in the ERP just gets the new tokens (re-authorisation, no duplicate row).
    - The row that started the flow (``akun``) takes the first new shop; further new shops get rows
      named from ``nama_toko`` (shop name looked up by the caller) or "Shopee <shop_id>".
    - If the starting row ends up unused it is removed, so no empty placeholder is left behind.
    Returns one dict per shop: akun_id, id_toko_eksternal, nama_toko, baru.
    """
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    shop_ids = [str(sid) for sid in payload.get("shop_id_list") or []]
    if not shop_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Shopee tidak mengembalikan toko yang diotorisasi. Pilih minimal satu toko lalu coba lagi.",
        )
    nama_toko = nama_toko or {}
    akun_dipakai = False
    hasil: list[dict] = []
    for sid in shop_ids:
        row = (
            await session.execute(
                select(AkunMarketplace).where(
                    AkunMarketplace.platform == "shopee", AkunMarketplace.id_toko_eksternal == sid
                )
            )
        ).scalar_one_or_none()
        baru = row is None
        if baru and not akun_dipakai and not akun.id_toko_eksternal:
            row, akun_dipakai = akun, True
        elif baru:
            row = AkunMarketplace(platform="shopee", nama_toko=nama_toko.get(sid) or f"Shopee {sid}")
            session.add(row)
        erp_shopee.apply_token_payload(row, payload, shop_id=sid)
        await session.flush()
        hasil.append(
            {"akun_id": row.id, "id_toko_eksternal": sid, "nama_toko": row.nama_toko, "baru": baru}
        )
    if not akun_dipakai and not akun.id_toko_eksternal:
        await session.delete(akun)  # empty placeholder that started the flow
        await session.flush()
    return hasil


# --- Produk (SKU induk) --------------------------------------------------------


async def list_produk(session: AsyncSession) -> list[Produk]:
    stmt = select(Produk).order_by(Produk.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def get_produk(session: AsyncSession, produk_id: str) -> Produk:
    produk = await session.get(Produk, produk_id)
    if not produk:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Produk tidak ditemukan")
    return produk


async def create_produk(session: AsyncSession, payload: ProdukIn) -> Produk:
    existing = (
        await session.execute(select(Produk).where(Produk.sku_induk == payload.sku_induk))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="SKU induk sudah dipakai produk lain")
    if payload.stok < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Stok tidak boleh negatif")
    produk = Produk(
        sku_induk=payload.sku_induk,
        nama=payload.nama,
        deskripsi=payload.deskripsi,
        harga_dasar=payload.harga_dasar,
        stok=payload.stok,
        foto_url=payload.foto_url,
        berat_gram=payload.berat_gram,
        panjang_cm=payload.panjang_cm,
        lebar_cm=payload.lebar_cm,
        tinggi_cm=payload.tinggi_cm,
        preorder=payload.preorder,
        hari_proses=payload.hari_proses,
    )
    session.add(produk)
    try:
        await session.flush()
    except IntegrityError:
        # Two saves of the same SKU raced past the check above (double click /
        # retry): the unique index is the real guard, report it as a conflict.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="SKU induk sudah dipakai produk lain"
        ) from None
    if payload.stok:
        gudang = await ensure_default_gudang(session)
        session.add(
            StokLedger(
                produk_id=produk.id,
                gudang_id=gudang.id,
                qty_delta=payload.stok,
                reason="adjust",
                ref_type="produk",
                ref_id=produk.id,
                catatan="stok awal",
            )
        )
        await session.flush()
    return produk


async def update_produk(session: AsyncSession, produk_id: str, payload: ProdukPatch) -> Produk:
    produk = await get_produk(session, produk_id)
    data = payload.model_dump(exclude_unset=True)
    # Direct stok patch is allowed as an admin override but must go through
    # adjust so the ledger stays the SSOT. Reject raw stok overwrite here.
    if "stok" in data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ubah stok lewat POST /stok/adjust, bukan PATCH produk",
        )
    for kolom in ("berat_gram", "panjang_cm", "lebar_cm", "tinggi_cm", "preorder", "hari_proses"):
        if kolom in data and data[kolom] is None:
            del data[kolom]
    if "preorder" in data or "hari_proses" in data:
        try:
            data["preorder"] = data.get("preorder", produk.preorder)
            data["hari_proses"] = normalisasi_proses(data["preorder"], data.get("hari_proses", produk.hari_proses))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    for field, value in data.items():
        setattr(produk, field, value)
    await session.flush()
    return produk


async def delete_produk(session: AsyncSession, produk_id: str) -> None:
    """Delete a product with its stock history, reservations and listings. Order lines that pointed at it keep
    their text and simply lose the link. (Done in bulk: letting the ORM delete the product would try to detach
    the stock ledger rows instead, and ``produk_id`` there is NOT NULL.)"""
    produk = await get_produk(session, produk_id)
    listing_ids = select(ProdukListing.id).where(ProdukListing.produk_id == produk_id)
    await session.execute(update(ItemPesanan).where(ItemPesanan.listing_id.in_(listing_ids)).values(listing_id=None))
    await session.execute(update(ItemPesanan).where(ItemPesanan.produk_id == produk_id).values(produk_id=None))
    await session.execute(delete(StokLedger).where(StokLedger.produk_id == produk_id))
    await session.execute(delete(StokReservasi).where(StokReservasi.produk_id == produk_id))
    await session.execute(delete(ProdukListing).where(ProdukListing.produk_id == produk_id))
    await session.refresh(produk)
    await session.delete(produk)
    await session.flush()


# --- Produk Listing -----------------------------------------------------------


async def list_listing(session: AsyncSession, *, produk_id: str | None = None) -> list[ProdukListing]:
    stmt = select(ProdukListing).order_by(ProdukListing.created_at.desc())
    if produk_id:
        stmt = stmt.where(ProdukListing.produk_id == produk_id)
    return list((await session.execute(stmt)).scalars().all())


async def create_listing(session: AsyncSession, payload: ProdukListingIn) -> ProdukListing:
    platform = _validate_platform(payload.platform)
    await get_produk(session, payload.produk_id)
    await get_akun_marketplace(session, payload.akun_id)

    existing = (
        await session.execute(
            select(ProdukListing).where(
                ProdukListing.platform == platform, ProdukListing.id_eksternal == payload.id_eksternal
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Listing dengan id_eksternal ini sudah terdaftar"
        )

    listing = ProdukListing(
        produk_id=payload.produk_id,
        akun_id=payload.akun_id,
        platform=platform,
        id_eksternal=payload.id_eksternal,
        harga_jual=payload.harga_jual,
        stok_listing=payload.stok_listing,
    )
    session.add(listing)
    await session.flush()
    return listing


async def get_listing(session: AsyncSession, listing_id: str) -> ProdukListing:
    listing = await session.get(ProdukListing, listing_id)
    if not listing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Listing tidak ditemukan")
    return listing


async def update_listing(session: AsyncSession, listing_id: str, payload: ProdukListingPatch) -> ProdukListing:
    listing = await get_listing(session, listing_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(listing, field, value)
    await session.flush()
    return listing


async def delete_listing(session: AsyncSession, listing_id: str) -> None:
    listing = await get_listing(session, listing_id)
    await session.delete(listing)
    await session.flush()


async def impor_listing_marketplace(session: AsyncSession, akun: AkunMarketplace, entries: list[dict]) -> dict:
    """Link catalogue entries pulled from a marketplace to Produk by SKU.

    A new ProdukListing is created when the entry's SKU equals a Produk.sku_induk (case-insensitive).
    Existing listings are left alone: harga_jual / stok_listing are owner overrides that the
    marketplace must not overwrite. A new listing starts with the marketplace's current price so
    the first price push is a no-op. Entries without a matching SKU are reported, not created --
    inventing Produk rows would also invent stock.
    """
    hasil: dict = {"listing_baru": 0, "sudah_ada": 0, "tanpa_sku_cocok": 0, "contoh_tanpa_sku": []}
    ids = [e["id_eksternal"] for e in entries]
    ada = set()
    if ids:
        stmt = select(ProdukListing.id_eksternal).where(
            ProdukListing.platform == akun.platform, ProdukListing.id_eksternal.in_(ids)
        )
        ada = set((await session.execute(stmt)).scalars())
    skus = {e["sku"].lower() for e in entries if e["sku"]}
    produk_by_sku: dict[str, Produk] = {}
    if skus:
        stmt = select(Produk).where(func.lower(Produk.sku_induk).in_(skus))
        produk_by_sku = {p.sku_induk.lower(): p for p in (await session.execute(stmt)).scalars()}

    for e in entries:
        if e["id_eksternal"] in ada:
            hasil["sudah_ada"] += 1
            continue
        produk = produk_by_sku.get(e["sku"].lower()) if e["sku"] else None
        if produk is None:
            hasil["tanpa_sku_cocok"] += 1
            if len(hasil["contoh_tanpa_sku"]) < 20:
                hasil["contoh_tanpa_sku"].append(
                    {"id_eksternal": e["id_eksternal"], "nama_produk": e["nama_produk"], "sku": e["sku"]}
                )
            continue
        session.add(
            ProdukListing(
                produk_id=produk.id,
                akun_id=akun.id,
                platform=akun.platform,
                id_eksternal=e["id_eksternal"],
                harga_jual=e["harga"],
                aktif=e["aktif"],
            )
        )
        hasil["listing_baru"] += 1
    await session.flush()
    return hasil


async def simpan_katalog_shopee(session: AsyncSession, akun: AkunMarketplace, entries: list[dict]) -> dict:
    """Store the pulled Shopee catalogue for one shop (upsert per item). Only reference data: no Produk,
    stock or listing is created and Shopee is not touched. Items that disappeared from the shop are removed
    unless they were already sent to the store (that row is kept so the link is not lost)."""
    import json

    ada = {
        k.item_id: k
        for k in (await session.execute(select(KatalogShopee).where(KatalogShopee.akun_id == akun.id))).scalars()
    }
    baru = diperbarui = 0
    sekarang = datetime.now(timezone.utc)
    for e in entries:
        row = ada.pop(e["item_id"], None)
        if row is None:
            row = KatalogShopee(akun_id=akun.id, item_id=e["item_id"], nama=e["nama"])
            session.add(row)
            baru += 1
        else:
            diperbarui += 1
        row.nama = e["nama"]
        row.sku = e["sku"]
        row.deskripsi = e["deskripsi"]
        row.foto_json = json.dumps(e["foto"])
        row.varian_json = json.dumps(e["varian"])
        row.harga_min = e["harga_min"]
        row.harga_max = e["harga_max"]
        row.stok_shopee = e["stok_shopee"]
        row.berat_gram = e["berat_gram"]
        row.panjang_cm = e["panjang_cm"]
        row.lebar_cm = e["lebar_cm"]
        row.tinggi_cm = e["tinggi_cm"]
        row.status = e["status"]
        row.diambil_at = sekarang
    dihapus = 0
    for hilang in ada.values():
        if hilang.dikirim_toko_id is None:
            await session.delete(hilang)
            dihapus += 1
    await session.flush()
    return {"katalog_baru": baru, "katalog_diperbarui": diperbarui, "katalog_dihapus": dihapus}


KATALOG_FOTO_DAFTAR = 5
KATALOG_DESKRIPSI_DAFTAR = 300
KUNCI_URUT_KATALOG = ("toko", "nama", "sku", "harga", "stok", "berat", "status", "dikirim", "diambil")
_ALIAS_URUT_KATALOG = {
    "toko": "toko:asc", "nama": "nama:asc", "harga_naik": "harga:asc", "harga_turun": "harga:desc",
    "stok": "stok:desc", "terbaru": "diambil:desc",
}


def katalog_out(k: KatalogShopee, nama_toko: str | None = None, *, lengkap: bool = False) -> dict:
    import json

    foto = json.loads(k.foto_json or "[]")
    out = {
        "id": k.id,
        "akun_id": k.akun_id,
        "nama_toko": nama_toko,
        "item_id": k.item_id,
        "nama": k.nama,
        "sku": k.sku,
        "foto_utama": foto[0] if foto else None,
        "jumlah_foto": len(foto),
        "harga_min": k.harga_min,
        "harga_max": k.harga_max,
        "stok_shopee": k.stok_shopee,
        "jumlah_varian": len(json.loads(k.varian_json or "[]")),
        "status": k.status,
        "dikirim_toko_id": k.dikirim_toko_id,
        "dikirim_at": k.dikirim_at,
        # Enough to compare rows side by side in the list view; the full text and photos are in the detail.
        "foto": foto[:KATALOG_FOTO_DAFTAR],
        "deskripsi_ringkas": (k.deskripsi or "")[:KATALOG_DESKRIPSI_DAFTAR],
        "berat_gram": k.berat_gram,
        "panjang_cm": k.panjang_cm,
        "lebar_cm": k.lebar_cm,
        "tinggi_cm": k.tinggi_cm,
        "diambil_at": k.diambil_at,
    }
    if lengkap:
        out.update(deskripsi=k.deskripsi, foto=foto, varian=json.loads(k.varian_json or "[]"))
    return out


async def list_katalog_shopee(
    session: AsyncSession,
    *,
    akun_id: str | None = None,
    q: str | None = None,
    belum_dikirim: bool = False,
    urut: str = "toko:asc",
    halaman: int = 1,
    per_halaman: int = 48,
) -> dict:
    """Catalogue rows across all shops (``akun_id`` None) or one shop, with shop name. ``urut`` picks the order
    (by name puts the same title from different shops next to each other, for comparing)."""
    kunci, turun = _urut_kunci(urut, sah=KUNCI_URUT_KATALOG, alias=_ALIAS_URUT_KATALOG)
    kolom = {
        "toko": func.lower(AkunMarketplace.nama_toko),
        "nama": func.lower(KatalogShopee.nama),
        "sku": func.lower(KatalogShopee.sku),
        "harga": KatalogShopee.harga_min,
        "stok": KatalogShopee.stok_shopee,
        "berat": KatalogShopee.berat_gram,
        "status": KatalogShopee.status,  # NORMAL (listed) sorts before UNLIST
        "dikirim": KatalogShopee.dikirim_toko_id.is_not(None),  # sent = true, so asc lists the not-yet-sent first
        "diambil": KatalogShopee.diambil_at,
    }[kunci]
    urutan = ((kolom.desc() if turun else kolom.asc()).nulls_last(), func.lower(AkunMarketplace.nama_toko), func.lower(KatalogShopee.nama))
    cond = []
    if akun_id:
        cond.append(KatalogShopee.akun_id == akun_id)
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        cond.append(or_(func.lower(KatalogShopee.nama).like(like), func.lower(KatalogShopee.sku).like(like)))
    if belum_dikirim:
        cond.append(KatalogShopee.dikirim_toko_id.is_(None))
    total = int((await session.execute(select(func.count()).select_from(KatalogShopee).where(*cond))).scalar_one())
    stmt = (
        select(KatalogShopee, AkunMarketplace.nama_toko)
        .join(AkunMarketplace, AkunMarketplace.id == KatalogShopee.akun_id)
        .where(*cond)
        .order_by(*urutan, KatalogShopee.id)
        .offset((max(halaman, 1) - 1) * per_halaman)
        .limit(per_halaman)
    )
    rows = (await session.execute(stmt)).all()
    return {"total": total, "halaman": halaman, "per_halaman": per_halaman, "items": [katalog_out(k, n) for k, n in rows]}


async def jumlah_katalog_per_toko(session: AsyncSession) -> dict[str, int]:
    rows = (await session.execute(select(KatalogShopee.akun_id, func.count()).group_by(KatalogShopee.akun_id))).all()
    return {a: int(n) for a, n in rows}


async def get_katalog_shopee(session: AsyncSession, katalog_id: str) -> tuple[KatalogShopee, str]:
    row = (
        await session.execute(
            select(KatalogShopee, AkunMarketplace.nama_toko)
            .join(AkunMarketplace, AkunMarketplace.id == KatalogShopee.akun_id)
            .where(KatalogShopee.id == katalog_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Produk katalog tidak ditemukan")
    return row[0], row[1]


async def baris_push_listing(session: AsyncSession, akun: AkunMarketplace) -> list[dict]:
    """What a stock/price push would send for a shop: active listings of active products.

    Stock/price are the listing override when set, else the Produk value (stok = available quantity).
    """
    stmt = (
        select(ProdukListing, Produk)
        .join(Produk, ProdukListing.produk_id == Produk.id)
        .where(ProdukListing.akun_id == akun.id, ProdukListing.aktif.is_(True), Produk.aktif.is_(True))
        .order_by(Produk.sku_induk, ProdukListing.id_eksternal)
    )
    rows = []
    for listing, produk in (await session.execute(stmt)).all():
        rows.append(
            {
                "id_eksternal": listing.id_eksternal,
                "sku_induk": produk.sku_induk,
                "nama_produk": produk.nama,
                "stok": listing.stok_listing if listing.stok_listing is not None else produk.stok,
                "harga": listing.harga_jual if listing.harga_jual is not None else produk.harga_dasar,
            }
        )
    return rows


# --- Tahap 2: Gudang + Stock --------------------------------------------------


async def ensure_default_gudang(session: AsyncSession) -> Gudang:
    gudang = (
        await session.execute(select(Gudang).where(Gudang.kode == DEFAULT_GUDANG_KODE))
    ).scalar_one_or_none()
    if gudang:
        return gudang
    gudang = Gudang(kode=DEFAULT_GUDANG_KODE, nama="Gudang Utama")
    session.add(gudang)
    await session.flush()
    return gudang


async def list_gudang(session: AsyncSession) -> list[Gudang]:
    await ensure_default_gudang(session)
    return list((await session.execute(select(Gudang).order_by(Gudang.kode))).scalars().all())


async def create_gudang(session: AsyncSession, payload: GudangIn) -> Gudang:
    existing = (await session.execute(select(Gudang).where(Gudang.kode == payload.kode))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Kode gudang sudah dipakai")
    gudang = Gudang(kode=payload.kode, nama=payload.nama)
    session.add(gudang)
    await session.flush()
    return gudang


async def transfer_stok(session: AsyncSession, payload: StokTransferIn) -> Produk:
    """Move qty of one Produk from one Gudang to another as a paired ledger
    entry (transfer_out at the source, transfer_in at the destination).
    Produk.stok (the available-everywhere cache) is unchanged -- a transfer
    doesn't add or remove available stock, it only moves which warehouse
    holds it."""
    if payload.dari_gudang_id == payload.ke_gudang_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Gudang asal dan tujuan tidak boleh sama")

    produk_stmt = select(Produk).where(Produk.id == payload.produk_id).with_for_update()
    produk = (await session.execute(produk_stmt)).scalar_one_or_none()
    if not produk:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Produk tidak ditemukan")

    dari = await session.get(Gudang, payload.dari_gudang_id)
    if not dari:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gudang asal tidak ditemukan")
    ke = await session.get(Gudang, payload.ke_gudang_id)
    if not ke:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gudang tujuan tidak ditemukan")

    # Per-warehouse on-hand isn't tracked as a running balance column (Tahap
    # 2/3 keeps Produk.stok as the single available-everywhere cache) -- the
    # ledger itself is the source of truth for "how much of this SKU is
    # physically in this warehouse", so validate against a ledger sum here.
    saldo_row = await session.execute(
        select(func.coalesce(func.sum(StokLedger.qty_delta), 0)).where(
            StokLedger.produk_id == payload.produk_id, StokLedger.gudang_id == payload.dari_gudang_id
        )
    )
    saldo_asal = int(saldo_row.scalar_one())
    if saldo_asal < payload.qty:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Saldo gudang asal tidak cukup (tersedia {saldo_asal}, butuh {payload.qty})",
        )

    session.add(
        StokLedger(
            produk_id=produk.id,
            gudang_id=dari.id,
            qty_delta=-payload.qty,
            reason="transfer_out",
            ref_type="transfer",
            ref_id=ke.id,
            catatan=payload.catatan,
        )
    )
    session.add(
        StokLedger(
            produk_id=produk.id,
            gudang_id=ke.id,
            qty_delta=payload.qty,
            reason="transfer_in",
            ref_type="transfer",
            ref_id=dari.id,
            catatan=payload.catatan,
        )
    )
    await session.flush()
    return produk


async def list_stok_ledger(
    session: AsyncSession, *, produk_id: str | None = None, limit: int = 100
) -> list[StokLedger]:
    stmt = select(StokLedger).order_by(StokLedger.created_at.desc()).limit(max(1, min(limit, 500)))
    if produk_id:
        stmt = stmt.where(StokLedger.produk_id == produk_id)
    return list((await session.execute(stmt)).scalars().all())


async def adjust_stok(session: AsyncSession, payload: StokAdjustIn) -> Produk:
    """Manual stock adjustment. Updates Produk.stok atomically and appends ledger."""
    if payload.qty_delta == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="qty_delta tidak boleh 0")

    stmt = select(Produk).where(Produk.id == payload.produk_id).with_for_update()
    produk = (await session.execute(stmt)).scalar_one_or_none()
    if not produk:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Produk tidak ditemukan")

    new_stok = produk.stok + payload.qty_delta
    if new_stok < 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Stok tidak cukup (tersedia {produk.stok}, delta {payload.qty_delta})",
        )

    gudang_id = payload.gudang_id
    if gudang_id:
        gudang = await session.get(Gudang, gudang_id)
        if not gudang:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gudang tidak ditemukan")
    else:
        gudang = await ensure_default_gudang(session)
        gudang_id = gudang.id

    produk.stok = new_stok
    session.add(
        StokLedger(
            produk_id=produk.id,
            gudang_id=gudang_id,
            qty_delta=payload.qty_delta,
            reason="adjust",
            ref_type="adjust",
            ref_id=produk.id,
            catatan=payload.catatan,
        )
    )
    await session.flush()
    return produk


async def _reserve_for_pesanan(session: AsyncSession, pesanan: Pesanan) -> None:
    """Hold available stock for each line that has produk_id. Fails the whole
    transition if any line would oversell."""
    gudang = await ensure_default_gudang(session)
    for item in pesanan.items:
        if not item.produk_id:
            continue
        stmt = select(Produk).where(Produk.id == item.produk_id).with_for_update()
        produk = (await session.execute(stmt)).scalar_one_or_none()
        if not produk:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Produk {item.produk_id} pada item pesanan tidak ditemukan",
            )
        if produk.stok < item.qty:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Stok tidak cukup untuk mereservasi '{produk.sku_induk}' "
                    f"(tersedia {produk.stok}, butuh {item.qty})"
                ),
            )
        produk.stok -= item.qty
        session.add(
            StokReservasi(
                produk_id=produk.id,
                gudang_id=gudang.id,
                pesanan_id=pesanan.id,
                qty=item.qty,
                status="aktif",
            )
        )
        session.add(
            StokLedger(
                produk_id=produk.id,
                gudang_id=gudang.id,
                qty_delta=-item.qty,
                reason="reserve",
                ref_type="pesanan",
                ref_id=pesanan.id,
                catatan=f"reserve for {pesanan.id_eksternal}",
            )
        )
    await session.flush()


async def _release_reservasi_pesanan(session: AsyncSession, pesanan: Pesanan) -> None:
    """Undo aktif reservations on cancel -- restore Produk.stok."""
    gudang = await ensure_default_gudang(session)
    stmt = select(StokReservasi).where(
        StokReservasi.pesanan_id == pesanan.id, StokReservasi.status == "aktif"
    )
    rows = list((await session.execute(stmt)).scalars().all())
    for row in rows:
        produk_stmt = select(Produk).where(Produk.id == row.produk_id).with_for_update()
        produk = (await session.execute(produk_stmt)).scalar_one_or_none()
        if produk:
            produk.stok += row.qty
        row.status = "released"
        session.add(
            StokLedger(
                produk_id=row.produk_id,
                gudang_id=row.gudang_id or gudang.id,
                qty_delta=row.qty,
                reason="release",
                ref_type="pesanan",
                ref_id=pesanan.id,
                catatan=f"release on cancel {pesanan.id_eksternal}",
            )
        )
    await session.flush()


async def _consume_reservasi_pesanan(session: AsyncSession, pesanan: Pesanan) -> None:
    """Mark reservations consumed on ship. Available cache already reduced
    at reserve time; ledger records the outbound ship event (qty_delta=0
    relative to available, documented as reason=ship)."""
    gudang = await ensure_default_gudang(session)
    stmt = select(StokReservasi).where(
        StokReservasi.pesanan_id == pesanan.id, StokReservasi.status == "aktif"
    )
    rows = list((await session.execute(stmt)).scalars().all())
    for row in rows:
        row.status = "consumed"
        session.add(
            StokLedger(
                produk_id=row.produk_id,
                gudang_id=row.gudang_id or gudang.id,
                qty_delta=0,
                reason="ship",
                ref_type="pesanan",
                ref_id=pesanan.id,
                catatan=f"ship consumed reservation qty={row.qty} for {pesanan.id_eksternal}",
            )
        )
    await session.flush()


# --- Tahap 2: Orders OMS ------------------------------------------------------


def pesanan_out(pesanan: Pesanan) -> Pesanan:
    """Identity helper -- router uses PesananOut from_attributes."""
    return pesanan


async def list_pesanan(
    session: AsyncSession,
    *,
    platform: str | None = None,
    akun_id: str | None = None,
    status_filter: str | None = None,
) -> list[Pesanan]:
    stmt = select(Pesanan).options(selectinload(Pesanan.items)).order_by(Pesanan.created_at.desc())
    if platform:
        stmt = stmt.where(Pesanan.platform == _validate_platform(platform))
    if akun_id:
        stmt = stmt.where(Pesanan.akun_id == akun_id)
    if status_filter:
        status_filter = status_filter.strip().lower()
        if status_filter not in STATUS_PESANAN:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status tidak dikenal")
        stmt = stmt.where(Pesanan.status == status_filter)
    return list((await session.execute(stmt)).scalars().all())


def _urut_kunci(urut: str, *, sah: tuple[str, ...], alias: dict[str, str] | None = None) -> tuple[str, bool]:
    """'total:desc' -> ('total', True). Old single-word values go through ``alias``. 400 on anything unknown."""
    urut = (alias or {}).get(urut, urut)
    kunci, _, arah = urut.partition(":")
    if kunci not in sah or arah not in ("", "asc", "desc"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"urut harus berbentuk kunci:asc atau kunci:desc, dengan kunci salah satu dari {sah}",
        )
    return kunci, arah == "desc"


# --- Pesanan: filterable, paged list + counts for the filter chips ---------------

# Shopee statuses after "arrange shipment" (kept in step with lib/pesanan.ts SUDAH_DIPROSES in the frontend).
_MP_SUDAH_DIPROSES = ("PROCESSED", "SHIPPED", "TO_CONFIRM_RECEIVE", "COMPLETED")
TAHAP_PESANAN = ("belum_bayar", "perlu_diproses", "menunggu_kurir", "dikirim", "selesai", "dibatalkan")
KUNCI_URUT_PESANAN = ("tanggal", "nomor", "toko", "status", "total", "kurir")
_ALIAS_URUT_PESANAN = {"terbaru": "tanggal:desc", "terlama": "tanggal:asc", "total_besar": "total:desc", "total_kecil": "total:asc"}


def _tgl_pesanan():
    """When the order was placed: the marketplace's time, or the time the ERP first saw it."""
    return func.coalesce(Pesanan.dipesan_at, Pesanan.created_at)


def _kondisi_tahap(tahap: str):
    sudah = Pesanan.status_marketplace.in_(_MP_SUDAH_DIPROSES)
    return {
        "belum_bayar": Pesanan.status == "unpaid",
        "perlu_diproses": and_(Pesanan.status == "to_ship", or_(Pesanan.status_marketplace.is_(None), ~sudah)),
        "menunggu_kurir": and_(Pesanan.status == "to_ship", sudah),
        "dikirim": Pesanan.status == "shipped",
        "selesai": Pesanan.status == "completed",
        "dibatalkan": Pesanan.status == "cancelled",
    }[tahap]


def _ekspresi_tahap():
    return case(
        *[(_kondisi_tahap(t), literal(t)) for t in TAHAP_PESANAN[:-1]],
        else_=literal("dibatalkan"),
    )


def _kondisi_pesanan(
    *,
    akun_id: str | None = None,
    akun_diizinkan: set[str] | None = None,
    tahap: str | None = None,
    resi: str | None = None,
    q: str | None = None,
    dari: datetime | None = None,
    sampai: datetime | None = None,
) -> list:
    cond = []
    if akun_diizinkan is not None:
        cond.append(Pesanan.akun_id.in_(akun_diizinkan) if akun_diizinkan else literal(False))
    if akun_id:
        cond.append(Pesanan.akun_id == akun_id)
    if tahap:
        if tahap not in TAHAP_PESANAN:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"tahap harus salah satu dari {TAHAP_PESANAN}")
        cond.append(_kondisi_tahap(tahap))
    if resi:
        if resi not in ("belum", "sudah"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="resi harus 'belum' atau 'sudah'")
        cond += [
            Pesanan.status == "to_ship",
            Pesanan.status_marketplace == "PROCESSED",
            Pesanan.resi_dicetak_at.is_not(None) if resi == "sudah" else Pesanan.resi_dicetak_at.is_(None),
        ]
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        cond.append(
            or_(
                func.lower(Pesanan.id_eksternal).like(like),
                func.lower(Pesanan.nama_pembeli).like(like),
                func.lower(func.coalesce(Pesanan.nomor_resi, "")).like(like),
                exists().where(ItemPesanan.pesanan_id == Pesanan.id, func.lower(ItemPesanan.nama_produk).like(like)),
            )
        )
    if dari:
        cond.append(_tgl_pesanan() >= dari)
    if sampai:
        cond.append(_tgl_pesanan() <= sampai)
    return cond


async def daftar_pesanan(
    session: AsyncSession,
    *,
    urut: str = "tanggal:desc",
    halaman: int = 1,
    per_halaman: int = 50,
    **filters,
) -> dict:
    """One page of orders matching the filters, plus the total match count (all pages).

    ``urut`` is ``<column>:asc|desc`` with column one of KUNCI_URUT_PESANAN (the old terbaru / terlama /
    total_besar / total_kecil still work). Rows with no value for the column go last.
    """
    kunci, turun = _urut_kunci(urut, sah=KUNCI_URUT_PESANAN, alias=_ALIAS_URUT_PESANAN)
    cond = _kondisi_pesanan(**filters)
    total = int((await session.execute(select(func.count()).select_from(Pesanan).where(*cond))).scalar_one())
    nama_toko = select(AkunMarketplace.nama_toko).where(AkunMarketplace.id == Pesanan.akun_id).scalar_subquery()
    urutan_tahap = case(*[(_kondisi_tahap(t), i) for i, t in enumerate(TAHAP_PESANAN[:-1])], else_=len(TAHAP_PESANAN) - 1)
    kolom = {
        "tanggal": _tgl_pesanan(),
        "nomor": func.lower(Pesanan.id_eksternal),
        "toko": func.lower(nama_toko),
        "status": urutan_tahap,
        "total": Pesanan.total,
        "kurir": func.lower(func.coalesce(Pesanan.kurir, "")),
    }[kunci]
    stmt = (
        select(Pesanan)
        .options(selectinload(Pesanan.items))
        .where(*cond)
        .order_by((kolom.desc() if turun else kolom.asc()).nulls_last(), _tgl_pesanan().desc(), Pesanan.id)
        .offset((max(halaman, 1) - 1) * per_halaman)
        .limit(per_halaman)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return {"total": total, "halaman": halaman, "per_halaman": per_halaman, "items": rows}


async def ringkasan_pesanan(
    session: AsyncSession,
    *,
    akun_id: str | None = None,
    tahap: str | None = None,
    **filters,
) -> dict:
    """Counts for the filter chips. Each chip row ignores its own filter so it shows what picking it would give:
    the status counts honour the chosen shop, the shop counts honour the chosen status."""
    dasar = dict(filters)
    per_tahap = (
        await session.execute(
            select(_ekspresi_tahap().label("t"), func.count())
            .where(*_kondisi_pesanan(akun_id=akun_id, **dasar))
            .group_by("t")
        )
    ).all()
    tahap_hitung = {t: 0 for t in TAHAP_PESANAN} | {t: int(n) for t, n in per_tahap}
    per_toko = (
        await session.execute(
            select(Pesanan.akun_id, func.count())
            .where(*_kondisi_pesanan(tahap=tahap, **dasar))
            .group_by(Pesanan.akun_id)
        )
    ).all()
    jumlah_toko = {a: int(n) for a, n in per_toko}
    akun_ids = dasar.get("akun_diizinkan")
    toko = sorted(
        (a for a in await list_akun_marketplace(session) if a.id_toko_eksternal and (akun_ids is None or a.id in akun_ids)),
        key=lambda a: a.nama_toko.lower(),
    )
    return {
        "tahap": {"semua": sum(tahap_hitung.values()), **tahap_hitung},
        "toko": [{"akun_id": a.id, "nama_toko": a.nama_toko, "jumlah": jumlah_toko.get(a.id, 0)} for a in toko],
        "total_toko": sum(jumlah_toko.values()),
    }


async def get_pesanan(session: AsyncSession, pesanan_id: str) -> Pesanan:
    stmt = (
        select(Pesanan)
        .where(Pesanan.id == pesanan_id)
        .options(selectinload(Pesanan.items), selectinload(Pesanan.akun), selectinload(Pesanan.reservasi))
    )
    pesanan = (await session.execute(stmt)).scalar_one_or_none()
    if not pesanan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pesanan tidak ditemukan")
    return pesanan


def _item_subtotal(item: ItemPesananIn) -> Decimal:
    if item.subtotal is not None:
        return item.subtotal
    return Decimal(item.harga_satuan) * item.qty


async def create_pesanan(session: AsyncSession, payload: PesananIn) -> Pesanan:
    """Create (or reject duplicate) a unified inbox order. Owner-gated at
    router; single-org tenant so no per-owner row filter yet."""
    platform = _validate_platform(payload.platform)
    status_awal = (payload.status or "unpaid").strip().lower()
    if status_awal not in STATUS_PESANAN:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status tidak dikenal")
    if status_awal in {"shipped", "completed"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pesanan baru tidak boleh langsung shipped/completed",
        )

    if payload.akun_id:
        akun = await get_akun_marketplace(session, payload.akun_id)
        if akun.platform != platform:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="akun_id tidak cocok dengan platform pesanan",
            )

    existing = (
        await session.execute(
            select(Pesanan).where(Pesanan.platform == platform, Pesanan.id_eksternal == payload.id_eksternal)
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Pesanan dengan id_eksternal ini sudah ada",
        )

    for item in payload.items:
        if item.produk_id:
            await get_produk(session, item.produk_id)
        if item.listing_id:
            await get_listing(session, item.listing_id)

    total = payload.total
    if total is None:
        total = sum((_item_subtotal(it) for it in payload.items), Decimal("0"))

    pesanan = Pesanan(
        platform=platform,
        id_eksternal=payload.id_eksternal,
        akun_id=payload.akun_id,
        status="unpaid",  # always start unpaid; reserve happens on to_ship
        nama_pembeli=payload.nama_pembeli or "",
        total=total,
    )
    session.add(pesanan)
    await session.flush()

    for item in payload.items:
        session.add(
            ItemPesanan(
                pesanan_id=pesanan.id,
                produk_id=item.produk_id,
                listing_id=item.listing_id,
                nama_produk=item.nama_produk,
                harga_satuan=item.harga_satuan,
                qty=item.qty,
                subtotal=_item_subtotal(item),
            )
        )
    await session.flush()

    # Re-load with items for potential status advance.
    pesanan = await get_pesanan(session, pesanan.id)
    if status_awal != "unpaid":
        pesanan = await ubah_status_pesanan(session, pesanan.id, status_awal)
    return pesanan


async def _dorong_proses_ke_marketplace(session: AsyncSession, pesanan: Pesanan) -> None:
    """Soft-fail push on to_ship. Never rolls back local status."""
    if not pesanan.akun_id:
        pesanan.tersinkron_marketplace = False
        pesanan.catatan_sinkron = "Tidak bisa disinkronkan: akun marketplace tidak terhubung"
        return
    akun = pesanan.akun or await session.get(AkunMarketplace, pesanan.akun_id)
    if akun is None:
        pesanan.tersinkron_marketplace = False
        pesanan.catatan_sinkron = "Tidak bisa disinkronkan: akun marketplace tidak ditemukan"
        return

    try:
        if pesanan.platform == "shopee":
            from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import (
                erp_shopee,
            )

            await erp_shopee.proses_pesanan(akun, pesanan)
        elif pesanan.platform == "lazada":
            from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import (
                erp_lazada,
            )

            await erp_lazada.proses_pesanan(akun, pesanan)
        elif pesanan.platform == "tiktokshop":
            from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import (
                erp_tiktok,
            )

            await erp_tiktok.proses_pesanan(akun, pesanan)
        elif pesanan.platform == "blibli":
            from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import (
                erp_blibli,
            )

            await erp_blibli.proses_pesanan(akun, pesanan)
        else:
            pesanan.tersinkron_marketplace = False
            pesanan.catatan_sinkron = f"Adapter {pesanan.platform} belum tersedia"
            return
    except HTTPException as exc:
        pesanan.tersinkron_marketplace = False
        pesanan.catatan_sinkron = str(exc.detail)
    except Exception as exc:  # noqa: BLE001 -- soft-fail by design
        pesanan.tersinkron_marketplace = False
        pesanan.catatan_sinkron = str(exc)
    else:
        pesanan.tersinkron_marketplace = True
        pesanan.catatan_sinkron = f"Berhasil disinkronkan ke {pesanan.platform}"


async def ubah_status_pesanan(
    session: AsyncSession, pesanan_id: str, status_baru: str, *, dorong_marketplace: bool = True
) -> Pesanan:
    status_baru = (status_baru or "").strip().lower()
    if status_baru not in STATUS_PESANAN:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status tidak dikenal")
    pesanan = await get_pesanan(session, pesanan_id)
    if status_baru not in _TRANSISI_STATUS.get(pesanan.status, set()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tidak bisa ubah status dari '{pesanan.status}' ke '{status_baru}'",
        )

    prev = pesanan.status
    if status_baru == "to_ship":
        await _reserve_for_pesanan(session, pesanan)
        pesanan.status = status_baru
        if dorong_marketplace:
            await _dorong_proses_ke_marketplace(session, pesanan)
        else:
            # The marketplace is where this status came from; pushing it back would re-ship the order.
            pesanan.tersinkron_marketplace = True
            pesanan.catatan_sinkron = f"Status mengikuti {pesanan.platform}"
    elif status_baru == "cancelled":
        if prev == "to_ship":
            await _release_reservasi_pesanan(session, pesanan)
        pesanan.status = status_baru
    elif status_baru == "shipped":
        await _consume_reservasi_pesanan(session, pesanan)
        pesanan.status = status_baru
    else:
        pesanan.status = status_baru

    await session.flush()
    return await get_pesanan(session, pesanan.id)


def _jalur_status(dari: str, ke: str) -> list[str]:
    """Shortest chain of allowed transitions dari -> ke (excluding dari); [] if none or already there."""
    if dari == ke:
        return []
    antrean = [[dari]]
    terlihat = {dari}
    while antrean:
        jalur = antrean.pop(0)
        for nxt in sorted(_TRANSISI_STATUS.get(jalur[-1], set())):
            if nxt == ke:
                return jalur[1:] + [nxt]
            if nxt not in terlihat:
                terlihat.add(nxt)
                antrean.append(jalur + [nxt])
    return []


async def _samakan_status_pesanan(session: AsyncSession, pesanan: Pesanan, status_target: str) -> bool:
    """Walk the order forward to status_target. Returns True if the status changed.

    Never moves an order backwards. A step that fails (e.g. not enough local stock to reserve)
    is rolled back on its own and noted on the order instead of aborting the whole import.
    """
    berubah = False
    for langkah in _jalur_status(pesanan.status, status_target):
        try:
            async with session.begin_nested():
                await ubah_status_pesanan(session, pesanan.id, langkah, dorong_marketplace=False)
        except HTTPException as exc:
            pesanan.catatan_sinkron = f"Status {pesanan.platform} '{status_target}' belum bisa diterapkan: {exc.detail}"
            pesanan.tersinkron_marketplace = False
            break
        berubah = True
        await session.refresh(pesanan)
    return berubah


async def impor_pesanan_marketplace(session: AsyncSession, akun: AkunMarketplace, rows: list[dict]) -> dict:
    """Upsert orders pulled from a marketplace, keyed on (platform, id_eksternal).

    New orders are created as 'unpaid' and walked to the marketplace status through the normal
    transition table, so stock reservation/ledger behave exactly like manually entered orders.
    Existing orders only move forward; their items are never rewritten.
    """
    hasil = {"baru": 0, "diperbarui": 0, "tidak_berubah": 0, "dilewati": 0}

    kandidat = {k for r in rows for it in r["items"] for k in it["id_eksternal_kandidat"]}
    listing_by_eksternal: dict[str, ProdukListing] = {}
    if kandidat:
        stmt = select(ProdukListing).where(
            ProdukListing.platform == akun.platform, ProdukListing.id_eksternal.in_(kandidat)
        )
        for listing in (await session.execute(stmt)).scalars():
            listing_by_eksternal[listing.id_eksternal] = listing

    for row in rows:
        if row["status"] is None:
            hasil["dilewati"] += 1
            continue
        pesanan = (
            await session.execute(
                select(Pesanan).where(
                    Pesanan.platform == akun.platform, Pesanan.id_eksternal == row["id_eksternal"]
                )
            )
        ).scalar_one_or_none()
        baru = pesanan is None
        if baru:
            pesanan = Pesanan(
                platform=akun.platform,
                id_eksternal=row["id_eksternal"],
                akun_id=akun.id,
                status="unpaid",
                nama_pembeli=row["nama_pembeli"],
                total=row["total"],
                dipesan_at=row.get("dipesan_at"),
            )
            session.add(pesanan)
            await session.flush()
            for it in row["items"]:
                listing = next(
                    (listing_by_eksternal[k] for k in it["id_eksternal_kandidat"] if k in listing_by_eksternal),
                    None,
                )
                session.add(
                    ItemPesanan(
                        pesanan_id=pesanan.id,
                        produk_id=listing.produk_id if listing else None,
                        listing_id=listing.id if listing else None,
                        nama_produk=it["nama_produk"],
                        harga_satuan=it["harga_satuan"],
                        qty=it["qty"],
                        subtotal=it["harga_satuan"] * it["qty"],
                    )
                )
            await session.flush()
            pesanan = await get_pesanan(session, pesanan.id)

        if row.get("dipesan_at") and pesanan.dipesan_at is None:
            pesanan.dipesan_at = row["dipesan_at"]  # orders ingested before this column existed get filled on the next pull
        sebelum_mp = pesanan.status_marketplace
        berubah = await _samakan_status_pesanan(session, pesanan, row["status"])
        pesanan.status_marketplace = row["status_mentah"]
        if berubah and pesanan.status == row["status"]:
            # Reached the marketplace status: replace stale notes such as "waiting for the courier".
            pesanan.tersinkron_marketplace = True
            pesanan.catatan_sinkron = f"Status mengikuti {akun.platform} ({row['status_mentah']})"
        if row.get("kurir") and not pesanan.kurir:
            pesanan.kurir = row["kurir"]
        if row.get("nomor_resi") and not pesanan.nomor_resi:
            pesanan.nomor_resi = row["nomor_resi"]
        if pesanan.status in {"shipped", "completed"} and pesanan.tanggal_kirim is None:
            pesanan.tanggal_kirim = datetime.now(timezone.utc)
        if baru:
            hasil["baru"] += 1
        elif berubah or sebelum_mp != pesanan.status_marketplace:
            hasil["diperbarui"] += 1
        else:
            hasil["tidak_berubah"] += 1
    await session.flush()
    return hasil


# Even with incremental pulls, the whole 15-day window is re-read this often to repair any drift.
BATAS_SINKRON_PENUH = timedelta(hours=6)


def _aware(waktu: datetime | None) -> datetime | None:
    return waktu if waktu is None or waktu.tzinfo else waktu.replace(tzinfo=timezone.utc)


# Orders stored before the real order time was kept are completed this many per sync (Shopee rate limits).
BATAS_LENGKAPI_WAKTU_PESAN = 100


async def id_pesanan_tanpa_waktu_pesan(session: AsyncSession, akun: AkunMarketplace) -> list[str]:
    """Order numbers of this shop that still lack ``dipesan_at`` (newest first, capped per sync)."""
    hasil = await session.execute(
        select(Pesanan.id_eksternal)
        .where(Pesanan.akun_id == akun.id, Pesanan.dipesan_at.is_(None))
        .order_by(Pesanan.created_at.desc())
        .limit(BATAS_LENGKAPI_WAKTU_PESAN)
    )
    return [r for (r,) in hasil.all()]


async def sinkron_pesanan_akun(session: AsyncSession, akun: AkunMarketplace, *, penuh: bool = False) -> dict:
    """Pull one shop's orders from Shopee and import them. Returns the import counts + pulled.

    Normally incremental: only orders changed since the last successful sync are requested, which is
    a handful of API calls instead of re-reading 15 days every time. A full 15-day read happens the
    first time, when ``penuh`` is set (manual sync), or when the last full read is over 6 hours old.
    The watermark only advances when the whole sync succeeded.
    """
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    mulai = datetime.now(timezone.utc)  # taken before asking Shopee, so nothing changing meanwhile is skipped
    watermark = _aware(akun.watermark_sinkron_pesanan)
    terakhir_penuh = _aware(akun.sinkron_penuh_pesanan_at)
    perlu_penuh = penuh or watermark is None or terakhir_penuh is None or mulai - terakhir_penuh >= BATAS_SINKRON_PENUH

    rows = await erp_shopee.sync_pesanan(
        session,
        akun,
        await id_pesanan_punya_resi(session, akun),
        None if perlu_penuh else watermark,
        await id_pesanan_tanpa_waktu_pesan(session, akun),
    )
    async with session.begin_nested():  # a failure while importing leaves no half-imported shop behind
        hasil = await impor_pesanan_marketplace(session, akun, rows)
    akun.watermark_sinkron_pesanan = mulai
    if perlu_penuh:
        akun.sinkron_penuh_pesanan_at = mulai
    return {"pulled": len(rows), "penuh": perlu_penuh, **hasil}


async def klaim_sinkron_pesanan(session: AsyncSession, akun: AkunMarketplace, jeda_detik: float) -> bool:
    """Atomically claim the right to sync this shop: True only if it was not synced in the last jeda_detik.

    The claim is committed at once, so a second request (or worker) arriving a moment later is turned
    away, and a shop whose sync keeps failing is not hammered either.
    """
    sekarang = datetime.now(timezone.utc)
    kolom = AkunMarketplace.terakhir_sinkron_pesanan
    hasil = await session.execute(
        update(AkunMarketplace)
        .where(AkunMarketplace.id == akun.id, or_(kolom.is_(None), kolom < sekarang - timedelta(seconds=jeda_detik)))
        .values(terakhir_sinkron_pesanan=sekarang)
    )
    await session.commit()
    return hasil.rowcount == 1


async def sinkron_semua_pesanan(
    session: AsyncSession,
    akun_list: list[AkunMarketplace],
    *,
    jeda_detik: float = 60,
    batas_detik: float = 45,
) -> list[dict]:
    """Sync every connected Shopee shop in akun_list, one after another.

    One result dict per shop: hasil is ok / dilewati (synced recently) / ditunda (time budget used up,
    picked up by the next call) / gagal (with pesan). A failing shop never stops the others.
    """
    mulai = time.monotonic()
    keluar: list[dict] = []
    # A rollback (after one shop fails) expires every loaded object, so keep plain ids and re-load each shop.
    daftar = [(a.id, a.nama_toko) for a in akun_list]
    for akun_id, nama_toko in daftar:
        info = {"akun_id": akun_id, "nama_toko": nama_toko, "hasil": "ok", "baru": 0, "diperbarui": 0, "pesan": None}
        keluar.append(info)
        akun = await session.get(AkunMarketplace, akun_id)
        if akun is None:
            info.update(hasil="gagal", pesan="Toko tidak ditemukan.")
            continue
        if akun.status == "token_kadaluarsa":
            info.update(hasil="gagal", pesan="Token Shopee kedaluwarsa, hubungkan ulang toko.")
            continue
        if time.monotonic() - mulai >= batas_detik:
            info["hasil"] = "ditunda"
            continue
        if not await klaim_sinkron_pesanan(session, akun, jeda_detik):
            info["hasil"] = "dilewati"
            continue
        try:
            hasil = await sinkron_pesanan_akun(session, akun)
            await session.commit()
            info.update(baru=hasil["baru"], diperbarui=hasil["diperbarui"])
        except HTTPException as exc:
            await session.rollback()
            info.update(hasil="gagal", pesan=str(exc.detail))
        except Exception:  # noqa: BLE001 -- one broken shop must not break the page for the others
            await session.rollback()
            logging.getLogger(__name__).exception("sinkron pesanan gagal untuk akun %s", akun_id)
            info.update(hasil="gagal", pesan="Kesalahan tak terduga saat sinkron.")
    return keluar


async def id_pesanan_punya_resi(session: AsyncSession, akun: AkunMarketplace) -> set[str]:
    """id_eksternal of this shop's orders that already store a tracking number (skipped when pulling)."""
    stmt = select(Pesanan.id_eksternal).where(
        Pesanan.akun_id == akun.id, Pesanan.nomor_resi.is_not(None), Pesanan.nomor_resi != ""
    )
    return set((await session.execute(stmt)).scalars())


async def _pesanan_marketplace(session: AsyncSession, pesanan_id: str) -> tuple[Pesanan, AkunMarketplace]:
    """Order + shop for actions that talk to the marketplace; only orders that follow Shopee qualify."""
    pesanan = await get_pesanan(session, pesanan_id)
    if pesanan.platform != "shopee" or pesanan.status_marketplace is None or not pesanan.akun_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Hanya pesanan hasil sinkron Shopee yang bisa diproses/dicetak dari sini.",
        )
    akun = await get_akun_marketplace(session, pesanan.akun_id)
    return pesanan, akun


async def proses_pesanan_marketplace(session: AsyncSession, pesanan_id: str) -> Pesanan:
    """Arrange shipment on Shopee for a pulled order (courier pickup) and store the tracking number."""
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    pesanan, akun = await _pesanan_marketplace(session, pesanan_id)
    if pesanan.status != "to_ship":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Pesanan berstatus '{pesanan.status}', hanya pesanan 'to_ship' yang bisa diproses.",
        )
    if pesanan.status_marketplace in erp_shopee.STATUS_SUDAH_DIPROSES:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Pesanan sudah diproses di Shopee.")
    hasil = await erp_shopee.proses_pengiriman(session, akun, pesanan.id_eksternal)
    pesanan.status_marketplace = hasil["status_marketplace"]
    if hasil["nomor_resi"]:
        pesanan.nomor_resi = hasil["nomor_resi"]
    pesanan.tersinkron_marketplace = True
    pesanan.catatan_sinkron = "Diproses di Shopee, menunggu kurir pickup"
    await session.flush()
    return pesanan


async def unduh_resi_massal(
    session: AsyncSession, pesanan_ids: list[str], tipe: str | None = None, oleh: str | None = None
) -> tuple[bytes, str]:
    """One PDF with the labels of several Shopee orders that are arranged and waiting for the courier.

    Shopee prints one shop and one courier per download, so mixed selections are refused with a message
    saying how to split them. Returns (pdf, filename).
    """
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    unik = list(dict.fromkeys(pesanan_ids))
    semua = [await _pesanan_marketplace(session, pid) for pid in unik]
    belum = [p.id_eksternal for p, _ in semua if p.status_marketplace != "PROCESSED"]
    if belum:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Hanya pesanan yang sudah diproses dan menunggu kurir yang bisa dicetak. Tidak memenuhi: {', '.join(belum)}.",
        )
    kelompok = {(p.akun_id, (p.kurir or "").strip().lower()) for p, _ in semua}
    if len(kelompok) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cetak per toko dan per kurir: pilihanmu berisi toko atau kurir yang berbeda.",
        )
    akun = semua[0][1]
    pdf = await erp_shopee.unduh_resi_banyak(session, akun, [(p.id_eksternal, p.nomor_resi) for p, _ in semua], tipe)
    _tandai_dicetak([p for p, _ in semua], oleh)
    nama = f"resi-{semua[0][0].id_eksternal}.pdf" if len(semua) == 1 else f"resi-{len(semua)}-pesanan.pdf"
    return pdf, nama


async def batalkan_pesanan_marketplace(session: AsyncSession, pesanan_id: str, alasan: str) -> Pesanan:
    """Cancel a pulled Shopee order on Shopee, then locally (reserved stock is released).

    Only possible before the courier has it; Shopee has the final say and its refusal is passed on.
    """
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    pesanan, akun = await _pesanan_marketplace(session, pesanan_id)
    if pesanan.status not in {"unpaid", "to_ship"} or pesanan.status_marketplace not in erp_shopee.STATUS_BISA_DIBATALKAN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Pesanan sudah dikirim atau selesai, tidak bisa dibatalkan dari sini.",
        )
    await erp_shopee.batalkan_pesanan(session, akun, pesanan.id_eksternal, alasan)
    pesanan = await ubah_status_pesanan(session, pesanan.id, "cancelled", dorong_marketplace=False)
    pesanan.status_marketplace = "CANCELLED"
    pesanan.tersinkron_marketplace = True
    pesanan.catatan_sinkron = f"Dibatalkan di Shopee ({alasan})"
    await session.flush()
    return pesanan


def _tandai_dicetak(pesanan: list[Pesanan], oleh: str | None) -> None:
    sekarang = datetime.now(timezone.utc)
    for p in pesanan:
        p.resi_dicetak_at = sekarang
        p.resi_dicetak_oleh = oleh


async def tandai_resi_pesanan(session: AsyncSession, pesanan_id: str, dicetak: bool, oleh: str | None) -> Pesanan:
    """Mark a pulled order's label as printed, or clear the mark (e.g. it was printed but got lost)."""
    pesanan, _ = await _pesanan_marketplace(session, pesanan_id)
    if dicetak:
        _tandai_dicetak([pesanan], oleh)
    else:
        pesanan.resi_dicetak_at = None
        pesanan.resi_dicetak_oleh = None
    await session.flush()
    return pesanan


async def unduh_resi_pesanan(
    session: AsyncSession, pesanan_id: str, tipe: str | None = None, oleh: str | None = None
) -> tuple[bytes, str]:
    """Shopee's shipping label PDF for a pulled, already-processed order. Returns (pdf, filename).

    The order is marked as printed (time + who) once the PDF exists; a failed attempt leaves it unmarked."""
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    pesanan, akun = await _pesanan_marketplace(session, pesanan_id)
    if pesanan.status_marketplace not in erp_shopee.STATUS_SUDAH_DIPROSES:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Proses pesanan di Shopee dulu sebelum mencetak resi.")
    pdf = await erp_shopee.unduh_resi(session, akun, pesanan.id_eksternal, pesanan.nomor_resi, tipe)
    _tandai_dicetak([pesanan], oleh)
    return pdf, f"resi-{pesanan.id_eksternal}.pdf"


async def delete_pesanan(session: AsyncSession, pesanan_id: str) -> None:
    """Hard-delete only unpaid orders without reservations. Prefer cancel."""
    pesanan = await get_pesanan(session, pesanan_id)
    if pesanan.status != "unpaid":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Hanya pesanan unpaid yang boleh dihapus; batalkan (cancelled) untuk yang lain",
        )
    await session.delete(pesanan)
    await session.flush()


# --- Tahap 3: staff-akun scoping ----------------------------------------------


async def assign_staff_akun(session: AsyncSession, payload: StaffAkunIn) -> StaffAkunMarketplace:
    user = await session.get(UserMarketplaceErp, payload.user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User tidak ditemukan")
    await get_akun_marketplace(session, payload.akun_id)
    existing = (
        await session.execute(
            select(StaffAkunMarketplace).where(
                StaffAkunMarketplace.user_id == payload.user_id, StaffAkunMarketplace.akun_id == payload.akun_id
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Staff sudah ditugaskan ke akun ini")
    row = StaffAkunMarketplace(user_id=payload.user_id, akun_id=payload.akun_id)
    session.add(row)
    await session.flush()
    return row


async def list_staff_akun(session: AsyncSession, *, user_id: str | None = None) -> list[StaffAkunMarketplace]:
    stmt = select(StaffAkunMarketplace).order_by(StaffAkunMarketplace.created_at.desc())
    if user_id:
        stmt = stmt.where(StaffAkunMarketplace.user_id == user_id)
    return list((await session.execute(stmt)).scalars().all())


async def remove_staff_akun(session: AsyncSession, staff_akun_id: str) -> None:
    row = await session.get(StaffAkunMarketplace, staff_akun_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Penugasan staff-akun tidak ditemukan")
    await session.delete(row)
    await session.flush()


# --- Tahap 3: Pengiriman -------------------------------------------------------


async def set_pengiriman(session: AsyncSession, pesanan_id: str, payload: PengirimanIn) -> Pesanan:
    """Manual courier/AWB entry -- no courier API wired yet (see
    IDEAL_FOLLOWUPS). Only meaningful once an order has left "unpaid": it
    can't be shipped before being confirmed to_ship."""
    pesanan = await get_pesanan(session, pesanan_id)
    if pesanan.status not in {"to_ship", "shipped", "completed"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Info pengiriman hanya bisa diisi setelah pesanan berstatus to_ship",
        )
    pesanan.kurir = payload.kurir
    pesanan.nomor_resi = payload.nomor_resi
    pesanan.tanggal_kirim = payload.tanggal_kirim or datetime.now(timezone.utc)
    await session.flush()
    return await get_pesanan(session, pesanan.id)


# --- Tahap 3: Settlement --------------------------------------------------------


_SETTLEMENT_EPSILON = Decimal("1")


def _hitung_status_settlement(payload_or_row) -> str:
    expected_net = (
        payload_or_row.gross_sales
        - payload_or_row.fee_platform
        - payload_or_row.fee_payment
        - payload_or_row.ongkir_subsidi
        - payload_or_row.penalti
    )
    if abs(expected_net - payload_or_row.net) <= _SETTLEMENT_EPSILON:
        return "matched"
    return "discrepancy"


async def create_settlement(session: AsyncSession, payload: SettlementIn) -> Settlement:
    akun = await get_akun_marketplace(session, payload.akun_id)
    if payload.periode_selesai < payload.periode_mulai:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="periode_selesai sebelum periode_mulai")
    settlement = Settlement(
        akun_id=akun.id,
        platform=akun.platform,
        periode_mulai=payload.periode_mulai,
        periode_selesai=payload.periode_selesai,
        gross_sales=payload.gross_sales,
        fee_platform=payload.fee_platform,
        fee_payment=payload.fee_payment,
        ongkir_subsidi=payload.ongkir_subsidi,
        penalti=payload.penalti,
        net=payload.net,
        catatan=payload.catatan,
    )
    settlement.status = _hitung_status_settlement(settlement)
    session.add(settlement)
    await session.flush()
    return settlement


async def list_settlement(
    session: AsyncSession, *, akun_id: str | None = None, status_filter: str | None = None
) -> list[Settlement]:
    stmt = select(Settlement).order_by(Settlement.periode_mulai.desc())
    if akun_id:
        stmt = stmt.where(Settlement.akun_id == akun_id)
    if status_filter:
        stmt = stmt.where(Settlement.status == status_filter.strip().lower())
    return list((await session.execute(stmt)).scalars().all())


async def get_settlement(session: AsyncSession, settlement_id: str) -> Settlement:
    settlement = await session.get(Settlement, settlement_id)
    if not settlement:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Settlement tidak ditemukan")
    return settlement


async def update_settlement(session: AsyncSession, settlement_id: str, payload: SettlementPatch) -> Settlement:
    settlement = await get_settlement(session, settlement_id)
    data = payload.model_dump(exclude_unset=True)
    status_override = data.pop("status", None)
    for field, value in data.items():
        setattr(settlement, field, value)
    # Recompute discrepancy/matched from the numbers unless the caller is
    # explicitly promoting a reviewed row to "paid" (that's a human decision
    # the formula can't make).
    settlement.status = status_override if status_override == "paid" else _hitung_status_settlement(settlement)
    await session.flush()
    return settlement


# --- Tahap 3: Laporan ringkas ---------------------------------------------------


async def laporan_ringkas(
    session: AsyncSession, *, dari: datetime, sampai: datetime, batas_stok_kritis: int = _DEFAULT_BATAS_STOK_KRITIS
) -> dict:
    pesanan_stmt = select(Pesanan).options(selectinload(Pesanan.items)).where(
        Pesanan.created_at >= dari, Pesanan.created_at <= sampai
    )
    pesanan_rows = list((await session.execute(pesanan_stmt)).scalars().all())

    total_omzet = sum(
        (p.total for p in pesanan_rows if p.status in _STATUS_TERHITUNG_PENJUALAN), Decimal("0")
    )

    jumlah_per_status: dict[str, int] = {s: 0 for s in STATUS_PESANAN}
    for p in pesanan_rows:
        jumlah_per_status[p.status] = jumlah_per_status.get(p.status, 0) + 1

    terlaris: dict[str, dict] = {}
    for p in pesanan_rows:
        if p.status not in _STATUS_TERHITUNG_PENJUALAN:
            continue
        for item in p.items:
            key = item.produk_id or item.nama_produk
            entry = terlaris.setdefault(
                key, {"produk_id": item.produk_id, "nama_produk": item.nama_produk, "qty_terjual": 0, "omzet": Decimal("0")}
            )
            entry["qty_terjual"] += item.qty
            entry["omzet"] += item.subtotal
    produk_terlaris = sorted(terlaris.values(), key=lambda e: e["qty_terjual"], reverse=True)[:10]

    stok_kritis_rows = (
        await session.execute(
            select(Produk)
            .where(Produk.aktif.is_(True), Produk.stok <= batas_stok_kritis)
            .order_by(Produk.stok.asc())
        )
    ).scalars().all()
    stok_kritis = [
        {"produk_id": p.id, "sku_induk": p.sku_induk, "nama": p.nama, "stok": p.stok} for p in stok_kritis_rows
    ]

    return {
        "dari": dari,
        "sampai": sampai,
        "total_omzet": total_omzet,
        "jumlah_pesanan_per_status": jumlah_per_status,
        "produk_terlaris": produk_terlaris,
        "stok_kritis": stok_kritis,
    }


_ZONA_LAPORAN = "Asia/Jakarta"
_TAHAP_TERHITUNG_OMZET = ("perlu_diproses", "menunggu_kurir", "dikirim", "selesai")


async def laporan_dashboard(
    session: AsyncSession, *, dari: datetime, sampai: datetime, batas_stok_kritis: int = _DEFAULT_BATAS_STOK_KRITIS
) -> dict:
    """Everything the dashboard tables show for the period, by the date the buyer placed each order:
    totals, a row per shop (counts per stage + revenue), orders per stage, best sellers with the shops that
    sold them, a row per day and low stock. Revenue counts orders that are not unpaid or cancelled."""
    from zoneinfo import ZoneInfo

    zona = ZoneInfo(_ZONA_LAPORAN)

    def _aware(dt: datetime) -> datetime:
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    tgl = _tgl_pesanan()
    tahap_expr = _ekspresi_tahap()
    jendela = [tgl >= dari, tgl <= sampai]
    rows = (
        await session.execute(
            select(Pesanan.akun_id, tahap_expr.label("t"), Pesanan.total, tgl.label("tgl")).where(*jendela)
        )
    ).all()

    toko_rows = {
        a.id: a
        for a in await list_akun_marketplace(session)
        if a.id_toko_eksternal
    }
    per_toko: dict[str | None, dict] = {}

    def _toko(akun_id: str | None) -> dict:
        if akun_id not in per_toko:
            akun = toko_rows.get(akun_id)
            per_toko[akun_id] = {
                "akun_id": akun_id,
                "nama_toko": akun.nama_toko if akun else "(tanpa toko)",
                "pesanan": 0,
                "omzet": Decimal("0"),
                **{t: 0 for t in TAHAP_PESANAN},
                "pesanan_terbaru": None,
            }
        return per_toko[akun_id]

    for akun_id in toko_rows:
        _toko(akun_id)  # shops without orders still get a (zero) row

    per_tahap = {t: {"tahap": t, "jumlah": 0, "nilai": Decimal("0")} for t in TAHAP_PESANAN}
    per_hari: dict = {}
    total_omzet = Decimal("0")
    total_pesanan = 0
    for akun_id, t, total, waktu in rows:
        entry = _toko(akun_id)
        entry[t] += 1
        per_tahap[t]["jumlah"] += 1
        per_tahap[t]["nilai"] += total
        if waktu is not None and (entry["pesanan_terbaru"] is None or _aware(waktu) > entry["pesanan_terbaru"]):
            entry["pesanan_terbaru"] = _aware(waktu)
        if t in _TAHAP_TERHITUNG_OMZET:
            entry["pesanan"] += 1
            entry["omzet"] += total
            total_pesanan += 1
            total_omzet += total
            if waktu is not None:
                hari = per_hari.setdefault(_aware(waktu).astimezone(zona).date(), {"pesanan": 0, "omzet": Decimal("0")})
                hari["pesanan"] += 1
                hari["omzet"] += total

    terlaris_rows = (
        await session.execute(
            select(
                ItemPesanan.nama_produk,
                Pesanan.akun_id,
                func.sum(ItemPesanan.qty),
                func.sum(ItemPesanan.subtotal),
                func.count(func.distinct(Pesanan.id)),
            )
            .join(Pesanan, Pesanan.id == ItemPesanan.pesanan_id)
            .where(*jendela, Pesanan.status.in_(_STATUS_TERHITUNG_PENJUALAN))
            .group_by(ItemPesanan.nama_produk, Pesanan.akun_id)
        )
    ).all()
    produk: dict[str, dict] = {}
    for nama, akun_id, qty, omzet, jumlah in terlaris_rows:
        e = produk.setdefault(nama, {"nama_produk": nama, "qty_terjual": 0, "omzet": Decimal("0"), "pesanan": 0, "toko": []})
        e["qty_terjual"] += int(qty)
        e["omzet"] += omzet
        e["pesanan"] += int(jumlah)
        akun = toko_rows.get(akun_id)
        e["toko"].append({"nama_toko": akun.nama_toko if akun else "(tanpa toko)", "qty": int(qty)})
    produk_terlaris = sorted(produk.values(), key=lambda e: (-e["qty_terjual"], e["nama_produk"]))[:20]
    for e in produk_terlaris:
        e["toko"].sort(key=lambda x: -x["qty"])

    data_sejak = (await session.execute(select(func.min(tgl)))).scalar_one_or_none()

    stok_kritis = [
        {"produk_id": p.id, "sku_induk": p.sku_induk, "nama": p.nama, "stok": p.stok}
        for p in (
            await session.execute(
                select(Produk).where(Produk.aktif.is_(True), Produk.stok <= batas_stok_kritis).order_by(Produk.stok.asc())
            )
        ).scalars()
    ]
    return {
        "dari": dari,
        "sampai": sampai,
        "data_sejak": _aware(data_sejak) if data_sejak else None,
        "total_omzet": total_omzet,
        "total_pesanan": total_pesanan,
        "rata_rata_pesanan": (total_omzet / total_pesanan) if total_pesanan else Decimal("0"),
        "jumlah_toko": len(toko_rows),
        "per_toko": sorted(per_toko.values(), key=lambda e: (-e["omzet"], e["nama_toko"].lower())),
        "per_tahap": list(per_tahap.values()),
        "produk_terlaris": produk_terlaris,
        "per_hari": [
            {"tanggal": d, **v} for d, v in sorted(per_hari.items(), reverse=True)
        ],
        "stok_kritis": stok_kritis,
    }


# --- Settlement per order, pulled from Shopee (get_escrow_list / get_escrow_detail) -------------

KUNCI_URUT_SETTLEMENT = ("dirilis", "pesanan", "toko", "penjualan", "komisi", "layanan", "ongkir", "cair")
_KOLOM_UANG_SETTLEMENT = (
    "jumlah_cair", "penjualan", "voucher_penjual", "komisi", "layanan", "transaksi", "ongkir", "subsidi_ongkir", "penyesuaian",
)
SETTLEMENT_HARI_MAKS = 90


def settlement_pesanan_out(r: SettlementPesanan, nama_toko: str | None = None) -> dict:
    return {
        "id": r.id,
        "akun_id": r.akun_id,
        "nama_toko": nama_toko,
        "order_sn": r.order_sn,
        "dirilis_at": r.dirilis_at,
        **{k: getattr(r, k) for k in _KOLOM_UANG_SETTLEMENT},
    }


async def simpan_settlement_pesanan(session: AsyncSession, akun: AkunMarketplace, rows: list[dict]) -> dict:
    """Insert released-order rows for one shop; a stored (shop, order) is refreshed, never duplicated."""
    ada = {
        r.order_sn: r
        for r in (
            await session.execute(
                select(SettlementPesanan).where(
                    SettlementPesanan.akun_id == akun.id, SettlementPesanan.order_sn.in_([x["order_sn"] for x in rows] or [""])
                )
            )
        ).scalars()
    }
    baru = diperbarui = 0
    for row in rows:
        rec = ada.get(row["order_sn"])
        if rec is None:
            session.add(SettlementPesanan(akun_id=akun.id, **row))
            baru += 1
        else:
            for k, v in row.items():
                setattr(rec, k, v)
            diperbarui += 1
    await session.flush()
    return {"baru": baru, "diperbarui": diperbarui}


async def sinkron_settlement_akun(session: AsyncSession, akun: AkunMarketplace, hari: int = 15) -> dict:
    """Pull what Shopee released to this shop in the last ``hari`` days (1..90). Orders already stored are not
    read again; a big backlog is taken in parts (``sisa`` > 0 means: pull again)."""
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    if not 1 <= hari <= SETTLEMENT_HARI_MAKS:
        raise HTTPException(status_code=400, detail=f"hari harus 1 sampai {SETTLEMENT_HARI_MAKS}")
    sekarang = int(time.time())
    sudah = set(
        (await session.execute(select(SettlementPesanan.order_sn).where(SettlementPesanan.akun_id == akun.id))).scalars()
    )
    hasil = await erp_shopee.sync_settlement(session, akun, sekarang - hari * 86400, sekarang, sudah)
    simpan = await simpan_settlement_pesanan(session, akun, hasil["rows"])
    return {"ditemukan": hasil["ditemukan"], "sisa": hasil["sisa"], **simpan}


def _kondisi_settlement(akun_id: str | None, dari: datetime | None, sampai: datetime | None, q: str | None) -> list:
    cond = []
    if akun_id:
        cond.append(SettlementPesanan.akun_id == akun_id)
    if dari:
        cond.append(SettlementPesanan.dirilis_at >= dari)
    if sampai:
        cond.append(SettlementPesanan.dirilis_at <= sampai)
    if q and q.strip():
        cond.append(func.lower(SettlementPesanan.order_sn).like(f"%{q.strip().lower()}%"))
    return cond


async def list_settlement_pesanan(
    session: AsyncSession,
    *,
    akun_id: str | None = None,
    dari: datetime | None = None,
    sampai: datetime | None = None,
    q: str | None = None,
    urut: str = "dirilis:desc",
    halaman: int = 1,
    per_halaman: int = 50,
) -> dict:
    kunci, turun = _urut_kunci(urut, sah=KUNCI_URUT_SETTLEMENT)
    kolom = {
        "dirilis": SettlementPesanan.dirilis_at,
        "pesanan": SettlementPesanan.order_sn,
        "toko": func.lower(AkunMarketplace.nama_toko),
        "penjualan": SettlementPesanan.penjualan,
        "komisi": SettlementPesanan.komisi,
        "layanan": SettlementPesanan.layanan,
        "ongkir": SettlementPesanan.ongkir,
        "cair": SettlementPesanan.jumlah_cair,
    }[kunci]
    cond = _kondisi_settlement(akun_id, dari, sampai, q)
    total = int((await session.execute(select(func.count()).select_from(SettlementPesanan).where(*cond))).scalar_one())
    stmt = (
        select(SettlementPesanan, AkunMarketplace.nama_toko)
        .join(AkunMarketplace, AkunMarketplace.id == SettlementPesanan.akun_id)
        .where(*cond)
        .order_by((kolom.desc() if turun else kolom.asc()).nulls_last(), SettlementPesanan.id)
        .offset((max(halaman, 1) - 1) * per_halaman)
        .limit(per_halaman)
    )
    rows = (await session.execute(stmt)).all()
    return {
        "total": total,
        "halaman": halaman,
        "per_halaman": per_halaman,
        "items": [settlement_pesanan_out(r, n) for r, n in rows],
    }


async def ringkasan_settlement_pesanan(
    session: AsyncSession, *, dari: datetime | None = None, sampai: datetime | None = None, q: str | None = None
) -> dict:
    """Totals per shop (and overall) for the released orders in the period: the table above the order list."""
    cond = _kondisi_settlement(None, dari, sampai, q)
    jumlah = [func.coalesce(func.sum(getattr(SettlementPesanan, k)), 0) for k in _KOLOM_UANG_SETTLEMENT]
    stmt = (
        select(SettlementPesanan.akun_id, AkunMarketplace.nama_toko, func.count(), *jumlah, func.max(SettlementPesanan.dirilis_at))
        .join(AkunMarketplace, AkunMarketplace.id == SettlementPesanan.akun_id)
        .where(*cond)
        .group_by(SettlementPesanan.akun_id, AkunMarketplace.nama_toko)
        .order_by(func.lower(AkunMarketplace.nama_toko))
    )
    toko = []
    total = {"pesanan": 0, **{k: Decimal("0") for k in _KOLOM_UANG_SETTLEMENT}}
    for akun_id, nama, n, *angka, terakhir in (await session.execute(stmt)).all():
        baris = {"akun_id": akun_id, "nama_toko": nama, "pesanan": int(n), "dirilis_terakhir": terakhir}
        for k, v in zip(_KOLOM_UANG_SETTLEMENT, angka):
            baris[k] = Decimal(str(v))
            total[k] += baris[k]
        total["pesanan"] += baris["pesanan"]
        toko.append(baris)
    return {"toko": toko, "total": total}


# --- Tahap 4: Iklan (ads) -------------------------------------------------------

# Linear campaign lifecycle: draft -> aktif <-> dijeda -> selesai. No path
# back from selesai (matches the "finished" semantics of a real ad platform).
_TRANSISI_STATUS_IKLAN = {
    "draft": {"aktif", "selesai"},
    "aktif": {"dijeda", "selesai"},
    "dijeda": {"aktif", "selesai"},
    "selesai": set(),
}


async def create_campaign(session: AsyncSession, payload: IklanCampaignIn) -> IklanCampaign:
    akun = await get_akun_marketplace(session, payload.akun_id)
    if payload.produk_id:
        await get_produk(session, payload.produk_id)
    if payload.tanggal_selesai and payload.tanggal_selesai < payload.tanggal_mulai:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="tanggal_selesai sebelum tanggal_mulai")
    campaign = IklanCampaign(
        akun_id=akun.id,
        platform=akun.platform,
        produk_id=payload.produk_id,
        nama=payload.nama,
        budget_harian=payload.budget_harian,
        tanggal_mulai=payload.tanggal_mulai,
        tanggal_selesai=payload.tanggal_selesai,
        catatan=payload.catatan,
    )
    session.add(campaign)
    await session.flush()
    return campaign


async def list_campaign(
    session: AsyncSession, *, akun_id: str | None = None, platform: str | None = None, status_filter: str | None = None
) -> list[IklanCampaign]:
    stmt = select(IklanCampaign).order_by(IklanCampaign.created_at.desc())
    if akun_id:
        stmt = stmt.where(IklanCampaign.akun_id == akun_id)
    if platform:
        stmt = stmt.where(IklanCampaign.platform == _validate_platform(platform))
    if status_filter:
        status_filter = status_filter.strip().lower()
        if status_filter not in STATUS_IKLAN:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status iklan tidak dikenal")
        stmt = stmt.where(IklanCampaign.status == status_filter)
    return list((await session.execute(stmt)).scalars().all())


async def get_campaign(session: AsyncSession, campaign_id: str) -> IklanCampaign:
    campaign = await session.get(IklanCampaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign iklan tidak ditemukan")
    return campaign


async def update_campaign(session: AsyncSession, campaign_id: str, payload: IklanCampaignPatch) -> IklanCampaign:
    campaign = await get_campaign(session, campaign_id)
    data = payload.model_dump(exclude_unset=True)
    status_baru = data.pop("status", None)
    for field, value in data.items():
        setattr(campaign, field, value)
    if status_baru is not None:
        status_baru = status_baru.strip().lower()
        if status_baru not in STATUS_IKLAN:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status iklan tidak dikenal")
        if status_baru not in _TRANSISI_STATUS_IKLAN.get(campaign.status, set()):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Tidak bisa ubah status dari '{campaign.status}' ke '{status_baru}'",
            )
        campaign.status = status_baru
    await session.flush()
    return campaign


async def delete_campaign(session: AsyncSession, campaign_id: str) -> None:
    campaign = await get_campaign(session, campaign_id)
    if campaign.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Hanya campaign berstatus draft yang boleh dihapus; hentikan (selesai) untuk yang lain",
        )
    await session.delete(campaign)
    await session.flush()


async def record_metrik_harian(
    session: AsyncSession, campaign_id: str, payload: IklanMetrikHarianIn
) -> IklanMetrikHarian:
    """Upsert on (campaign_id, tanggal) -- re-entering the same day corrects
    it rather than duplicating, since this is manual entry from the
    platform's own ads dashboard and typos happen."""
    await get_campaign(session, campaign_id)
    existing = (
        await session.execute(
            select(IklanMetrikHarian).where(
                IklanMetrikHarian.campaign_id == campaign_id, IklanMetrikHarian.tanggal == payload.tanggal
            )
        )
    ).scalar_one_or_none()
    if existing:
        existing.impression = payload.impression
        existing.klik = payload.klik
        existing.biaya = payload.biaya
        await session.flush()
        return existing
    metrik = IklanMetrikHarian(
        campaign_id=campaign_id,
        tanggal=payload.tanggal,
        impression=payload.impression,
        klik=payload.klik,
        biaya=payload.biaya,
    )
    session.add(metrik)
    await session.flush()
    return metrik


async def list_metrik_harian(session: AsyncSession, campaign_id: str) -> list[IklanMetrikHarian]:
    stmt = (
        select(IklanMetrikHarian)
        .where(IklanMetrikHarian.campaign_id == campaign_id)
        .order_by(IklanMetrikHarian.tanggal.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def laporan_iklan(session: AsyncSession, campaign_id: str, *, dari: datetime, sampai: datetime) -> dict:
    """ROAS report for one campaign. omzet_atribusi comes from *actual*
    ItemPesanan/Pesanan data for the campaign's linked produk_id in the
    window -- not a number the owner enters -- which is what makes this an
    ERP feature rather than a standalone ad-spend tracker."""
    campaign = await get_campaign(session, campaign_id)

    metrik_stmt = select(IklanMetrikHarian).where(
        IklanMetrikHarian.campaign_id == campaign_id,
        IklanMetrikHarian.tanggal >= dari,
        IklanMetrikHarian.tanggal <= sampai,
    )
    metrik_rows = list((await session.execute(metrik_stmt)).scalars().all())
    total_impression = sum((m.impression for m in metrik_rows), 0)
    total_klik = sum((m.klik for m in metrik_rows), 0)
    total_biaya = sum((m.biaya for m in metrik_rows), Decimal("0"))
    ctr = (Decimal(total_klik) / Decimal(total_impression) * 100) if total_impression else Decimal("0")

    omzet_atribusi = Decimal("0")
    if campaign.produk_id:
        item_stmt = (
            select(func.coalesce(func.sum(ItemPesanan.subtotal), 0))
            .select_from(ItemPesanan)
            .join(Pesanan, Pesanan.id == ItemPesanan.pesanan_id)
            .where(
                ItemPesanan.produk_id == campaign.produk_id,
                Pesanan.status.in_(_STATUS_TERHITUNG_PENJUALAN),
                Pesanan.created_at >= dari,
                Pesanan.created_at <= sampai,
            )
        )
        omzet_atribusi = Decimal(str((await session.execute(item_stmt)).scalar_one()))

    roas = (omzet_atribusi / total_biaya) if total_biaya else None

    return {
        "campaign_id": campaign.id,
        "dari": dari,
        "sampai": sampai,
        "total_impression": total_impression,
        "total_klik": total_klik,
        "ctr": ctr,
        "total_biaya": total_biaya,
        "omzet_atribusi": omzet_atribusi,
        "roas": roas,
    }


# Re-export reason tuple for tests / docs
__all_reasons__ = REASON_STOK_LEDGER
