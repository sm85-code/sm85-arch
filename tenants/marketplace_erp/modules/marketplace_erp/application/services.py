from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.security import hash_password, verify_password
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import (
    AkunMarketplaceIn,
    AkunMarketplacePatch,
    LoginIn,
    ProdukIn,
    ProdukListingIn,
    ProdukListingPatch,
    ProdukPatch,
    RegisterIn,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    PLATFORM_MARKETPLACE,
    AkunMarketplace,
    Produk,
    ProdukListing,
    UserMarketplaceErp,
)


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
    """Service-layer stand-in for a partial unique constraint: (platform,
    id_toko_eksternal) must be unique only while id_toko_eksternal is set --
    rows still pending OAuth may share a NULL shop id. Same reasoning as
    tenants/toko/modules/erp/application/services.py."""
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
    return produk


async def update_produk(session: AsyncSession, produk_id: str, payload: ProdukPatch) -> Produk:
    produk = await get_produk(session, produk_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(produk, field, value)
    await session.flush()
    return produk


async def delete_produk(session: AsyncSession, produk_id: str) -> None:
    produk = await get_produk(session, produk_id)
    await session.delete(produk)
    await session.flush()


# --- Produk Listing (mapping SKU induk -> listing per platform) ---------------


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
