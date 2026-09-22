"""Exercises the toko checkout/order business logic against a real (SQLite,
in-memory) async session -- not just import checks -- since stock validation
and status transitions are the parts most worth catching regressions in.
"""
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.toko.modules.toko.application import services
from tenants.toko.modules.toko.infrastructure.database import TokoBase
from tenants.toko.modules.toko.infrastructure.models import ProdukToko, UserToko


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(TokoBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    await engine.dispose()


async def _make_user(session, email="pembeli@test.com") -> UserToko:
    user = UserToko(nama="Pembeli Test", email=email, password_hash="x", role="pembeli")
    session.add(user)
    await session.flush()
    return user


async def _make_produk(session, *, nama="Beras 5kg", harga=Decimal("65000.00"), stok=10) -> ProdukToko:
    produk = ProdukToko(nama=nama, harga=harga, stok=stok)
    session.add(produk)
    await session.flush()
    return produk


@pytest.mark.asyncio
async def test_checkout_decrements_stock_and_clears_cart(session):
    user = await _make_user(session)
    produk = await _make_produk(session, stok=10)

    await services.tambah_ke_keranjang(session, user.id, produk.id, 3)
    pesanan = await services.checkout(session, user.id)

    assert pesanan.status == "menunggu_pembayaran"
    assert str(pesanan.total) == "195000.00"
    assert len(pesanan.items) == 1
    assert pesanan.items[0].qty == 3

    await session.refresh(produk)
    assert produk.stok == 7

    keranjang_setelah = await services.get_keranjang(session, user.id)
    assert keranjang_setelah == []


@pytest.mark.asyncio
async def test_checkout_rejects_when_stock_insufficient(session):
    user = await _make_user(session)
    produk = await _make_produk(session, stok=2)

    await services.tambah_ke_keranjang(session, user.id, produk.id, 5)

    with pytest.raises(Exception) as exc_info:
        await services.checkout(session, user.id)
    assert "Stok" in str(exc_info.value.detail)

    # Stock and cart must be untouched after a rejected checkout.
    await session.refresh(produk)
    assert produk.stok == 2
    assert len(await services.get_keranjang(session, user.id)) == 1


@pytest.mark.asyncio
async def test_checkout_rejects_empty_cart(session):
    user = await _make_user(session)
    with pytest.raises(Exception) as exc_info:
        await services.checkout(session, user.id)
    assert "kosong" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_status_transitions_follow_allowed_path(session):
    user = await _make_user(session)
    produk = await _make_produk(session, stok=5)
    await services.tambah_ke_keranjang(session, user.id, produk.id, 1)
    pesanan = await services.checkout(session, user.id)

    pesanan = await services.ubah_status_pesanan(session, pesanan.id, "dibayar")
    assert pesanan.status == "dibayar"

    with pytest.raises(Exception) as exc_info:
        await services.ubah_status_pesanan(session, pesanan.id, "dikirim")
    assert "Tidak bisa ubah status" in str(exc_info.value.detail)

    pesanan = await services.ubah_status_pesanan(session, pesanan.id, "diproses")
    assert pesanan.status == "diproses"
