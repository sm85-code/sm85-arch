"""generate_spp_massal harus bisa dipakai dengan nominal SPP kustom per
generate (bendahara unit boleh menentukan tarif sendiri), dan tetap jatuh
ke default global (SPP_NOMINAL) kalau nominal tidak diisi.
"""
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.madrasah.modules.madrasah.application import services
from tenants.madrasah.modules.madrasah.infrastructure.database import MadrasahBase
from tenants.madrasah.modules.madrasah.infrastructure.models import SantriMadrasah


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
async def test_generate_spp_massal_pakai_nominal_kustom(session):
    santri = SantriMadrasah(nama="Santri Kustom")
    session.add(santri)
    await session.flush()

    tagihan = await services.generate_spp_massal(session, nominal=Decimal("75000"))
    assert len(tagihan) == 1
    assert tagihan[0].nominal == Decimal("75000")


@pytest.mark.asyncio
async def test_generate_spp_massal_default_kalau_nominal_kosong(session):
    santri = SantriMadrasah(nama="Santri Default")
    session.add(santri)
    await session.flush()

    tagihan = await services.generate_spp_massal(session)
    assert len(tagihan) == 1
    assert tagihan[0].nominal == services.DEFAULT_SPP_NOMINAL
