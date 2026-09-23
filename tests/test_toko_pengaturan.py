"""Exercises the toko-web global settings (product decision B) -- pickup vs
drop_off order-processing method -- against a real (SQLite, in-memory)
async session, same pattern as the other tests/test_toko_*.py files.
"""
import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.toko.modules.toko.application import services
from tenants.toko.modules.toko.application.schemas import PengaturanPatch
from tenants.toko.modules.toko.infrastructure.auth import require_roles_toko
from tenants.toko.modules.toko.infrastructure.database import TokoBase
from tenants.toko.modules.toko.infrastructure.models import UserToko


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(TokoBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_get_or_create_pengaturan_creates_default_row(session):
    pengaturan = await services.get_or_create_pengaturan(session)
    assert pengaturan.metode_proses_pesanan == "drop_off"

    # Calling again must not create a second row -- same singleton returned.
    again = await services.get_or_create_pengaturan(session)
    assert again.id == pengaturan.id


@pytest.mark.asyncio
async def test_update_pengaturan_accepts_valid_value(session):
    updated = await services.update_pengaturan(session, PengaturanPatch(metode_proses_pesanan="pickup"))
    assert updated.metode_proses_pesanan == "pickup"

    reread = await services.get_or_create_pengaturan(session)
    assert reread.metode_proses_pesanan == "pickup"


@pytest.mark.asyncio
async def test_update_pengaturan_rejects_invalid_value(session):
    with pytest.raises(ValidationError):
        PengaturanPatch(metode_proses_pesanan="ambil_sendiri")


def test_pengaturan_out_shape(session=None):
    from datetime import datetime, timezone

    from tenants.toko.modules.toko.infrastructure.models import PengaturanToko

    pengaturan = PengaturanToko(
        id="global", metode_proses_pesanan="drop_off", updated_at=datetime.now(timezone.utc)
    )
    out = services.pengaturan_out(pengaturan)
    assert out["metode_proses_pesanan"] == "drop_off"
    assert "updated_at" in out


@pytest.mark.asyncio
async def test_require_roles_toko_rejects_non_admin_for_pengaturan():
    guard = require_roles_toko("admin_toko", "owner")
    pembeli = UserToko(nama="Pembeli", email="pembeli-pengaturan@test.com", password_hash="x", role="pembeli")

    with pytest.raises(HTTPException) as exc_info:
        await guard(user=pembeli)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_roles_toko_allows_admin_for_pengaturan():
    guard = require_roles_toko("admin_toko", "owner")
    admin = UserToko(nama="Admin", email="admin-pengaturan@test.com", password_hash="x", role="admin_toko")

    result = await guard(user=admin)
    assert result is admin
