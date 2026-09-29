from __future__ import annotations

from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.security import hash_password, verify_password
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import (
    AkunMarketplaceIn,
    AkunMarketplacePatch,
    ItemPesananIn,
    LoginIn,
    PesananIn,
    ProdukIn,
    ProdukListingIn,
    ProdukListingPatch,
    ProdukPatch,
    RegisterIn,
    StokAdjustIn,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    DEFAULT_GUDANG_KODE,
    PLATFORM_MARKETPLACE,
    REASON_STOK_LEDGER,
    STATUS_PESANAN,
    AkunMarketplace,
    Gudang,
    ItemPesanan,
    Pesanan,
    Produk,
    ProdukListing,
    StokLedger,
    StokReservasi,
    UserMarketplaceErp,
)

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


async def register_user(session: AsyncSession, payload: RegisterIn) -> UserMarketplaceErp:
    existing = (
        await session.execute(select(UserMarketplaceErp).where(UserMarketplaceErp.email == payload.email))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email sudah terdaftar")
    user = UserMarketplaceErp(
        nama=payload.nama,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role="owner",
    )
    session.add(user)
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
    )
    session.add(produk)
    await session.flush()
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


async def ubah_status_pesanan(session: AsyncSession, pesanan_id: str, status_baru: str) -> Pesanan:
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
        await _dorong_proses_ke_marketplace(session, pesanan)
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


# Re-export reason tuple for tests / docs
__all_reasons__ = REASON_STOK_LEDGER
