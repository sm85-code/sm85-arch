"""Exercises kategori, buku alamat, and chat -- against a real (SQLite,
in-memory) async session, not just import checks.
"""
import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.toko.modules.toko.application import services
from tenants.toko.modules.toko.application.schemas import AlamatIn, AlamatPatch, KategoriIn
from tenants.toko.modules.toko.infrastructure.database import TokoBase
from tenants.toko.modules.toko.infrastructure.models import UserToko


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    # SQLite doesn't enforce FK constraints (ON DELETE SET NULL etc.) by
    # default like Postgres does, and PRAGMA foreign_keys is per-connection
    # (not per-database) -- an event listener on every new DBAPI connection
    # is the only way to make it stick across the pool, so this fixture
    # actually exercises the same behavior production runs on.
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fk(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(TokoBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    await engine.dispose()


async def _make_user(session, email="pembeli@test.com", role="pembeli") -> UserToko:
    user = UserToko(nama="Pembeli Test", email=email, password_hash="x", role=role)
    session.add(user)
    await session.flush()
    return user


# --- Kategori --------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_kategori_rejects_duplicate_name(session):
    await services.create_kategori(session, KategoriIn(nama="Sembako"))
    with pytest.raises(Exception) as exc_info:
        await services.create_kategori(session, KategoriIn(nama="Sembako"))
    assert "sudah ada" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_delete_kategori_sets_produk_kategori_id_null(session):
    from tenants.toko.modules.toko.application.schemas import ProdukIn

    kategori = await services.create_kategori(session, KategoriIn(nama="Elektronik"))
    produk = await services.create_produk(
        session, ProdukIn(nama="Kipas Angin", harga="150000", kategori_id=kategori.id)
    )
    await services.delete_kategori(session, kategori.id)
    await session.flush()
    # ON DELETE SET NULL happens at the DB level, outside SQLAlchemy's
    # knowledge, so the already-loaded `produk` object's cached kategori_id
    # is stale until expired -- a fresh request (fresh session) would never
    # see the stale value, this just simulates that here.
    session.expire(produk, ["kategori_id"])

    ulang = await services.get_produk(session, produk.id)
    assert ulang.kategori_id is None


# --- Buku alamat -------------------------------------------------------


@pytest.mark.asyncio
async def test_first_address_is_auto_marked_utama(session):
    user = await _make_user(session)
    alamat = await services.create_alamat(
        session,
        user.id,
        AlamatIn(nama_penerima="Budi", telepon_penerima="081234567890", alamat_lengkap="Jl. A", kode_pos="40123"),
    )
    assert alamat.utama is True


@pytest.mark.asyncio
async def test_marking_new_address_utama_unsets_the_old_one(session):
    user = await _make_user(session)
    pertama = await services.create_alamat(
        session,
        user.id,
        AlamatIn(nama_penerima="Budi", telepon_penerima="081234567890", alamat_lengkap="Jl. A", kode_pos="40123"),
    )
    kedua = await services.create_alamat(
        session,
        user.id,
        AlamatIn(
            nama_penerima="Budi",
            telepon_penerima="081234567890",
            alamat_lengkap="Jl. B",
            kode_pos="40124",
            utama=True,
        ),
    )
    await session.refresh(pertama)
    assert kedua.utama is True
    assert pertama.utama is False


@pytest.mark.asyncio
async def test_cannot_patch_another_users_address(session):
    user_a = await _make_user(session, email="a@test.com")
    user_b = await _make_user(session, email="b@test.com")
    alamat = await services.create_alamat(
        session,
        user_a.id,
        AlamatIn(nama_penerima="Budi", telepon_penerima="081234567890", alamat_lengkap="Jl. A", kode_pos="40123"),
    )
    with pytest.raises(Exception) as exc_info:
        await services.update_alamat(session, user_b.id, alamat.id, AlamatPatch(label="Kantor"))
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_invalid_postal_code_rejected():
    with pytest.raises(Exception):
        AlamatIn(nama_penerima="Budi", telepon_penerima="081234567890", alamat_lengkap="Jl. A", kode_pos="123")


# --- Chat ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_round_trip_and_unread_flags(session):
    pembeli = await _make_user(session, email="pembeli@test.com", role="pembeli")
    admin = await _make_user(session, email="admin@test.com", role="admin_toko")

    percakapan = await services.get_or_create_percakapan(session, pembeli.id)
    await services.kirim_pesan(session, percakapan.id, pembeli, "Halo, produk ini masih ada?")

    percakapan = await services.get_percakapan(session, percakapan.id)
    assert percakapan.unread_admin is True
    assert len(percakapan.pesan) == 1
    assert percakapan.pesan[0].pengirim_admin is False

    await services.kirim_pesan(session, percakapan.id, admin, "Masih ada, silakan checkout.")
    percakapan = await services.get_percakapan(session, percakapan.id)
    assert percakapan.unread_pembeli is True
    assert len(percakapan.pesan) == 2

    # unread_admin sudah False sejak admin kirim balasan di atas (kirim_pesan
    # otomatis membersihkan unread milik pengirim sendiri) -- tandai_dibaca
    # di sini cuma menegaskan sisi pembeli, tidak mengubah status admin.
    await services.tandai_dibaca(session, percakapan.id, sebagai_admin=False)
    percakapan = await services.get_percakapan(session, percakapan.id)
    assert percakapan.unread_pembeli is False
    assert percakapan.unread_admin is False


@pytest.mark.asyncio
async def test_get_or_create_percakapan_is_idempotent(session):
    user = await _make_user(session)
    p1 = await services.get_or_create_percakapan(session, user.id)
    p2 = await services.get_or_create_percakapan(session, user.id)
    assert p1.id == p2.id
