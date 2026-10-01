"""store tenant: admin and buyer sub-tenants are isolated from each other.

Route handlers/dependencies are called directly (httpx is not a project
dependency), same approach as tests/test_marketplace_erp_auth_security.py.
Both sides deliberately share ONE signing secret here (the live-safe
fallback when JWT_SECRET_STORE_* are unset) to prove that the aud/iss claims
alone keep the two sides apart.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shared.security import create_access_token
from tenants.store.adapters.api.v1 import store_admin_router as admin_module
from tenants.store.adapters.api.v1 import store_buyer_router as buyer_module
from tenants.store.modules.store.infrastructure import auth
from tenants.store.modules.store.infrastructure import media_storage
from tenants.store.modules.store.infrastructure.database import StoreBase
from tenants.store.modules.store.infrastructure.models import AdminStore, PembeliStore


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_STORE_ADMIN", "one-shared-test-secret")
    monkeypatch.setenv("JWT_SECRET_STORE_BUYER", "one-shared-test-secret")


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(StoreBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    await engine.dispose()


async def _admin(session, role="admin", email="a@mail.com") -> AdminStore:
    user = AdminStore(nama="A", email=email, password_hash="x", role=role)
    session.add(user)
    await session.flush()
    return user


async def _buyer(session, email="b@mail.com") -> PembeliStore:
    user = PembeliStore(nama="B", email=email, password_hash="x")
    session.add(user)
    await session.flush()
    return user


def _request(cookie_name=None, token=None, headers=None):
    cookies = {cookie_name: token} if cookie_name else {}
    return SimpleNamespace(cookies=cookies, headers=headers or {})


# --- token isolation -------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_token_accepted_by_admin_dependency(session):
    admin = await _admin(session)
    token = auth.issue_admin_token(admin)

    user = await auth.get_current_admin(_request(auth.ADMIN_COOKIE_NAME, token), session)

    assert user.id == admin.id


@pytest.mark.asyncio
async def test_buyer_token_rejected_by_admin_dependency_even_in_admin_cookie(session):
    buyer = await _buyer(session)
    token = auth.issue_buyer_token(buyer)

    with pytest.raises(HTTPException) as exc:
        await auth.get_current_admin(_request(auth.ADMIN_COOKIE_NAME, token), session)

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_admin_token_rejected_by_buyer_dependency_even_in_buyer_cookie(session):
    admin = await _admin(session, role="owner")
    token = auth.issue_admin_token(admin)

    with pytest.raises(HTTPException) as exc:
        await auth.get_current_buyer(_request(auth.BUYER_COOKIE_NAME, token), session)

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_buyer_cookie_is_ignored_by_admin_dependency(session):
    buyer = await _buyer(session)
    token = auth.issue_buyer_token(buyer)

    with pytest.raises(HTTPException) as exc:
        await auth.get_current_admin(_request(auth.BUYER_COOKIE_NAME, token), session)

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_token_without_aud_and_iss_is_rejected(session):
    admin = await _admin(session)
    from jose import jwt

    legacy = jwt.encode({"sub": admin.id, "role": "owner"}, "one-shared-test-secret", algorithm="HS256")

    with pytest.raises(HTTPException) as exc:
        await auth.get_current_admin(_request(auth.ADMIN_COOKIE_NAME, legacy), session)

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_other_tenant_token_is_rejected(session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET_TOKO", "one-shared-test-secret")
    admin = await _admin(session)
    foreign = create_access_token(admin.id, "owner", 0, tenant="toko")

    with pytest.raises(HTTPException) as exc:
        await auth.get_current_admin(_request(auth.ADMIN_COOKIE_NAME, foreign), session)

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_token_for_deleted_user_is_rejected(session):
    admin = await _admin(session)
    token = auth.issue_admin_token(admin)
    await session.delete(admin)
    await session.flush()

    with pytest.raises(HTTPException) as exc:
        await auth.get_current_admin(_request(auth.ADMIN_COOKIE_NAME, token), session)

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_missing_credentials_are_401(session):
    with pytest.raises(HTTPException) as exc:
        await auth.get_current_buyer(_request(), session)
    assert exc.value.status_code == 401


# --- roles -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_staff_routes_are_owner_only(session):
    guard = auth.require_admin_roles(*admin_module.STAFF_MANAGE_ROLES)
    owner, plain = await _admin(session, "owner", "o@mail.com"), await _admin(session, "admin", "p@mail.com")

    assert await guard(user=owner) is owner
    with pytest.raises(HTTPException) as exc:
        await guard(user=plain)
    assert exc.value.status_code == 403


# --- cookies ---------------------------------------------------------------


def test_cookie_names_differ_and_are_httponly():
    assert auth.ADMIN_COOKIE_NAME != auth.BUYER_COOKIE_NAME
    resp = Response()
    auth.set_admin_cookie(resp, "t")
    auth.set_buyer_cookie(resp, "t")
    cookies = resp.headers.getlist("set-cookie")
    assert any(c.startswith("store_admin_token=") and "HttpOnly" in c for c in cookies)
    assert any(c.startswith("store_buyer_token=") and "HttpOnly" in c for c in cookies)
    assert not any("Domain=" in c for c in cookies)  # host-only: never shared across subdomains


def test_logout_clears_only_its_own_cookie():
    resp = Response()
    auth.clear_admin_cookie(resp)
    cookies = resp.headers.getlist("set-cookie")
    assert len(cookies) == 1 and cookies[0].startswith("store_admin_token=")


# --- route inventory: nothing sensitive is left open ------------------------


def _routes(router):
    for route in router.routes:
        yield route, {getattr(d.call, "__name__", "") for d in route.dependant.dependencies}


def test_every_admin_route_requires_admin_auth_except_login_logout_and_seed():
    open_paths = {"/auth/login", "/auth/logout", "/seed-now"}
    for route, deps in _routes(admin_module.store_admin_router):
        if route.path in open_paths:
            continue
        guarded = {"get_current_admin", "_inner"} & deps
        assert guarded, f"admin route {route.path} has no admin auth dependency"


def test_seed_route_is_gated():
    route = next(r for r in admin_module.store_admin_router.routes if r.path == "/seed-now")
    assert "authorize_store_seed" in {getattr(d.call, "__name__", "") for d in route.dependant.dependencies}


def test_buyer_routes_are_public_only_for_catalog_and_auth():
    public = {
        "/auth/register",
        "/auth/login",
        "/auth/google",
        "/auth/logout",
        "/produk",
        "/produk/{produk_id}",
        "/kategori",
        "/payment/callback",
    }
    for route, deps in _routes(buyer_module.store_buyer_router):
        if route.path in public:
            continue
        assert "get_current_buyer" in deps, f"buyer route {route.path} is not buyer-authenticated"


def test_no_route_is_shared_between_the_two_sides_without_its_own_guard():
    admin_paths = {r.path for r in admin_module.store_admin_router.routes}
    buyer_only = {r.path for r in buyer_module.store_buyer_router.routes if r.path.startswith("/keranjang")}
    assert not (admin_paths & buyer_only)


# --- seed gate -------------------------------------------------------------


def test_seed_secret_header(monkeypatch):
    monkeypatch.setenv("STORE_SEED_SECRET", "s3cret")
    assert admin_module._seed_secret_ok(SimpleNamespace(headers={"X-Store-Seed-Secret": "s3cret"})) is True
    assert admin_module._seed_secret_ok(SimpleNamespace(headers={"X-Seed-Secret": "s3cret"})) is True
    assert admin_module._seed_secret_ok(SimpleNamespace(headers={"X-Store-Seed-Secret": "wrong"})) is False
    monkeypatch.delenv("STORE_SEED_SECRET")
    assert admin_module._seed_secret_ok(SimpleNamespace(headers={"X-Store-Seed-Secret": "anything"})) is False


@pytest.mark.asyncio
async def test_seed_is_never_anonymous(monkeypatch, session):
    monkeypatch.delenv("STORE_SEED_SECRET", raising=False)
    with pytest.raises(HTTPException) as exc:
        await admin_module.authorize_store_seed(_request(), session)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_seed_allows_secret_header_without_user(monkeypatch, session):
    monkeypatch.setenv("STORE_SEED_SECRET", "bootstrap")
    result = await admin_module.authorize_store_seed(_request(headers={"X-Store-Seed-Secret": "bootstrap"}), session)
    assert result is None


@pytest.mark.asyncio
async def test_seed_rejects_non_owner_admin(monkeypatch, session):
    monkeypatch.delenv("STORE_SEED_SECRET", raising=False)
    admin = await _admin(session, "admin")
    token = auth.issue_admin_token(admin)
    with pytest.raises(HTTPException) as exc:
        await admin_module.authorize_store_seed(_request(auth.ADMIN_COOKIE_NAME, token), session)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_seed_requires_owner_password_env(monkeypatch, session):
    from tenants.store.modules.store.infrastructure import seeder

    monkeypatch.delenv("STORE_SEED_OWNER_PASSWORD", raising=False)
    monkeypatch.setattr(seeder, "engine", object())
    with pytest.raises(RuntimeError, match="STORE_SEED_OWNER_PASSWORD"):
        await seeder.seed_store(session)


# --- placeholders fail closed ----------------------------------------------


@pytest.mark.asyncio
async def test_payment_callback_never_marks_an_order_paid_anonymously(session):
    with pytest.raises(HTTPException) as exc:
        await buyer_module.payment_callback({"trx_id": "anything", "status": "berhasil"}, session)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_ipaymu_and_biteship_placeholders_return_501():
    from tenants.store.modules.store.infrastructure import payment_ipaymu, shipping_biteship

    with pytest.raises(HTTPException) as exc:
        await payment_ipaymu.create_payment(
            pesanan_id="p", total="1", nama_pembeli="n", email_pembeli="e@x.com", notify_url="/n", return_url="/r"
        )
    assert exc.value.status_code == 501
    with pytest.raises(HTTPException) as exc:
        await shipping_biteship.cek_ongkir(kode_pos_asal="1", kode_pos_tujuan="2", berat_gram=1, nilai_barang="1")
    assert exc.value.status_code == 501


@pytest.mark.asyncio
async def test_photo_upload_validates_then_reports_r2_not_ready(monkeypatch):
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(HTTPException) as exc:
        await media_storage.upload_produk_photo(b"x", "application/pdf")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        await media_storage.upload_produk_photo(b"x" * (5 * 1024 * 1024 + 1), "image/png")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        await media_storage.upload_produk_photo(b"\x89PNG\r\n\x1a\n" + b"0" * 16, "image/png")
    assert exc.value.status_code == 501


def test_photo_file_name_pattern():
    import re

    name = media_storage.build_nama_file_foto("image/webp")
    assert re.match(r"^ampelkuning_\d{8}_[0-9a-f]+\.webp$", name)


@pytest.mark.asyncio
async def test_buyer_cannot_set_own_ongkir_or_touch_another_buyers_order(session):
    from decimal import Decimal

    from tenants.store.modules.store.application import services
    from tenants.store.modules.store.application.schemas import PengirimanIn
    from tenants.store.modules.store.infrastructure.models import ProdukStore

    owner_buyer, other = await _buyer(session, "own@mail.com"), await _buyer(session, "other@mail.com")
    produk = ProdukStore(nama="Beras", harga=Decimal("1000"), stok=5)
    session.add(produk)
    await session.flush()
    await services.tambah_ke_keranjang(session, owner_buyer.id, produk.id, 1)
    pesanan = await services.checkout(session, owner_buyer.id)
    payload = PengirimanIn(
        kurir="jne",
        layanan="reg",
        ongkir=Decimal("5000"),
        nama_penerima="N",
        telepon_penerima="081234567890",
        alamat_tujuan="Jl. A",
    )

    with pytest.raises(HTTPException) as exc:
        await buyer_module.isi_alamat_pengiriman(pesanan.id, payload, session, other)
    assert exc.value.status_code == 404

    out = await buyer_module.isi_alamat_pengiriman(pesanan.id, payload, session, owner_buyer)
    assert out["ongkir"] == "0"


def test_store_never_answers_503_from_application_code():
    """DigitalOcean App Platform replaces an application 503 with its own HTML 504 page, so the client
    never sees our JSON message (found in production on the 'pay now' button). "Not available yet" is 501."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "tenants" / "store"
    offenders = [
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if "HTTP_503" in p.read_text() or "status_code=503" in p.read_text()
    ]
    assert offenders == []
