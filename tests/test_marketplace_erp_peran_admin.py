"""Roles: admin (everything, incl. other users' usernames) > owner > staff; self-service profile."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shared.security import hash_password
from tenants.marketplace_erp.adapters.api.v1 import marketplace_erp_router as router
from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import (
    ProfilUpdateIn,
    UserCreateIn,
    UserUpdateIn,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import seeder
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import (
    akun_ids_diizinkan,
    require_roles_marketplace_erp,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import UserMarketplaceErp


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _user(session, email, role, nama=None):
    u = UserMarketplaceErp(nama=nama or role.title(), email=email, password_hash=hash_password("rahasia123"), role=role)
    session.add(u)
    await session.flush()
    return u


@pytest.mark.asyncio
async def test_admin_passes_every_owner_level_guard_and_staff_does_not():
    guard = require_roles_marketplace_erp(*router.OWNER_ONLY)
    for role in ("admin", "owner"):
        assert (await guard(user=SimpleNamespace(role=role))).role == role
    with pytest.raises(HTTPException) as exc:
        await guard(user=SimpleNamespace(role="staff"))
    assert exc.value.status_code == 403
    assert "admin" in router.OWNER_OR_STAFF and "admin" in router.OWNER_ONLY and router.ADMIN_ONLY == ("admin",)


@pytest.mark.asyncio
async def test_admin_is_unrestricted_across_shops(session):
    admin = await _user(session, "a@x.id", "admin")
    assert await akun_ids_diizinkan(admin, session) is None


def test_only_an_admin_may_edit_other_users():
    import inspect

    assert router.ADMIN_ONLY == ("admin",)
    src = inspect.getsource(router.update_user)
    assert "ADMIN_ONLY" in src


def test_who_may_create_which_role():
    services.pastikan_boleh_membuat_peran("admin", "admin")
    services.pastikan_boleh_membuat_peran("admin", "owner")
    services.pastikan_boleh_membuat_peran("owner", "staff")
    for pembuat, peran in (("owner", "owner"), ("owner", "admin"), ("staff", "staff"), (None, "staff")):
        with pytest.raises(HTTPException) as exc:
            services.pastikan_boleh_membuat_peran(pembuat, peran)
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_router_create_user_applies_the_role_matrix(session):
    owner = await _user(session, "o@x.id", "owner")
    admin = await _user(session, "a@x.id", "admin")
    ok = await router.create_user(
        payload=UserCreateIn(nama="S", email="s@x.id", password="sementara123", role="staff"), session=session, _=owner
    )
    assert ok.role == "staff"
    with pytest.raises(HTTPException) as exc:
        await router.create_user(
            payload=UserCreateIn(nama="O2", email="o2@x.id", password="sementara123", role="owner"), session=session, _=owner
        )
    assert exc.value.status_code == 403
    made = await router.create_user(
        payload=UserCreateIn(nama="A2", email="a2@x.id", password="sementara123", role="admin"), session=session, _=admin
    )
    assert made.role == "admin"


@pytest.mark.asyncio
async def test_admin_changes_username_name_and_role_of_another_user(session):
    staff = await _user(session, "lama@x.id", "staff", nama="Lama")
    await _user(session, "terpakai@x.id", "staff")
    out = await services.update_user(session, staff.id, UserUpdateIn(email="baru@x.id", nama="Baru", role="owner"))
    assert (out.email, out.nama, out.role) == ("baru@x.id", "Baru", "owner")
    with pytest.raises(HTTPException) as exc:  # username already taken
        await services.update_user(session, staff.id, UserUpdateIn(email="terpakai@x.id"))
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        await services.update_user(session, "nope", UserUpdateIn(nama="x"))
    assert exc.value.status_code == 404
    with pytest.raises(ValidationError):
        UserUpdateIn(role="superadmin")


@pytest.mark.asyncio
async def test_the_last_admin_cannot_be_demoted(session):
    admin = await _user(session, "a@x.id", "admin")
    with pytest.raises(HTTPException) as exc:
        await services.update_user(session, admin.id, UserUpdateIn(role="owner"))
    assert exc.value.status_code == 409
    kedua = await _user(session, "a2@x.id", "admin")
    assert (await services.update_user(session, kedua.id, UserUpdateIn(role="owner"))).role == "owner"


@pytest.mark.asyncio
async def test_everyone_edits_their_own_name_but_never_username_or_role(session):
    staff = await _user(session, "s@x.id", "staff", nama="Lama")
    out = await router.update_profil(payload=ProfilUpdateIn(nama="  Nama Baru "), session=session, user=staff)
    assert out.nama == "Nama Baru" and out.email == "s@x.id" and out.role == "staff"
    for terlarang in ({"nama": "x", "email": "lain@x.id"}, {"nama": "x", "role": "admin"}):
        with pytest.raises(ValidationError):
            ProfilUpdateIn(**terlarang)
    with pytest.raises(ValidationError):
        ProfilUpdateIn(nama="")


@pytest.mark.asyncio
async def test_admin_is_ensured_by_promoting_the_oldest_owner_or_the_named_email(session, monkeypatch):
    monkeypatch.delenv("MARKETPLACE_ERP_ADMIN_EMAIL", raising=False)
    assert await seeder._ensure_admin(session) is None  # nobody to promote
    pertama = await _user(session, "o1@x.id", "owner")
    await _user(session, "o2@x.id", "owner")
    await _user(session, "s@x.id", "staff")
    diangkat = await seeder._ensure_admin(session)
    assert diangkat.id == pertama.id and diangkat.role == "admin"
    assert await seeder._ensure_admin(session) is None  # already has an admin
    monkeypatch.setenv("MARKETPLACE_ERP_ADMIN_EMAIL", "o2@x.id")
    assert (await seeder._ensure_admin(session)).email == "o2@x.id"
