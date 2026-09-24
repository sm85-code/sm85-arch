from __future__ import annotations

import os

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.api.scope import assert_can_mutate_period
from modules.identity.infrastructure.models import ClosedPeriod, SystemControl, User
from modules.siabumdes.application.closing import run_monthly_close, undo_monthly_close, SUB_UTANG_BH_UNIT
from modules.siabumdes.infrastructure.models import Account, UnitUsaha
from shared.database import Base
from shared.coa_taxonomy import SUB_IKHTISAR_LR, SUB_SALDO_LABA

DATABASE_URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL.startswith("postgresql"), reason="needs postgres")

_TABLES = None

def _tables():
    global _TABLES
    if _TABLES is None:
        from modules.siabumdes.infrastructure.models import JournalEntry, JournalItem, Transaction
        _TABLES = [Account.__table__, Transaction.__table__, JournalEntry.__table__, JournalItem.__table__,
                   UnitUsaha.__table__, ClosedPeriod.__table__, SystemControl.__table__, User.__table__]
    return _TABLES

@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=_tables()))
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=_tables()))
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=_tables()))
    await engine.dispose()

async def _seed_unit(session):
    unit = UnitUsaha(code="UU01", name="Unit Dagang", active=True)
    session.add(unit)
    slugs = [SUB_IKHTISAR_LR, SUB_SALDO_LABA, SUB_UTANG_BH_UNIT]
    for i, slug in enumerate(slugs):
        session.add(Account(code=f"8{i}00", name=slug, category="ekuitas", subcategory=slug,
                             normal_balance="kredit", group_code="UU01", active=True))
    await session.flush()
    return unit

@pytest.mark.asyncio
async def test_closed_unit_usaha_period_blocks_new_transactions(session):
    unit = await _seed_unit(session)
    result = await run_monthly_close(session, period="2026-09", group="UU01", actor_id="admin-1")
    assert result["closed"] is True and result["group"] == "UU01"

    user = User(username="bendahara2", email="b2@test.local", name="B", password_hash="x",
                role="bendahara", active=True)
    session.add(user)
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await assert_can_mutate_period(session, user, "2026-09-15", unit_usaha_id=unit.id)
    assert exc_info.value.status_code == 400
    assert "UU01" in exc_info.value.detail

    # BUMDES-level (unit_usaha_id=None) same month must remain unaffected.
    await assert_can_mutate_period(session, user, "2026-09-20", unit_usaha_id=None)
    # Different unit, same month, must remain unaffected (per-group isolation).
    await assert_can_mutate_period(session, user, "2026-09-20", unit_usaha_id="nonexistent-unit-id")

@pytest.mark.asyncio
async def test_double_close_unit_usaha_period_rejected(session):
    await _seed_unit(session)
    await run_monthly_close(session, period="2026-09", group="UU01", actor_id="admin-1")
    with pytest.raises(ValueError):
        await run_monthly_close(session, period="2026-09", group="UU01", actor_id="admin-1")

    rows = (await session.execute(select(ClosedPeriod).where(ClosedPeriod.group_code == "UU01"))).scalars().all()
    assert len(rows) == 1

@pytest.mark.asyncio
async def test_unit_usaha_reopen_then_can_close_again(session):
    await _seed_unit(session)
    await run_monthly_close(session, period="2026-09", group="UU01", actor_id="admin-1")
    await undo_monthly_close(session, "2026-09", "UU01")
    rows = (await session.execute(select(ClosedPeriod).where(ClosedPeriod.group_code == "UU01"))).scalars().all()
    assert len(rows) == 0
    result = await run_monthly_close(session, period="2026-09", group="UU01", actor_id="admin-1")
    assert result["closed"] is True
