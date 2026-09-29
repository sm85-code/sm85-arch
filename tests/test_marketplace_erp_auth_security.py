"""marketplace_erp auth hardening:

* POST /auth/register is closed by default (403) -- it used to let anyone
  create a role=owner account. Opt back in only with
  MARKETPLACE_ERP_ALLOW_REGISTER=true.
* Owner-only account creation (POST /users) replaces it.
* POST /auth/change-password for the logged-in user.
* must_change_password flag for the seeded default-password owner and for
  owner-created accounts, exposed via /auth/me (UserOut).

Runs against an in-memory SQLite session sharing MarketplaceErpBase metadata
(same pattern as tests/test_marketplace_erp_foundation.py). Route handlers
and dependencies are called directly -- httpx is not a project dependency.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

if not (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")):
    os.environ["DATABASE_URL"] = "postgresql://placeholder@localhost/placeholder"

from shared.security import hash_password, verify_password  # noqa: E402
from tenants.marketplace_erp.adapters.api.v1 import marketplace_erp_router as router_module  # noqa: E402
from tenants.marketplace_erp.adapters.api.v1.marketplace_erp_router import (  # noqa: E402
    marketplace_erp_router,
)
from tenants.marketplace_erp.modules.marketplace_erp.application import services  # noqa: E402
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import (  # noqa: E402
    ChangePasswordIn,
    LoginIn,
    RegisterIn,
    UserCreateIn,
    UserOut,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import (  # noqa: E402
    database as mpe_database,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import seeder  # noqa: E402
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import (  # noqa: E402
    MARKETPLACE_ERP_COOKIE_NAME,
    get_current_user_marketplace_erp,
    issue_marketplace_erp_token,
    require_roles_marketplace_erp,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import (  # noqa: E402
    MarketplaceErpBase,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (  # noqa: E402
    UserMarketplaceErp,
)


@pytest_asyncio.fixture
async def engine_and_session(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    # Point the seeder at the SQLite engine (it reads mpe_database.engine /
    # SessionLocal at call time).
    monkeypatch.setattr(mpe_database, "engine", engine)
    monkeypatch.setattr(mpe_database, "SessionLocal", session_local)
    async with session_local() as s:
        yield engine, session_local, s
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine_and_session):
    return engine_and_session[2]


async def _make_user(session, *, email="owner@test.com", password="rahasia123", role="owner", must=False):
    user = UserMarketplaceErp(
        nama=role.title(), email=email, password_hash=hash_password(password), role=role,
        must_change_password=must,
    )
    session.add(user)
    await session.flush()
    return user


def _route(path: str, method: str):
    return next(
        r for r in marketplace_erp_router.routes
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set())
    )


def _dep_names(route) -> list[str]:
    names = []
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        names.append(getattr(dep.call, "__name__", ""))
        stack.extend(dep.dependencies)
    return names


# --- Public registration closed ------------------------------------------------


@pytest.mark.asyncio
async def test_public_register_forbidden_by_default(session, monkeypatch):
    monkeypatch.delenv("MARKETPLACE_ERP_ALLOW_REGISTER", raising=False)
    with pytest.raises(HTTPException) as exc:
        await router_module.register(
            RegisterIn(nama="Attacker", email="evil@attacker.com", password="hunter2hunter2"), session=session
        )
    assert exc.value.status_code == 403
    assert await services.list_users(session) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "false", "0", "no", "random"])
async def test_public_register_forbidden_unless_flag_truthy(session, monkeypatch, value):
    monkeypatch.setenv("MARKETPLACE_ERP_ALLOW_REGISTER", value)
    with pytest.raises(HTTPException) as exc:
        await router_module.register(
            RegisterIn(nama="X", email="x@example.com", password="hunter2hunter2"), session=session
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_public_register_allowed_when_flag_true(session, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ERP_ALLOW_REGISTER", "true")
    user = await router_module.register(
        RegisterIn(nama="Dev", email="dev@example.com", password="hunter2hunter2"), session=session
    )
    assert user.role == "owner"
    assert user.must_change_password is False


def test_register_route_has_no_open_owner_shortcut():
    # The handler itself enforces the flag; make sure nobody re-wires it to a
    # dependency-free service call.
    route = _route("/auth/register", "POST")
    assert route.endpoint is router_module.register


def test_register_schema_rejects_short_password():
    with pytest.raises(ValidationError):
        RegisterIn(nama="A", email="a@example.com", password="short")


# --- Owner-only user creation ----------------------------------------------------


def test_users_routes_are_owner_only():
    for method in ("GET", "POST"):
        route = _route("/users", method)
        assert "_inner" in _dep_names(route), f"{method} /users must use require_roles_marketplace_erp"


@pytest.mark.asyncio
async def test_require_owner_rejects_staff():
    guard = require_roles_marketplace_erp("owner")
    with pytest.raises(HTTPException) as exc:
        await guard(user=SimpleNamespace(role="staff"))
    assert exc.value.status_code == 403
    owner = SimpleNamespace(role="owner")
    assert await guard(user=owner) is owner


@pytest.mark.asyncio
async def test_owner_creates_staff_with_forced_password_change(session):
    user = await router_module.create_user(
        UserCreateIn(nama="Staf Gudang", email="staf@example.com", password="sementara123"),
        session=session, _=SimpleNamespace(role="owner"),
    )
    assert user.role == "staff"
    assert user.must_change_password is True
    logged_in = await services.authenticate_user(session, LoginIn(email="staf@example.com", password="sementara123"))
    assert logged_in.id == user.id
    assert UserOut.model_validate(logged_in).must_change_password is True


@pytest.mark.asyncio
async def test_owner_can_create_another_owner(session):
    user = await services.create_user(
        session, UserCreateIn(nama="Owner 2", email="owner2@example.com", password="sementara123", role="OWNER")
    )
    assert user.role == "owner"


@pytest.mark.asyncio
async def test_create_user_rejects_duplicate_email(session):
    await _make_user(session, email="dup@example.com")
    with pytest.raises(HTTPException) as exc:
        await services.create_user(
            session, UserCreateIn(nama="Dup", email="dup@example.com", password="sementara123")
        )
    assert exc.value.status_code == 409


def test_user_create_schema_validation():
    with pytest.raises(ValidationError):
        UserCreateIn(nama="X", email="x@example.com", password="short")
    with pytest.raises(ValidationError):
        UserCreateIn(nama="X", email="x@example.com", password="panjang1234", role="superadmin")
    with pytest.raises(ValidationError):
        UserCreateIn(nama="X", email="x@example.com", password="a" * 73)


@pytest.mark.asyncio
async def test_list_users_returns_accounts(session):
    await _make_user(session, email="a@example.com")
    await _make_user(session, email="b@example.com", role="staff")
    rows = await router_module.list_users(session=session, _=SimpleNamespace(role="owner"))
    assert {r.email for r in rows} == {"a@example.com", "b@example.com"}
    # never leak hashes through the response model
    assert "password_hash" not in UserOut.model_validate(rows[0]).model_dump()


# --- Change password -------------------------------------------------------------


def test_change_password_route_requires_login():
    route = _route("/auth/change-password", "POST")
    assert "get_current_user_marketplace_erp" in _dep_names(route)


@pytest.mark.asyncio
async def test_change_password_success_clears_flag_and_reissues_cookie(session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET_MARKETPLACE_ERP", "test-secret-mpe")
    user = await _make_user(session, password=seeder.DEFAULT_PASSWORD, must=True)
    response = Response()
    out = await router_module.change_password(
        ChangePasswordIn(current_password=seeder.DEFAULT_PASSWORD, new_password="BaruSekali99"),
        response=response, session=session, user=user,
    )
    assert out.must_change_password is False
    assert verify_password("BaruSekali99", user.password_hash)
    assert not verify_password(seeder.DEFAULT_PASSWORD, user.password_hash)
    assert MARKETPLACE_ERP_COOKIE_NAME in response.headers.get("set-cookie", "")
    # old password no longer logs in, new one does
    with pytest.raises(HTTPException):
        await services.authenticate_user(session, LoginIn(email=user.email, password=seeder.DEFAULT_PASSWORD))
    again = await services.authenticate_user(session, LoginIn(email=user.email, password="BaruSekali99"))
    assert again.id == user.id


@pytest.mark.asyncio
async def test_change_password_rejects_wrong_current(session):
    user = await _make_user(session, password="rahasia123")
    with pytest.raises(HTTPException) as exc:
        await services.change_password(
            session, user, ChangePasswordIn(current_password="salah-banget", new_password="BaruSekali99")
        )
    assert exc.value.status_code == 400
    assert verify_password("rahasia123", user.password_hash)


@pytest.mark.asyncio
async def test_change_password_rejects_same_password(session):
    user = await _make_user(session, password="rahasia123")
    with pytest.raises(HTTPException) as exc:
        await services.change_password(
            session, user, ChangePasswordIn(current_password="rahasia123", new_password="rahasia123")
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_change_password_rejects_default_password(session):
    user = await _make_user(session, password="rahasia123")
    with pytest.raises(HTTPException) as exc:
        await services.change_password(
            session, user, ChangePasswordIn(current_password="rahasia123", new_password=seeder.DEFAULT_PASSWORD)
        )
    assert exc.value.status_code == 400


def test_change_password_schema_min_length():
    with pytest.raises(ValidationError):
        ChangePasswordIn(current_password="rahasia123", new_password="1234567")
    with pytest.raises(ValidationError):
        ChangePasswordIn(current_password="", new_password="BaruSekali99")
    ChangePasswordIn(current_password="x", new_password="12345678")


@pytest.mark.asyncio
async def test_staff_can_change_own_password(session):
    staff = await _make_user(session, email="s@example.com", password="sementara123", role="staff", must=True)
    await services.change_password(
        session, staff, ChangePasswordIn(current_password="sementara123", new_password="PunyaSaya123")
    )
    assert staff.must_change_password is False


@pytest.mark.asyncio
async def test_me_exposes_must_change_password_via_token(session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET_MARKETPLACE_ERP", "test-secret-mpe")
    user = await _make_user(session, must=True)
    token = issue_marketplace_erp_token(user)
    req = SimpleNamespace(cookies={MARKETPLACE_ERP_COOKIE_NAME: token}, headers={})
    current = await get_current_user_marketplace_erp(req, session=session)
    out = await router_module.me(user=current)
    assert UserOut.model_validate(out).must_change_password is True


# --- Seeder: first owner via seed-now + default-password flag ------------------


@pytest.mark.asyncio
async def test_seed_creates_owner_flagged_for_password_change(session):
    result = await seeder.seed_marketplace_erp(session)
    owner = await session.get(UserMarketplaceErp, result["owner_id"])
    assert owner.role == "owner"
    assert owner.must_change_password is True
    assert verify_password(seeder.DEFAULT_PASSWORD, owner.password_hash)


@pytest.mark.asyncio
async def test_seed_flags_existing_default_password_owner(session):
    owner = await _make_user(session, email=seeder.OWNER_EMAIL, password=seeder.DEFAULT_PASSWORD, must=False)
    await session.commit()
    await seeder.seed_marketplace_erp(session)
    await session.refresh(owner)
    assert owner.must_change_password is True


@pytest.mark.asyncio
async def test_seed_does_not_reflag_after_password_changed(session):
    await seeder.seed_marketplace_erp(session)
    owner = await services.authenticate_user(
        session, LoginIn(email=seeder.OWNER_EMAIL, password=seeder.DEFAULT_PASSWORD)
    )
    await services.change_password(
        session, owner, ChangePasswordIn(current_password=seeder.DEFAULT_PASSWORD, new_password="OwnerBaru2026")
    )
    await session.commit()
    await seeder.seed_marketplace_erp(session)
    await session.refresh(owner)
    assert owner.must_change_password is False
    assert verify_password("OwnerBaru2026", owner.password_hash)


@pytest.mark.asyncio
async def test_ensure_schema_flags_default_owner_on_startup(engine_and_session):
    _engine, session_local, s = engine_and_session
    await _make_user(s, email=seeder.OWNER_EMAIL, password=seeder.DEFAULT_PASSWORD, must=False)
    await s.commit()
    await seeder.ensure_marketplace_erp_schema()
    async with session_local() as fresh:
        owner = (await services.list_users(fresh))[0]
        assert owner.must_change_password is True


@pytest.mark.asyncio
async def test_ensure_schema_noop_without_database(monkeypatch):
    monkeypatch.setattr(mpe_database, "engine", None)
    monkeypatch.setattr(mpe_database, "SessionLocal", None)
    await seeder.ensure_marketplace_erp_schema()  # must not raise
