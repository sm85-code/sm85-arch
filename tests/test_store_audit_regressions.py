"""Focused regressions for stale inventory, atomic checkout and master families."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tenants.store.modules.store.application import services
from tenants.store.modules.store.application.schemas import ProdukIn, ProdukPatch, CheckoutIn, PengirimanIn
from tenants.store.modules.store.application.order_expiry import expire_idle
from tenants.store.modules.store.application.erp_publish import publish_master
from tenants.store.modules.store.infrastructure.database import StoreBase
from tenants.store.modules.store.infrastructure.models import PembeliStore, ProdukStore
from tenants.store.adapters.api.v1 import store_buyer_router as router
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.application import services as erp
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import ProdukIn as MasterIn, ProdukKeluargaIn, PublishTokoIn


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(StoreBase.metadata.create_all)
        await connection.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def seed(db):
    buyer = PembeliStore(nama="Pembeli", email="buyer@example.com", password_hash="x")
    db.add(buyer)
    product = await services.create_produk(db, ProdukIn(nama="Produk", harga=Decimal(100), stok=10))
    await db.flush()
    return buyer, product


@pytest.mark.asyncio
async def test_stale_inventory_edit_cannot_restore_sold_stock(db):
    _, product = await seed(db)
    await db.execute(update(ProdukStore).where(ProdukStore.id == product.id).values(stok=7))
    with pytest.raises(HTTPException) as exc:
        await services.update_produk(db, product.id, ProdukPatch(stok=12, expected_stok=10))
    assert exc.value.status_code == 409
    await db.refresh(product)
    assert product.stok == 7


@pytest.mark.asyncio
async def test_failed_shipping_rolls_back_order_and_cart(db, monkeypatch):
    buyer, product = await seed(db)
    await services.tambah_ke_keranjang(db, buyer.id, product.id, 2)
    await db.commit()
    pid, uid = product.id, buyer.id
    async def fail(*args):
        raise HTTPException(502, "Rates unavailable")
    monkeypatch.setattr(router, "isi_alamat_pengiriman", fail)
    with pytest.raises(HTTPException):
        await router.checkout(CheckoutIn(pengiriman=PengirimanIn(kurir="jne", layanan="reg", nama_penerima="Buyer", telepon_penerima="081234567890", alamat_tujuan="Jalan Mawar", kode_pos_tujuan="46396")), db, buyer)
    await db.rollback()
    assert (await db.get(ProdukStore, pid)).stok == 10
    assert len(await services.get_keranjang(db, uid)) == 1
    assert await services.list_pesanan_milik(db, uid) == []


@pytest.mark.asyncio
async def test_idle_expiry_releases_stock_once(db):
    buyer, product = await seed(db)
    await services.tambah_ke_keranjang(db, buyer.id, product.id, 2)
    order = await services.checkout(db, buyer.id)
    order.created_at = datetime.now(timezone.utc) - timedelta(days=2)
    await db.flush()
    assert await expire_idle(db) == 1
    assert await expire_idle(db) == 0
    await db.refresh(product)
    assert product.stok == 10


@pytest.mark.asyncio
async def test_unknown_payment_is_not_expired_or_cancelled(db):
    buyer, product = await seed(db)
    await services.tambah_ke_keranjang(db, buyer.id, product.id, 2)
    order = await services.checkout(db, buyer.id)
    order.created_at = datetime.now(timezone.utc) - timedelta(days=2)
    order.payment_state = "sending"
    await db.flush()
    assert await expire_idle(db) == 0
    with pytest.raises(HTTPException):
        await services.ubah_status_pesanan(db, order.id, "dibatalkan")
    await db.refresh(product)
    assert product.stok == 8


@pytest.mark.asyncio
async def test_family_publish_one_parent_and_independent_variant_inventory(db):
    family = await erp.create_produk_keluarga(db, ProdukKeluargaIn(nama="Kaos", tiers=["Ukuran"]))
    masters = []
    for i, size in enumerate(["S", "L"]):
        masters.append(await erp.create_produk(db, MasterIn(sku_induk=f"SKU{i}", nama="Kaos", harga_dasar=Decimal(100+i), keluarga_id=family["id"], opsi_varian=[{"tier":"Ukuran", "opsi":size}])))
    first = await publish_master(db, db, masters[0].id, PublishTokoIn(salin_foto=False))
    product = await services.get_produk(db, first["produk"]["id"])
    assert len(product.varian) == 2
    assert services.produk_out(product)["varian"][0]["opsi"][0]["tier"] == "Ukuran"
    product.varian[0].stok = 5
    await db.flush()
    second = await publish_master(db, db, masters[1].id, PublishTokoIn(salin_foto=False))
    assert second["produk"]["id"] == first["produk"]["id"]
    assert second["produk"]["stok"] == 5


@pytest.mark.asyncio
async def test_payment_session_reused_without_second_provider_call(db, monkeypatch):
    from types import SimpleNamespace
    buyer, product = await seed(db)
    await services.tambah_ke_keranjang(db, buyer.id, product.id, 1)
    order = await services.checkout(db, buyer.id)
    monkeypatch.setattr(router, "ipaymu_is_configured", lambda: True)
    monkeypatch.setattr(router.shipping_biteship, "aktif", lambda: False)
    calls = []
    async def provider(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(checkout_url="https://example.com/pay", gateway_ref="s1")
    monkeypatch.setattr(router, "ipaymu_create_payment", provider)
    first = await router.mulai_pembayaran(order.id, db, buyer)
    second = await router.mulai_pembayaran(order.id, db, buyer)
    assert first == second and len(calls) == 1


@pytest.mark.asyncio
async def test_cleanup_failure_keeps_receipt_until_success(db, monkeypatch):
    from tenants.store.modules.store.application import media_cleanup
    from tenants.store.modules.store.infrastructure.models import MediaCleanup
    await media_cleanup.enqueue(db, "produk/orphan.jpg")
    row = await db.get(MediaCleanup, "produk/orphan.jpg")
    row.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    await db.commit()
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    async def fail(key):
        return False
    monkeypatch.setattr(media_cleanup, "delete_foto", fail)
    await media_cleanup.run_once(factory)
    await db.refresh(row)
    assert row.key == "produk/orphan.jpg"
    async def success(key):
        return True
    monkeypatch.setattr(media_cleanup, "delete_foto", success)
    await media_cleanup.run_once(factory)
    db.expire_all()
    assert await db.get(MediaCleanup, "produk/orphan.jpg") is None


@pytest.mark.asyncio
async def test_chat_order_attachment_is_scoped_to_buyer_and_history_bounded(db):
    from tenants.store.modules.store.infrastructure.models import PesanChatStore
    buyer, product = await seed(db)
    await services.tambah_ke_keranjang(db, buyer.id, product.id, 1)
    order = await services.checkout(db, buyer.id)
    another = PembeliStore(nama="Other", email="other@example.com", password_hash="x")
    db.add(another)
    await db.flush()
    chat = await services.get_or_create_percakapan(db, another.id)
    with pytest.raises(HTTPException):
        await services.kirim_pesan(db, chat.id, "admin", "", sebagai_admin=True, pesanan_id=order.id)
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(51):
        db.add(PesanChatStore(percakapan_id=chat.id, pengirim_id=another.id, isi=str(i), created_at=base + timedelta(seconds=i)))
    await db.flush()
    latest = await services.get_percakapan(db, chat.id)
    assert len(latest.pesan) == 50 and latest.has_older
    cursor = latest.pesan[0].id
    older = await services.get_percakapan(db, chat.id, cursor)
    assert len(older.pesan) == 1 and older.pesan[0].isi == "0"
