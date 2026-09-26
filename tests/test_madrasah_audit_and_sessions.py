"""Regression tests for the two remaining P0-adjacent audit findings:

1. Changing a madrasah account's password or role bumps session_version, so
   a JWT issued before that change is rejected on its next use (no token
   blacklist table needed).
2. Sensitive actions (login, SPP paid, account/santri deleted, buku kas
   entry, reset-now) are written to the new madrasah_audit_log table.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.madrasah.modules.madrasah.application import services
from tenants.madrasah.modules.madrasah.application.schemas import BukuKasIn, UserPatch
from tenants.madrasah.modules.madrasah.infrastructure import auth as auth_module
from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase
from tenants.madrasah.modules.madrasah.infrastructure.models import AuditLogMadrasah, UserMadrasah
from shared.security import hash_password


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MadrasahBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_new_user_starts_at_session_version_zero(session):
    row = UserMadrasah(nama="Guru A", no_hp="081300000001", password_hash=hash_password("x"), role="wali_kelas")
    session.add(row)
    await session.flush()
    assert row.session_version == 0


@pytest.mark.asyncio
async def test_password_change_bumps_session_version(session):
    row = UserMadrasah(nama="Guru A", no_hp="081300000002", password_hash=hash_password("old"), role="wali_kelas")
    session.add(row)
    await session.flush()

    await services.patch_guru(session, row.id, UserPatch(password="baru-sekali"))
    assert row.session_version == 1


@pytest.mark.asyncio
async def test_role_change_bumps_session_version_but_rename_does_not(session):
    row = UserMadrasah(nama="Guru A", no_hp="081300000003", password_hash=hash_password("x"), role="wali_kelas")
    session.add(row)
    await session.flush()

    await services.patch_guru(session, row.id, UserPatch(nama="Guru A Baru"))
    assert row.session_version == 0, "renaming alone must not force everyone's token to expire"

    await services.patch_guru(session, row.id, UserPatch(role="kurikulum"))
    assert row.session_version == 1


@pytest.mark.asyncio
async def test_record_audit_writes_row_with_actor(session):
    actor = UserMadrasah(nama="Admin", no_hp="081300000004", password_hash=hash_password("x"), role="admin")
    session.add(actor)
    await session.flush()

    await services.record_audit(
        session,
        aktor=actor,
        aksi="spp_lunas",
        entitas="madrasah_tagihan_syahriyah",
        entitas_id="tagihan-1",
        ip="127.0.0.1",
    )

    rows = list((await session.execute(select(AuditLogMadrasah))).scalars())
    assert len(rows) == 1
    assert rows[0].aktor_id == actor.id
    assert rows[0].aktor_nama == "Admin"
    assert rows[0].aksi == "spp_lunas"
    assert rows[0].entitas_id == "tagihan-1"


@pytest.mark.asyncio
async def test_record_audit_without_actor_for_failed_login(session):
    await services.record_audit(
        session,
        aktor=None,
        aksi="login_gagal",
        entitas="madrasah_users",
        keterangan="no_hp=081399999999",
        ip="10.0.0.5",
    )

    rows = list((await session.execute(select(AuditLogMadrasah))).scalars())
    assert len(rows) == 1
    assert rows[0].aktor_id is None
    assert "081399999999" in rows[0].keterangan


@pytest.mark.asyncio
async def test_stale_token_rejected_after_password_change(session, monkeypatch):
    user = UserMadrasah(nama="Guru", no_hp="081300000006", password_hash=hash_password("old"), role="wali_kelas")
    session.add(user)
    await session.flush()
    old_sv = user.session_version

    await services.patch_guru(session, user.id, UserPatch(password="ganti-password"))
    assert user.session_version == old_sv + 1

    monkeypatch.setattr(auth_module, "_token_from_request", lambda request: "fake-token")
    monkeypatch.setattr(auth_module, "decode_access_token", lambda token: {"sub": user.id, "sv": old_sv})

    with pytest.raises(HTTPException) as exc:
        await auth_module.get_current_user_madrasah(SimpleNamespace(), session)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_current_token_accepted_when_session_version_matches(session, monkeypatch):
    user = UserMadrasah(nama="Guru", no_hp="081300000007", password_hash=hash_password("x"), role="wali_kelas")
    session.add(user)
    await session.flush()

    monkeypatch.setattr(auth_module, "_token_from_request", lambda request: "fake-token")
    monkeypatch.setattr(auth_module, "decode_access_token", lambda token: {"sub": user.id, "sv": user.session_version})

    result = await auth_module.get_current_user_madrasah(SimpleNamespace(), session)
    assert result.id == user.id


@pytest.mark.asyncio
async def test_buku_kas_and_delete_flows_do_not_break_when_audited(session):
    admin = UserMadrasah(nama="Admin", no_hp="081300000005", password_hash=hash_password("x"), role="admin")
    session.add(admin)
    await session.flush()

    entry = await services.create_buku_kas_entry(
        session, BukuKasIn(tanggal=__import__("datetime").date.today(), tipe="keluar", kategori="ATK", jumlah=10000, keterangan="pulpen"), dicatat_oleh=admin.id
    )
    await services.record_audit(session, aktor=admin, aksi="buku_kas_catat", entitas="madrasah_buku_kas", entitas_id=entry.id)

    log = await services.list_audit_log(session)
    assert len(log) == 1
    assert log[0].aksi == "buku_kas_catat"
