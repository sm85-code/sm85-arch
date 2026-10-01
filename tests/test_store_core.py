"""store tenant: catalog / cart / checkout / orders / address / chat business
logic against a real (in-memory SQLite) async session -- the parts most
worth catching regressions in (stock handling, ownership, status flow).
"""
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.store.modules.store.application import services
from tenants.store.modules.store.application.schemas import (
    AlamatIn,
    KategoriIn,
    ProdukIn,
    RegisterRequest,
    StaffIn,
)
from tenants.store.modules.store.infrastructure.database import StoreBase
from tenants.store.modules.store.infrastructure.models import AdminStore, PembeliStore, ProdukStore


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fk(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(StoreBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    await engine.dispose()


async def _pembeli(session, email="pembeli@test.com") -> PembeliStore:
    user = PembeliStore(nama="Pembeli", email=email, password_hash="x")
    session.add(user)
    await session.flush()
    return user


async def _produk(session, *, stok=10, harga=Decimal("65000.00"), aktif=True) -> ProdukStore:
    produk = ProdukStore(nama="Beras 5kg", harga=harga, stok=stok, aktif=aktif)
    session.add(produk)
    await session.flush()
    return produk


# --- Checkout & stock ------------------------------------------------------


@pytest.mark.asyncio
async def test_checkout_decrements_stock_and_clears_cart(session):
    user, produk = await _pembeli(session), None
    produk = await _produk(session, stok=10)
    await services.tambah_ke_keranjang(session, user.id, produk.id, 3)

    pesanan = await services.checkout(session, user.id)

    assert pesanan.status == "menunggu_pembayaran"
    assert str(pesanan.total) == "195000.00"
    await session.refresh(produk)
    assert produk.stok == 7
    assert await services.get_keranjang(session, user.id) == []


@pytest.mark.asyncio
async def test_checkout_rejects_insufficient_stock(session):
    user = await _pembeli(session)
    produk = await _produk(session, stok=2)
    await services.tambah_ke_keranjang(session, user.id, produk.id, 5)

    with pytest.raises(Exception) as exc:
        await services.checkout(session, user.id)

    assert "tidak cukup" in str(exc.value.detail)
    await session.refresh(produk)
    assert produk.stok == 2


@pytest.mark.asyncio
async def test_checkout_stock_guard_holds_when_stock_changes_after_check(session):
    """The in-memory stock check can be stale under concurrency; the
    conditional UPDATE must still refuse to go below zero."""
    user = await _pembeli(session)
    produk = await _produk(session, stok=5)
    await services.tambah_ke_keranjang(session, user.id, produk.id, 4)
    # Another checkout drained the stock in the DB after our row was loaded.
    items = await services.get_keranjang(session, user.id)
    items[0].produk.stok = 5  # stale in-memory value says "enough"
    await session.execute(
        ProdukStore.__table__.update().where(ProdukStore.id == produk.id).values(stok=1)
    )

    with pytest.raises(Exception) as exc:
        await services.checkout(session, user.id)

    assert "tidak cukup" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_cancel_order_restores_stock(session):
    user = await _pembeli(session)
    produk = await _produk(session, stok=10)
    await services.tambah_ke_keranjang(session, user.id, produk.id, 3)
    pesanan = await services.checkout(session, user.id)

    await services.ubah_status_pesanan(session, pesanan.id, "dibatalkan")

    await session.refresh(produk)
    assert produk.stok == 10


@pytest.mark.asyncio
async def test_invalid_status_transition_is_rejected(session):
    user = await _pembeli(session)
    produk = await _produk(session)
    await services.tambah_ke_keranjang(session, user.id, produk.id, 1)
    pesanan = await services.checkout(session, user.id)

    with pytest.raises(Exception) as exc:
        await services.ubah_status_pesanan(session, pesanan.id, "selesai")

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_inactive_product_cannot_be_added_to_cart(session):
    user = await _pembeli(session)
    produk = await _produk(session, aktif=False)

    with pytest.raises(Exception) as exc:
        await services.tambah_ke_keranjang(session, user.id, produk.id, 1)

    assert exc.value.status_code == 400


def test_produk_input_rejects_negative_price_and_stock():
    with pytest.raises(Exception):
        ProdukIn(nama="x", harga="-1")
    with pytest.raises(Exception):
        ProdukIn(nama="x", harga="1000", stok=-1)


# --- Product output / provenance -------------------------------------------


@pytest.mark.asyncio
async def test_produk_out_builds_foto_url_from_key_and_media_base(session, monkeypatch):
    monkeypatch.setenv("MEDIA_BASE_URL", "https://img.example.com/")
    produk = await services.create_produk(session, ProdukIn(nama="Gula", harga="14000", stok=3))
    await services.set_foto_produk(session, produk.id, "ampelkuning_20260101_abc.jpg")

    out = services.produk_out(await services.get_produk(session, produk.id))

    assert out["foto_key"] == "ampelkuning_20260101_abc.jpg"
    assert out["foto_url"] == "https://img.example.com/ampelkuning_20260101_abc.jpg"
    assert out["sumber"] == "manual"


@pytest.mark.asyncio
async def test_produk_out_has_no_foto_url_without_media_base(session, monkeypatch):
    monkeypatch.delenv("MEDIA_BASE_URL", raising=False)
    produk = await services.create_produk(session, ProdukIn(nama="Gula", harga="14000"))
    await services.set_foto_produk(session, produk.id, "k.jpg")

    assert services.produk_out(await services.get_produk(session, produk.id))["foto_url"] is None


@pytest.mark.asyncio
async def test_delete_kategori_keeps_products(session):
    kategori = await services.create_kategori(session, KategoriIn(nama="Sembako"))
    produk = await services.create_produk(session, ProdukIn(nama="Gula", harga="1000", kategori_id=kategori.id))

    await services.delete_kategori(session, kategori.id)
    await session.flush()
    session.expire(produk, ["kategori_id"])

    assert (await services.get_produk(session, produk.id)).kategori_id is None


# --- Accounts: admin and buyer are separate tables --------------------------


@pytest.mark.asyncio
async def test_register_and_authenticate_buyer(session):
    user = await services.register(session, RegisterRequest(nama="Budi", email="budi@mail.com", password="password123"))

    assert (await services.authenticate_buyer(session, "budi@mail.com", "password123")).id == user.id
    with pytest.raises(Exception) as exc:
        await services.authenticate_buyer(session, "budi@mail.com", "salah-salah")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_buyer_credentials_do_not_log_into_admin_and_vice_versa(session):
    await services.register(session, RegisterRequest(nama="Budi", email="same@mail.com", password="password123"))

    with pytest.raises(Exception) as exc:
        await services.authenticate_admin(session, "same@mail.com", "password123")
    assert exc.value.status_code == 401

    from shared.security import hash_password

    session.add(AdminStore(nama="A", email="admin@mail.com", password_hash=hash_password("adminpass1"), role="admin"))
    await session.flush()
    with pytest.raises(Exception) as exc:
        await services.authenticate_buyer(session, "admin@mail.com", "adminpass1")
    assert exc.value.status_code == 401


def test_register_requires_min_password_length():
    with pytest.raises(Exception):
        RegisterRequest(nama="Budi", email="budi@mail.com", password="short")


@pytest.mark.asyncio
async def test_google_only_account_cannot_log_in_with_password(session):
    user = await services.login_or_register_google(session, google_sub="g-1", email="g@mail.com", nama="G")

    assert user.password_hash is None
    with pytest.raises(Exception) as exc:
        await services.authenticate_buyer(session, "g@mail.com", "")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_google_login_never_touches_admin_accounts(session):
    from shared.security import hash_password

    admin = AdminStore(nama="Owner", email="owner@mail.com", password_hash=hash_password("ownerpass1"), role="owner")
    session.add(admin)
    await session.flush()

    buyer = await services.login_or_register_google(session, google_sub="g-2", email="owner@mail.com", nama="Someone")

    assert isinstance(buyer, PembeliStore)
    await session.refresh(admin)
    assert admin.role == "owner"
    assert not hasattr(admin, "google_sub")


@pytest.mark.asyncio
async def test_google_login_links_existing_buyer_by_email_and_is_stable(session):
    buyer = await services.register(session, RegisterRequest(nama="Budi", email="b@mail.com", password="password123"))

    linked = await services.login_or_register_google(session, google_sub="g-3", email="b@mail.com", nama="Budi")
    again = await services.login_or_register_google(session, google_sub="g-3", email="changed@mail.com", nama="Budi")

    assert linked.id == buyer.id == again.id
    assert linked.google_sub == "g-3"


# --- Staff -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_staff_always_makes_plain_admin_and_blocks_duplicate_email(session):
    staff = await services.create_staff(session, StaffIn(nama="S", email="s@mail.com", password="password123"))

    assert staff.role == "admin"
    with pytest.raises(Exception) as exc:
        await services.create_staff(session, StaffIn(nama="S2", email="s@mail.com", password="password123"))
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_owner_account_cannot_be_deleted_via_staff(session):
    owner = AdminStore(nama="O", email="o@mail.com", password_hash="x", role="owner")
    session.add(owner)
    await session.flush()

    with pytest.raises(Exception) as exc:
        await services.delete_staff(session, owner.id)

    assert exc.value.status_code == 400


# --- Alamat & chat ---------------------------------------------------------


@pytest.mark.asyncio
async def test_first_address_becomes_primary_and_is_owner_scoped(session):
    a, b = await _pembeli(session, "a@mail.com"), await _pembeli(session, "b@mail.com")
    alamat = await services.create_alamat(
        session,
        a.id,
        AlamatIn(nama_penerima="A", telepon_penerima="081234567890", alamat_lengkap="Jl. Mawar 1", kode_pos="12345"),
    )

    assert alamat.utama is True
    with pytest.raises(Exception) as exc:
        await services.delete_alamat(session, b.id, alamat.id)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_chat_sender_side_comes_from_the_caller_not_a_role_string(session):
    buyer = await _pembeli(session)
    percakapan = await services.get_or_create_percakapan(session, buyer.id)

    await services.kirim_pesan(session, percakapan.id, buyer.id, "halo", sebagai_admin=False)
    loaded = await services.get_percakapan(session, percakapan.id)
    assert loaded.unread_admin is True and loaded.unread_pembeli is False

    await services.kirim_pesan(session, percakapan.id, "admin-id", "ya kak", sebagai_admin=True)
    loaded = await services.get_percakapan(session, percakapan.id)
    assert loaded.unread_admin is False and loaded.unread_pembeli is True
    assert [p.pengirim_admin for p in loaded.pesan] == [False, True]


# --- Laporan ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_ringkasan_status_counts_orders(session):
    user = await _pembeli(session)
    produk = await _produk(session, stok=10)
    await services.tambah_ke_keranjang(session, user.id, produk.id, 1)
    await services.checkout(session, user.id)

    assert await services.laporan_ringkasan_status(session) == {"menunggu_pembayaran": 1}
