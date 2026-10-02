from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
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
    Pesanan,
    Produk,
    ProdukListing,
    Settlement,
    StaffAkunMarketplace,
    StokLedger,
    StokReservasi,
    UserMarketplaceErp,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.seeder import DEFAULT_PASSWORD

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


async def _cek_email_belum_terdaftar(session: AsyncSession, email: str) -> None:
    existing = (
        await session.execute(select(UserMarketplaceErp).where(UserMarketplaceErp.email == email))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email sudah terdaftar")


async def register_user(session: AsyncSession, payload: RegisterIn) -> UserMarketplaceErp:
    """Legacy self-registration (always role=owner).

    Only reachable through POST /auth/register when the operator explicitly
    sets MARKETPLACE_ERP_ALLOW_REGISTER=true -- see the router gate.
    """
    await _cek_email_belum_terdaftar(session, payload.email)
    user = UserMarketplaceErp(
        nama=payload.nama,
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
    await _cek_email_belum_terdaftar(session, payload.email)
    user = UserMarketplaceErp(
        nama=payload.nama.strip(),
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=payload.role,
        must_change_password=True,
    )
    session.add(user)
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
    user = (
        await session.execute(select(UserMarketplaceErp).where(UserMarketplaceErp.email == payload.email))
    ).scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email atau password salah")
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


async def delete_akun_marketplace(session: AsyncSession, akun_id: str) -> None:
    akun = await get_akun_marketplace(session, akun_id)
    await session.delete(akun)
    await session.flush()


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
    produk = await get_produk(session, produk_id)
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


async def unduh_resi_pesanan(session: AsyncSession, pesanan_id: str) -> tuple[bytes, str]:
    """Shopee's shipping label PDF for a pulled, already-processed order. Returns (pdf, filename)."""
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    pesanan, akun = await _pesanan_marketplace(session, pesanan_id)
    if pesanan.status_marketplace not in erp_shopee.STATUS_SUDAH_DIPROSES:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Proses pesanan di Shopee dulu sebelum mencetak resi.")
    pdf = await erp_shopee.unduh_resi(session, akun, pesanan.id_eksternal, pesanan.nomor_resi)
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
