"""Exercises the marketplace_erp tenant foundation (Tahap 1) -- auth, akun
marketplace CRUD, produk (SKU induk) CRUD and produk listing mapping --
against a real (SQLite, in-memory) async session sharing
MarketplaceErpBase's metadata, same pattern as tests/test_toko_erp_marketplace.py.
"""
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import (
    AkunMarketplaceIn,
    LoginIn,
    ProdukIn,
    ProdukListingIn,
    RegisterIn,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    await engine.dispose()


# --- Auth --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_then_login(session):
    await services.register_user(session, RegisterIn(nama="Owner", email="owner@test.com", password="rahasia123"))
    user = await services.authenticate_user(session, LoginIn(email="owner@test.com", password="rahasia123"))
    assert user.role == "owner"


@pytest.mark.asyncio
async def test_register_rejects_duplicate_email(session):
    await services.register_user(session, RegisterIn(nama="Owner", email="owner@test.com", password="rahasia123"))
    with pytest.raises(HTTPException) as exc_info:
        await services.register_user(session, RegisterIn(nama="Owner2", email="owner@test.com", password="lainnya"))
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_login_rejects_wrong_password(session):
    await services.register_user(session, RegisterIn(nama="Owner", email="owner@test.com", password="rahasia123"))
    with pytest.raises(HTTPException) as exc_info:
        await services.authenticate_user(session, LoginIn(email="owner@test.com", password="salah"))
    assert exc_info.value.status_code == 401


# --- Akun Marketplace ----------------------------------------------------------


@pytest.mark.asyncio
async def test_create_akun_marketplace_rejects_unknown_platform(session):
    with pytest.raises(HTTPException) as exc_info:
        await services.create_akun_marketplace(
            session, AkunMarketplaceIn(platform="amazon", nama_toko="Toko Test")
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_create_akun_marketplace_rejects_duplicate_shop_id(session):
    await services.create_akun_marketplace(
        session, AkunMarketplaceIn(platform="shopee", nama_toko="Toko A", id_toko_eksternal="SHOP-1")
    )
    with pytest.raises(HTTPException) as exc_info:
        await services.create_akun_marketplace(
            session, AkunMarketplaceIn(platform="shopee", nama_toko="Toko B", id_toko_eksternal="SHOP-1")
        )
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_create_akun_marketplace_allows_multiple_pending_without_shop_id(session):
    a = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="Toko A"))
    b = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="Toko B"))
    assert a.id != b.id
    akun_list = await services.list_akun_marketplace(session, platform="shopee")
    assert len(akun_list) == 2


# --- Produk (SKU induk) + Listing -----------------------------------------------


@pytest.mark.asyncio
async def test_create_produk_rejects_duplicate_sku_induk(session):
    await services.create_produk(
        session, ProdukIn(sku_induk="SKU-001", nama="Sabun Cuci", harga_dasar=Decimal("15000"), stok=100)
    )
    with pytest.raises(HTTPException) as exc_info:
        await services.create_produk(
            session, ProdukIn(sku_induk="SKU-001", nama="Sabun Cuci Lain", harga_dasar=Decimal("16000"), stok=50)
        )
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_produk_listing_maps_one_produk_to_many_platforms(session):
    produk = await services.create_produk(
        session, ProdukIn(sku_induk="SKU-002", nama="Sampo", harga_dasar=Decimal("20000"), stok=50)
    )
    akun_shopee = await services.create_akun_marketplace(
        session, AkunMarketplaceIn(platform="shopee", nama_toko="Toko Shopee")
    )
    akun_tiktok = await services.create_akun_marketplace(
        session, AkunMarketplaceIn(platform="tiktokshop", nama_toko="Toko TikTok")
    )

    await services.create_listing(
        session,
        ProdukListingIn(produk_id=produk.id, akun_id=akun_shopee.id, platform="shopee", id_eksternal="SHP-ITEM-1"),
    )
    await services.create_listing(
        session,
        ProdukListingIn(
            produk_id=produk.id, akun_id=akun_tiktok.id, platform="tiktokshop", id_eksternal="TTS-ITEM-1"
        ),
    )

    listing = await services.list_listing(session, produk_id=produk.id)
    assert {row.platform for row in listing} == {"shopee", "tiktokshop"}


@pytest.mark.asyncio
async def test_create_listing_rejects_duplicate_platform_id_eksternal(session):
    produk = await services.create_produk(
        session, ProdukIn(sku_induk="SKU-003", nama="Deterjen", harga_dasar=Decimal("25000"), stok=30)
    )
    akun = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="lazada", nama_toko="Toko L"))
    await services.create_listing(
        session, ProdukListingIn(produk_id=produk.id, akun_id=akun.id, platform="lazada", id_eksternal="LZD-1")
    )
    with pytest.raises(HTTPException) as exc_info:
        await services.create_listing(
            session, ProdukListingIn(produk_id=produk.id, akun_id=akun.id, platform="lazada", id_eksternal="LZD-1")
        )
    assert exc_info.value.status_code == 409
