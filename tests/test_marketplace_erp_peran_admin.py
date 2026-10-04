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


async def _user(session, email, role, nama=None, username=None):
    u = UserMarketplaceErp(
        nama=nama or role.title(), email=email, username=username or email.split("@")[0],
        password_hash=hash_password("rahasia123"), role=role,
    )
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
        payload=UserCreateIn(nama="S", username="staf1", email="s@x.id", password="sementara123", role="staff"), session=session, _=owner
    )
    assert ok.role == "staff"
    with pytest.raises(HTTPException) as exc:
        await router.create_user(
            payload=UserCreateIn(nama="O2", username="owner2", email="o2@x.id", password="sementara123", role="owner"), session=session, _=owner
        )
    assert exc.value.status_code == 403
    made = await router.create_user(
        payload=UserCreateIn(nama="A2", username="admin2", email="a2@x.id", password="sementara123", role="admin"), session=session, _=admin
    )
    assert made.role == "admin"


@pytest.mark.asyncio
async def test_admin_changes_username_name_and_role_of_another_user(session):
    staff = await _user(session, "lama@x.id", "staff", nama="Lama", username="lama")
    await _user(session, "terpakai@x.id", "staff", username="terpakai")
    out = await services.update_user(session, staff.id, UserUpdateIn(username="  Budi.Baru ", nama="Baru", role="owner"))
    assert (out.username, out.nama, out.role, out.email) == ("budi.baru", "Baru", "owner", "lama@x.id")
    with pytest.raises(HTTPException) as exc:  # username already taken
        await services.update_user(session, staff.id, UserUpdateIn(username="terpakai"))
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        await services.update_user(session, "nope", UserUpdateIn(nama="x"))
    assert exc.value.status_code == 404
    with pytest.raises(ValidationError):
        UserUpdateIn(role="superadmin")
    for jelek in ("ab", "a b c", "bud@i", "-budi", "x" * 40, ""):
        with pytest.raises(ValidationError):
            UserUpdateIn(username=jelek)


@pytest.mark.asyncio
async def test_the_last_admin_cannot_be_demoted(session):
    admin = await _user(session, "a@x.id", "admin")
    with pytest.raises(HTTPException) as exc:
        await services.update_user(session, admin.id, UserUpdateIn(role="owner"))
    assert exc.value.status_code == 409
    kedua = await _user(session, "a2@x.id", "admin")
    assert (await services.update_user(session, kedua.id, UserUpdateIn(role="owner"))).role == "owner"


@pytest.mark.asyncio
async def test_username_is_required_free_text_and_unique_when_creating(session):
    admin = await _user(session, "a@x.id", "admin")
    with pytest.raises(ValidationError):
        UserCreateIn(nama="X", password="sementara123")  # username missing
    dibuat = await router.create_user(
        payload=UserCreateIn(nama="Staf", username="Staf.Gudang_1", password="sementara123"), session=session, _=admin
    )
    assert dibuat.username == "staf.gudang_1" and dibuat.email is None
    with pytest.raises(HTTPException) as exc:
        await router.create_user(payload=UserCreateIn(nama="Lagi", username="STAF.GUDANG_1", password="sementara123"), session=session, _=admin)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_login_by_username_or_email_in_any_case(session):
    from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import LoginIn

    await _user(session, "Budi@Contoh.id", "owner", username="budi.owner")
    for ident in ("budi.owner", "BUDI.OWNER", "budi@contoh.id", "Budi@Contoh.id"):
        assert (await services.authenticate_user(session, LoginIn(username=ident, password="rahasia123"))).username == "budi.owner"
    assert (await services.authenticate_user(session, LoginIn(email="budi.owner", password="rahasia123"))).username == "budi.owner"
    for salah in (LoginIn(username="budi.owner", password="salah"), LoginIn(username="tidakada", password="rahasia123")):
        with pytest.raises(HTTPException) as exc:
            await services.authenticate_user(session, salah)
        assert exc.value.status_code == 401
    with pytest.raises(ValidationError):
        LoginIn(password="rahasia123")


@pytest.mark.asyncio
async def test_profile_lets_everyone_set_a_valid_email_but_never_change_username_or_role(session, monkeypatch):
    async def lolos(email):
        return email.strip().lower()

    monkeypatch.setattr(services, "periksa_email", lolos)
    staff = await _user(session, "s@x.id", "staff", nama="Lama", username="staf1")
    await _user(session, "dipakai@x.id", "owner", username="o1")
    out = await router.update_profil(payload=ProfilUpdateIn(nama="  Nama Baru ", email=" Baru@Contoh.ID "), session=session, user=staff)
    assert (out.nama, out.email, out.username, out.role) == ("Nama Baru", "baru@contoh.id", "staf1", "staff")
    # Only the fields that were sent change.
    out = await router.update_profil(payload=ProfilUpdateIn(nama="Hanya Nama"), session=session, user=staff)
    assert (out.nama, out.email) == ("Hanya Nama", "baru@contoh.id")
    with pytest.raises(HTTPException) as exc:  # someone else's email
        await router.update_profil(payload=ProfilUpdateIn(email="dipakai@x.id"), session=session, user=staff)
    assert exc.value.status_code == 409
    out = await router.update_profil(payload=ProfilUpdateIn(email=""), session=session, user=staff)  # empty removes it
    assert out.email is None
    for terlarang in ({"username": "lain"}, {"role": "admin"}, {"nama": "x", "username": "lain"}):
        with pytest.raises(ValidationError):
            ProfilUpdateIn(**terlarang)
    with pytest.raises(ValidationError):
        ProfilUpdateIn(email="bukan-email")  # syntax is checked before saving
    with pytest.raises(ValidationError):
        ProfilUpdateIn(nama="")


def test_email_check_rejects_a_domain_that_cannot_receive_mail(monkeypatch):
    from email_validator import EmailUndeliverableError

    def tidak_ada(email, **kw):
        raise EmailUndeliverableError("The domain name tidak-ada.id does not exist.")

    monkeypatch.setattr("email_validator.validate_email", tidak_ada)
    with pytest.raises(HTTPException) as exc:
        services._periksa_email_sync("a@tidak-ada.id")
    assert exc.value.status_code == 422 and "domain" in exc.value.detail


@pytest.mark.asyncio
async def test_an_admin_is_always_created_and_never_with_a_fixed_password(session, monkeypatch):
    monkeypatch.delenv("MARKETPLACE_ERP_ADMIN_PASSWORD", raising=False)
    await _user(session, "o@x.id", "owner")
    admin = await seeder._ensure_admin(session)
    assert admin.role == "admin" and admin.username == "admin" and admin.must_change_password is True
    assert admin.email is None
    assert await seeder._ensure_admin(session) is None  # already has one
    # The owner stays an owner: the admin is a separate, higher account.
    assert (await session.get(UserMarketplaceErp, (await _user(session, "o2@x.id", "owner")).id)).role == "owner"


@pytest.mark.asyncio
async def test_admin_password_can_come_from_the_environment_and_username_never_collides(session, monkeypatch):
    from shared.security import verify_password

    monkeypatch.setenv("MARKETPLACE_ERP_ADMIN_PASSWORD", "dariDigitalOcean1")
    await _user(session, "x@x.id", "owner", username="admin")  # the name "admin" is taken by someone else
    admin = await seeder._ensure_admin(session)
    assert admin.username == "admin2" and verify_password("dariDigitalOcean1", admin.password_hash)


@pytest.mark.asyncio
async def test_accounts_from_before_usernames_get_one_from_their_email(session):
    session.add_all([
        UserMarketplaceErp(nama="A", email="Budi.Santoso@x.id", password_hash="x", role="owner"),
        UserMarketplaceErp(nama="B", email="budi.santoso@y.id", password_hash="x", role="staff"),
        UserMarketplaceErp(nama="C", email="c@x.id", password_hash="x", role="staff", username="sudah.ada"),
    ])
    await session.flush()
    assert await seeder._isi_username_kosong(session) == 2
    from sqlalchemy import select

    nama = {u.nama: u.username for u in (await session.execute(select(UserMarketplaceErp))).scalars()}
    assert nama["C"] == "sudah.ada" and {nama["A"], nama["B"]} == {"budi.santoso", "budi.santoso2"}
    assert seeder.basis_username("!!") == "pengguna" or len(seeder.basis_username("!!")) >= 3


@pytest.mark.asyncio
async def test_admin_deletes_an_account_with_its_shop_assignments_but_never_itself_or_the_last_admin(session):
    from sqlalchemy import select

    from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn, StaffAkunIn
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import StaffAkunMarketplace

    admin = await _user(session, "a@x.id", "admin")
    staff = await _user(session, "s@x.id", "staff")
    akun = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="T"))
    await services.assign_staff_akun(session, StaffAkunIn(user_id=staff.id, akun_id=akun.id))

    for target, kode in ((admin.id, 409), ("nope", 404)):  # yourself / unknown
        with pytest.raises(HTTPException) as exc:
            await services.hapus_user(session, target, admin)
        assert exc.value.status_code == kode

    await services.hapus_user(session, staff.id, admin)
    assert await session.get(UserMarketplaceErp, staff.id) is None
    assert (await session.execute(select(StaffAkunMarketplace))).scalars().all() == []

    owner = await _user(session, "o@x.id", "owner")  # the only admin cannot be removed by anyone else either
    with pytest.raises(HTTPException) as exc:
        await services.hapus_user(session, admin.id, owner)
    assert exc.value.status_code == 409


def test_only_an_admin_may_delete_users():
    import inspect

    assert "ADMIN_ONLY" in inspect.getsource(router.delete_user)
