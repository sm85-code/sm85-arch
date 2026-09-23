"""Exercises AkunMarketplace (multi-shop-per-platform support) in the
toko-erp submodule -- create/list/update/delete, duplicate
(platform, id_toko_eksternal) rejection, akun_id required on
produk/pesanan/percakapan create, platform-mismatch rejection, and
akun_id-based list filters. Same in-memory SQLite pattern as
tests/test_toko_erp_marketplace.py.
"""
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.toko.modules.erp.application import services as erp_services
from tenants.toko.modules.erp.application.schemas import (
    AkunMarketplaceIn,
    AkunMarketplacePatch,
    PercakapanERPIn,
    ProdukERPIn,
)
from tenants.toko.modules.toko.infrastructure.database import TokoBase


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(TokoBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_akun_marketplace(session):
    akun = await erp_services.create_akun_marketplace(
        session, AkunMarketplaceIn(platform="shopee", nama_toko="Toko Sembako A - Shopee")
    )
    assert akun.platform == "shopee"
    assert akun.status == "aktif"
    out = erp_services.akun_out(akun)
    assert out["nama_toko"] == "Toko Sembako A - Shopee"
    assert "access_token" not in out
    assert "refresh_token" not in out


@pytest.mark.asyncio
async def test_create_akun_marketplace_rejects_unknown_platform(session):
    with pytest.raises(HTTPException) as exc_info:
        await erp_services.create_akun_marketplace(session, AkunMarketplaceIn(platform="tokopedia", nama_toko="X"))
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_create_akun_marketplace_rejects_duplicate_id_toko_eksternal(session):
    await erp_services.create_akun_marketplace(
        session, AkunMarketplaceIn(platform="shopee", nama_toko="Toko A", id_toko_eksternal="SHOP-1")
    )
    with pytest.raises(HTTPException) as exc_info:
        await erp_services.create_akun_marketplace(
            session, AkunMarketplaceIn(platform="shopee", nama_toko="Toko A duplikat", id_toko_eksternal="SHOP-1")
        )
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_create_akun_marketplace_allows_multiple_without_id_toko_eksternal(session):
    # Several not-yet-authorized accounts on the same platform (nullable
    # id_toko_eksternal) must NOT collide with each other.
    a1 = await erp_services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="Toko A"))
    a2 = await erp_services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="Toko B"))
    assert a1.id != a2.id


@pytest.mark.asyncio
async def test_update_akun_marketplace_sets_credentials_and_status(session):
    akun = await erp_services.create_akun_marketplace(session, AkunMarketplaceIn(platform="lazada", nama_toko="Toko L"))
    updated = await erp_services.update_akun_marketplace(
        session,
        akun.id,
        AkunMarketplacePatch(status="nonaktif", access_token="tok-abc", refresh_token="ref-abc", id_toko_eksternal="LZD-SHOP-1"),
    )
    assert updated.status == "nonaktif"
    assert updated.access_token == "tok-abc"
    out = erp_services.akun_out(updated)
    assert out["sudah_terautentikasi"] is True
    assert "access_token" not in out


@pytest.mark.asyncio
async def test_update_akun_marketplace_duplicate_id_toko_eksternal_rejected(session):
    a1 = await erp_services.create_akun_marketplace(
        session, AkunMarketplaceIn(platform="blibli", nama_toko="Toko 1", id_toko_eksternal="BLI-1")
    )
    a2 = await erp_services.create_akun_marketplace(session, AkunMarketplaceIn(platform="blibli", nama_toko="Toko 2"))
    with pytest.raises(HTTPException) as exc_info:
        await erp_services.update_akun_marketplace(session, a2.id, AkunMarketplacePatch(id_toko_eksternal="BLI-1"))
    assert exc_info.value.status_code == 409
    assert a1.id_toko_eksternal == "BLI-1"


@pytest.mark.asyncio
async def test_delete_akun_marketplace(session):
    akun = await erp_services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="Toko X"))
    await erp_services.delete_akun_marketplace(session, akun.id)
    with pytest.raises(HTTPException) as exc_info:
        await erp_services.get_akun_marketplace(session, akun.id)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_list_akun_marketplace_filters_by_platform(session):
    await erp_services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="S1"))
    await erp_services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="S2"))
    await erp_services.create_akun_marketplace(session, AkunMarketplaceIn(platform="lazada", nama_toko="L1"))

    semua = await erp_services.list_akun_marketplace(session)
    assert len(semua) == 3

    hanya_shopee = await erp_services.list_akun_marketplace(session, platform="shopee")
    assert len(hanya_shopee) == 2


# --- akun_id required / validated on produk, pesanan, percakapan create ----


@pytest.mark.asyncio
async def test_produk_erp_in_requires_akun_id():
    with pytest.raises(ValidationError):
        ProdukERPIn(platform="shopee", id_eksternal="X-1", nama="Sabun", harga=Decimal("5000"))


@pytest.mark.asyncio
async def test_create_produk_erp_rejects_unknown_akun_id(session):
    with pytest.raises(HTTPException) as exc_info:
        await erp_services.create_produk_erp(
            session,
            ProdukERPIn(platform="shopee", akun_id="tidak-ada", id_eksternal="X-1", nama="Sabun", harga=Decimal("5000")),
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_create_produk_erp_rejects_mismatched_platform(session):
    akun_shopee = await erp_services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="S"))
    with pytest.raises(HTTPException) as exc_info:
        await erp_services.create_produk_erp(
            session,
            ProdukERPIn(platform="lazada", akun_id=akun_shopee.id, id_eksternal="X-2", nama="Sabun", harga=Decimal("5000")),
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_terima_pesanan_erp_requires_valid_matching_akun(session):
    akun_shopee = await erp_services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="S"))

    pesanan = await erp_services.terima_pesanan_erp(
        session, platform="shopee", akun_id=akun_shopee.id, id_eksternal="S-100", nama_pembeli="Budi"
    )
    assert pesanan.akun_id == akun_shopee.id

    with pytest.raises(HTTPException) as exc_info:
        await erp_services.terima_pesanan_erp(session, platform="lazada", akun_id=akun_shopee.id, id_eksternal="L-1")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_percakapan_erp_in_requires_akun_id():
    with pytest.raises(ValidationError):
        PercakapanERPIn(platform="shopee", id_eksternal_pembeli="buyer-1")


@pytest.mark.asyncio
async def test_get_or_create_percakapan_erp_rejects_mismatched_platform(session):
    akun_shopee = await erp_services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="S"))
    with pytest.raises(HTTPException) as exc_info:
        await erp_services.get_or_create_percakapan_erp(
            session, PercakapanERPIn(platform="lazada", akun_id=akun_shopee.id, id_eksternal_pembeli="buyer-9")
        )
    assert exc_info.value.status_code == 400
