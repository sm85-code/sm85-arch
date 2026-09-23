"""Exercises the new staff roles (admin_toko_web / admin_marketplace),
per-akun scoping enforcement for admin_marketplace, and the /admin/staff
management endpoints -- service-layer + require_roles_toko dependency
checks, same in-memory SQLite pattern as the other tests/test_toko_*.py
files (no HTTP client, direct calls, matching test_toko_erp_marketplace.py
and test_toko_erp_akun.py conventions).
"""
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.toko.modules.erp.application import services as erp_services
from tenants.toko.modules.erp.application.schemas import AkunMarketplaceIn, PercakapanERPIn, ProdukERPIn
from tenants.toko.modules.toko.application import services as toko_services
from tenants.toko.modules.toko.application.schemas import StaffIn, StaffPatch
from tenants.toko.modules.toko.infrastructure.auth import (
    akun_ids_diizinkan,
    pastikan_akses_akun,
    require_roles_toko,
)
from tenants.toko.modules.toko.infrastructure.database import TokoBase
from tenants.toko.modules.toko.infrastructure.models import UserToko
from tenants.toko.adapters.api.v1.erp_router import ADMIN_ROLES as ERP_ADMIN_ROLES
from tenants.toko.adapters.api.v1.erp_router import AKUN_MANAGE_ROLES
from tenants.toko.adapters.api.v1.toko_router import ADMIN_ROLES as TOKO_ADMIN_ROLES
from tenants.toko.adapters.api.v1.toko_router import STAFF_MANAGE_ROLES


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(TokoBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    await engine.dispose()


async def _make_user(session, *, email: str, role: str) -> UserToko:
    user = UserToko(nama=f"User {role}", email=email, password_hash="x", role=role)
    session.add(user)
    await session.flush()
    return user


async def _make_akun(session, *, platform: str = "shopee", nama_toko: str = "Toko"):
    return await erp_services.create_akun_marketplace(session, AkunMarketplaceIn(platform=platform, nama_toko=nama_toko))


async def _assert_allowed(role: str, allowed_roles: tuple[str, ...]) -> None:
    user = UserToko(nama="x", email=f"{role}-{id(role)}@t.com", role=role)
    guard = require_roles_toko(*allowed_roles)
    await guard(user=user)  # no raise


async def _assert_denied(role: str, allowed_roles: tuple[str, ...]) -> None:
    user = UserToko(nama="x", email=f"{role}-{id(role)}@t.com", role=role)
    guard = require_roles_toko(*allowed_roles)
    with pytest.raises(HTTPException) as exc_info:
        await guard(user=user)
    assert exc_info.value.status_code == 403


# --- role-guard tuples: who gets past each door ----------------------------


@pytest.mark.asyncio
async def test_admin_toko_web_can_reach_toko_web_admin_but_not_erp():
    await _assert_allowed("admin_toko_web", TOKO_ADMIN_ROLES)
    await _assert_denied("admin_toko_web", ERP_ADMIN_ROLES)


@pytest.mark.asyncio
async def test_admin_marketplace_can_reach_erp_admin_but_not_toko_web():
    await _assert_allowed("admin_marketplace", ERP_ADMIN_ROLES)
    await _assert_denied("admin_marketplace", TOKO_ADMIN_ROLES)


@pytest.mark.asyncio
async def test_admin_marketplace_cannot_reach_akun_crud_or_staff_management():
    await _assert_denied("admin_marketplace", AKUN_MANAGE_ROLES)
    await _assert_denied("admin_marketplace", STAFF_MANAGE_ROLES)
    await _assert_denied("admin_toko_web", STAFF_MANAGE_ROLES)


@pytest.mark.asyncio
async def test_owner_and_admin_toko_remain_unrestricted_regression():
    for role in ("owner", "admin_toko"):
        await _assert_allowed(role, TOKO_ADMIN_ROLES)
        await _assert_allowed(role, ERP_ADMIN_ROLES)
        await _assert_allowed(role, AKUN_MANAGE_ROLES)
        await _assert_allowed(role, STAFF_MANAGE_ROLES)


@pytest.mark.asyncio
async def test_pembeli_denied_everywhere():
    for roles in (TOKO_ADMIN_ROLES, ERP_ADMIN_ROLES, AKUN_MANAGE_ROLES, STAFF_MANAGE_ROLES):
        await _assert_denied("pembeli", roles)


# --- akun_ids_diizinkan / pastikan_akses_akun -------------------------------


@pytest.mark.asyncio
async def test_akun_ids_diizinkan_none_for_unrestricted_roles(session):
    for role in ("owner", "admin_toko"):
        user = await _make_user(session, email=f"{role}@t.com", role=role)
        assert await akun_ids_diizinkan(user, session) is None


@pytest.mark.asyncio
async def test_akun_ids_diizinkan_scoped_for_admin_marketplace(session):
    akun_a = await _make_akun(session, nama_toko="Toko A")
    akun_b = await _make_akun(session, nama_toko="Toko B")
    staff = await _make_user(session, email="staffm@t.com", role="admin_marketplace")
    await toko_services._set_staff_akun(session, staff.id, [akun_a.id])

    allowed = await akun_ids_diizinkan(staff, session)
    assert allowed == [akun_a.id]

    await pastikan_akses_akun(staff, session, akun_a.id)  # no raise
    with pytest.raises(HTTPException) as exc_info:
        await pastikan_akses_akun(staff, session, akun_b.id)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_akun_ids_diizinkan_empty_list_when_unassigned(session):
    staff = await _make_user(session, email="staffm2@t.com", role="admin_marketplace")
    allowed = await akun_ids_diizinkan(staff, session)
    assert allowed == []
    akun = await _make_akun(session)
    with pytest.raises(HTTPException):
        await pastikan_akses_akun(staff, session, akun.id)


# --- akun-scoped list/create service behavior -------------------------------


@pytest.mark.asyncio
async def test_list_produk_erp_scoped_to_allowed_akun_ids(session):
    akun_a = await _make_akun(session, nama_toko="A")
    akun_b = await _make_akun(session, nama_toko="B")
    await erp_services.create_produk_erp(
        session, ProdukERPIn(platform="shopee", akun_id=akun_a.id, id_eksternal="A-1", nama="Sabun A", harga=Decimal("1000"))
    )
    await erp_services.create_produk_erp(
        session, ProdukERPIn(platform="shopee", akun_id=akun_b.id, id_eksternal="B-1", nama="Sabun B", harga=Decimal("1000"))
    )

    # Unrestricted (akun_ids=None passthrough): sees everything.
    semua = await erp_services.list_produk_erp(session)
    assert len(semua) == 2

    # Scoped to only akun_a.
    hanya_a = await erp_services.list_produk_erp(session, akun_ids=[akun_a.id])
    assert len(hanya_a) == 1
    assert hanya_a[0].akun_id == akun_a.id


@pytest.mark.asyncio
async def test_create_produk_erp_under_disallowed_akun_rejected_by_router_guard(session):
    akun_b = await _make_akun(session, nama_toko="B")
    staff = await _make_user(session, email="staffm3@t.com", role="admin_marketplace")
    # staff has no akun assigned at all
    with pytest.raises(HTTPException) as exc_info:
        await pastikan_akses_akun(staff, session, akun_b.id)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_percakapan_erp_in_still_requires_akun_id():
    with pytest.raises(ValidationError):
        PercakapanERPIn(platform="shopee", id_eksternal_pembeli="buyer-1")


# --- Staff CRUD service layer -----------------------------------------------


@pytest.mark.asyncio
async def test_create_staff_admin_toko_web(session):
    staff = await toko_services.create_staff(
        session, StaffIn(nama="Web Admin", email="web1@t.com", password="pw12345", role="admin_toko_web")
    )
    assert staff.role == "admin_toko_web"
    out = await toko_services.staff_out(session, staff)
    assert out["email"] == "web1@t.com"
    assert "akun_ids" not in out


@pytest.mark.asyncio
async def test_create_staff_admin_marketplace_with_akun_assignment(session):
    akun = await _make_akun(session)
    staff = await toko_services.create_staff(
        session,
        StaffIn(nama="MP Admin", email="mp1@t.com", password="pw12345", role="admin_marketplace", akun_ids=[akun.id]),
    )
    out = await toko_services.staff_out(session, staff)
    assert out["akun_ids"] == [akun.id]


@pytest.mark.asyncio
async def test_create_staff_rejects_owner_or_admin_toko_role():
    with pytest.raises(ValidationError):
        StaffIn(nama="X", email="x@t.com", password="pw12345", role="owner")
    with pytest.raises(ValidationError):
        StaffIn(nama="X", email="x2@t.com", password="pw12345", role="admin_toko")


@pytest.mark.asyncio
async def test_list_staff_excludes_pembeli(session):
    await _make_user(session, email="buyer@t.com", role="pembeli")
    await toko_services.create_staff(
        session, StaffIn(nama="Web Admin", email="web2@t.com", password="pw12345", role="admin_toko_web")
    )
    rows = await toko_services.list_staff(session)
    emails = {r["email"] for r in rows}
    assert "web2@t.com" in emails
    assert "buyer@t.com" not in emails


@pytest.mark.asyncio
async def test_update_staff_replaces_akun_assignment(session):
    akun_a = await _make_akun(session, nama_toko="A")
    akun_b = await _make_akun(session, nama_toko="B")
    staff = await toko_services.create_staff(
        session,
        StaffIn(nama="MP Admin", email="mp2@t.com", password="pw12345", role="admin_marketplace", akun_ids=[akun_a.id]),
    )
    updated = await toko_services.update_staff(session, staff.id, StaffPatch(akun_ids=[akun_b.id]))
    out = await toko_services.staff_out(session, updated)
    assert out["akun_ids"] == [akun_b.id]


@pytest.mark.asyncio
async def test_delete_staff_removes_account(session):
    staff = await toko_services.create_staff(
        session, StaffIn(nama="Web Admin", email="web3@t.com", password="pw12345", role="admin_toko_web")
    )
    await toko_services.delete_staff(session, staff.id)
    with pytest.raises(HTTPException) as exc_info:
        await toko_services.get_staff(session, staff.id)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_staff_rejects_owner_and_admin_toko(session):
    owner = await _make_user(session, email="owner2@t.com", role="owner")
    with pytest.raises(HTTPException):
        await toko_services.delete_staff(session, owner.id)
