"""Security regressions across real isolated ORM sessions; no provider calls."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from shared.security import hash_password
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import auth as erp_auth
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import oauth_security, shopee_push, token_crypto
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import AkunMarketplace, ShopeeOAuthRequest, UserMarketplaceErp
from tenants.marketplace_erp.modules.marketplace_erp.application import services as erp_services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import ChangePasswordIn as ErpPassword
from tenants.store.modules.store.infrastructure import auth as store_auth
from tenants.store.modules.store.infrastructure.database import StoreBase
from tenants.store.modules.store.infrastructure.models import AdminStore, PembeliStore
from tenants.store.modules.store.application import services as store_services
from tenants.store.modules.store.application.schemas import ChangePasswordIn, RegisterRequest, StaffIn


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
        await conn.run_sync(StoreBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield engine, session, factory
    await engine.dispose()


async def owner(session):
    user = UserMarketplaceErp(nama="Owner", username="owner", password_hash=hash_password("old-password"), role="owner")
    session.add(user)
    await session.flush()
    return user


@pytest.mark.asyncio
async def test_database_stores_authenticated_ciphertext_and_orm_decrypts(db):
    _, session, _ = db
    akun = AkunMarketplace(platform="shopee", nama_toko="T", access_token="access-secret", refresh_token="refresh-secret")
    session.add(akun)
    await session.commit()
    row = (await session.execute(text("SELECT access_token, refresh_token FROM mpe_akun_marketplace"))).one()
    assert all(value.startswith(token_crypto.PREFIX) for value in row)
    assert "access-secret" not in row[0] and "refresh-secret" not in row[1]
    await session.refresh(akun)
    assert (akun.access_token, akun.refresh_token) == ("access-secret", "refresh-secret")


def test_token_tampering_wrong_key_and_unconfigured_key_fail_closed(monkeypatch):
    encrypted = token_crypto.encrypt_token("secret")
    damaged = encrypted[:-8] + "AAAAAAAA"
    with pytest.raises(RuntimeError, match="authenticated"):
        token_crypto.decrypt_token(damaged)
    monkeypatch.setenv("SHOPEE_TOKEN_ENCRYPTION_KEYS", Fernet.generate_key().decode())
    with pytest.raises(RuntimeError, match="authenticated"):
        token_crypto.decrypt_token(encrypted)
    with pytest.raises(RuntimeError, match="migration"):
        token_crypto.decrypt_token("plaintext")
    monkeypatch.delenv("SHOPEE_TOKEN_ENCRYPTION_KEYS")
    with pytest.raises(RuntimeError, match="configured"):
        token_crypto.encrypt_token("secret")


@pytest.mark.asyncio
async def test_legacy_tokens_migrate_and_key_rotation_retains_credentials(db, monkeypatch):
    engine, session, _ = db
    akun = AkunMarketplace(platform="shopee", nama_toko="T")
    session.add(akun)
    await session.commit()
    old_key = Fernet.generate_key().decode()
    monkeypatch.setenv("SHOPEE_TOKEN_ENCRYPTION_KEYS", old_key)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE mpe_akun_marketplace SET access_token='legacy', refresh_token='legacy-refresh'"))
        assert await token_crypto.migrate_token_storage(conn) == 1
    new_key = Fernet.generate_key().decode()
    monkeypatch.setenv("SHOPEE_TOKEN_ENCRYPTION_KEYS", f"{new_key},{old_key}")
    async with engine.begin() as conn:
        await token_crypto.migrate_token_storage(conn)
        raw = (await conn.execute(text("SELECT access_token FROM mpe_akun_marketplace"))).scalar_one()
    monkeypatch.setenv("SHOPEE_TOKEN_ENCRYPTION_KEYS", new_key)
    assert token_crypto.decrypt_token(raw) == "legacy"


@pytest.mark.asyncio
async def test_nonce_is_single_use_and_survives_a_new_session(db):
    _, session, factory = db
    user = await owner(session)
    nonce = await oauth_security.issue_nonce(session, "shop", user)
    await session.commit()
    async with factory() as other:
        await oauth_security.consume_nonce(other, "shop", nonce, user)
    with pytest.raises(HTTPException) as exc:
        await oauth_security.consume_nonce(session, "shop", nonce, user)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_nonce_rejects_missing_wrong_account_wrong_owner_changed_session_and_expiry(db):
    _, session, _ = db
    user = await owner(session)
    nonce = await oauth_security.issue_nonce(session, "shop", user)
    for target, state, caller in (
        ("shop", None, user),
        ("other-shop", nonce, user),
        ("shop", nonce, SimpleNamespace(id="other-owner", session_version=0)),
        ("shop", nonce, SimpleNamespace(id=user.id, session_version=1)),
    ):
        with pytest.raises(HTTPException):
            await oauth_security.consume_nonce(session, target, state, caller)
    await session.execute(update(ShopeeOAuthRequest).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    with pytest.raises(HTTPException):
        await oauth_security.consume_nonce(session, "shop", nonce, user)


def test_callback_destination_cannot_be_replaced_by_an_external_host(monkeypatch):
    monkeypatch.setattr(oauth_security, "origin_allowed", lambda origin: origin == "https://erp.example.com")
    root = "https://erp.example.com/oauth/shopee/callback"
    assert oauth_security.callback_url(root, "shop", "nonce", "") == root + "/shop/nonce"
    for url in ("https://evil.example/oauth/shopee/callback", root + "?redirect=evil", "https://erp.example.com/other"):
        with pytest.raises(HTTPException):
            oauth_security.callback_url(url, "shop", "nonce", "")


@pytest.mark.asyncio
async def test_push_replay_is_deduplicated_after_commit_in_a_new_session(db):
    _, session, factory = db
    push = {"code": 3, "timestamp": int(datetime.now(timezone.utc).timestamp()), "shop_id": 42}
    body = json.dumps(push).encode()
    assert await shopee_push.claim_push(session, body, push)
    await session.commit()
    async with factory() as other:
        assert not await shopee_push.claim_push(other, body, push)


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp", [None, True, 0, 10**30])
async def test_push_rejects_invalid_or_stale_timestamp(db, stamp):
    _, session, _ = db
    with pytest.raises(ValueError):
        await shopee_push.claim_push(session, b"push", {"timestamp": stamp})


@pytest.mark.asyncio
async def test_store_password_change_revokes_old_admin_token_and_keeps_new_token(db, monkeypatch):
    _, session, _ = db
    monkeypatch.setenv("JWT_SECRET_STORE_ADMIN", "test-key")
    user = AdminStore(nama="A", email="a@example.com", password_hash=hash_password("old-password"), role="admin")
    session.add(user)
    await session.flush()
    old = store_auth.issue_admin_token(user)
    await store_services.change_admin_password(session, user, "old-password", "new-password")
    request = SimpleNamespace(cookies={store_auth.ADMIN_COOKIE_NAME: old}, headers={})
    with pytest.raises(HTTPException) as exc:
        await store_auth.get_current_admin(request, session)
    assert exc.value.status_code == 401
    request.cookies[store_auth.ADMIN_COOKIE_NAME] = store_auth.issue_admin_token(user)
    assert (await store_auth.get_current_admin(request, session)).id == user.id


@pytest.mark.asyncio
async def test_buyer_token_checks_its_database_session_version(db, monkeypatch):
    _, session, _ = db
    monkeypatch.setenv("JWT_SECRET_STORE_BUYER", "test-key")
    user = PembeliStore(nama="B", email="b@example.com")
    session.add(user)
    await session.flush()
    request = SimpleNamespace(cookies={store_auth.BUYER_COOKIE_NAME: store_auth.issue_buyer_token(user)}, headers={})
    user.session_version += 1
    await session.flush()
    with pytest.raises(HTTPException):
        await store_auth.get_current_buyer(request, session)


@pytest.mark.asyncio
async def test_erp_password_change_revokes_old_token(db, monkeypatch):
    _, session, _ = db
    monkeypatch.setenv("JWT_SECRET_MARKETPLACE_ERP", "test-key")
    user = await owner(session)
    request = SimpleNamespace(cookies={erp_auth.MARKETPLACE_ERP_COOKIE_NAME: erp_auth.issue_marketplace_erp_token(user)}, headers={})
    await erp_services.change_password(session, user, ErpPassword(current_password="old-password", new_password="new-password"))
    with pytest.raises(HTTPException):
        await erp_auth.get_current_user_marketplace_erp(request, session)
    request.cookies[erp_auth.MARKETPLACE_ERP_COOKIE_NAME] = erp_auth.issue_marketplace_erp_token(user)
    assert (await erp_auth.get_current_user_marketplace_erp(request, session)).id == user.id


@pytest.mark.parametrize("model", [RegisterRequest, StaffIn])
def test_store_password_byte_and_name_limits(model):
    with pytest.raises(ValidationError):
        model(nama="A", email="a@example.com", password="é" * 37)
    assert model(nama=" A ", email="a@example.com", password="é" * 36).nama == "A"
    for name in (" " * 5, "a" * 256):
        with pytest.raises(ValidationError):
            model(nama=name, email="a@example.com", password="12345678")
    with pytest.raises(ValidationError):
        ChangePasswordIn(current_password="old", new_password="🙂" * 19)


@pytest.mark.asyncio
async def test_generic_exception_response_has_tracking_id_and_no_internal_details():
    from main import CsrfOriginMiddleware

    async def fail(request):
        raise RuntimeError("SELECT access_token FROM mpe_akun_marketplace; password=secret")

    mw = CsrfOriginMiddleware(app=None)
    request = Request({"type": "http", "method": "GET", "path": "/api/store/admin/staff", "headers": [], "query_string": b""})
    response = await mw.dispatch(request, fail)
    body = json.loads(response.body)
    assert response.status_code == 500
    assert set(body) == {"detail", "tracking_id"}
    assert response.headers["X-Request-ID"] == body["tracking_id"]
    assert "secret" not in str(body) and "SELECT" not in str(body) and "staff" not in str(body)


@pytest.mark.asyncio
async def test_oauth_start_issues_nonce_in_redirect_path_and_failed_exchange_cannot_reuse_it(db, monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    from tenants.marketplace_erp.adapters.api.v1 import marketplace_erp_router as router
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

    _, session, _ = db
    user = await owner(session)
    akun = AkunMarketplace(platform="shopee", nama_toko="T")
    session.add(akun)
    await session.flush()
    root = "https://erp.example.com/oauth/shopee/callback"
    monkeypatch.setenv("SHOPEE_REDIRECT_URI", root)
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_ID", "123")
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_KEY", "test-partner-key")
    start = await router.oauth_shopee_start(akun_id=akun.id, redirect_uri=None, session=session, user=user)
    redirect = parse_qs(urlsplit(start.authorize_url).query)["redirect"][0]
    assert redirect.startswith(root + "/" + akun.id + "/")
    nonce = redirect.rsplit("/", 1)[1]
    calls = []

    async def unavailable(**kwargs):
        calls.append(kwargs)
        raise HTTPException(status_code=502, detail="Provider unavailable")

    monkeypatch.setattr(erp_shopee, "exchange_token", unavailable)
    with pytest.raises(HTTPException) as exc:
        await router.oauth_shopee_callback(akun.id, code="code", shop_id="123", main_account_id=None, nonce=nonce, session=session, user=user)
    assert exc.value.status_code == 502
    user_id = user.id
    await session.rollback()
    akun_id = start.akun_id
    with pytest.raises(HTTPException) as exc:
        await router.oauth_shopee_callback(akun_id, code="code", shop_id="123", main_account_id=None, nonce=nonce, session=session, user=await session.get(UserMarketplaceErp, user_id))
    assert exc.value.status_code == 400 and len(calls) == 1
