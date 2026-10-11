"""Actual PostgreSQL cross-session locks, budget reservation and lease exclusion."""
import asyncio
import os
from uuid import uuid4
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text, select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import UserMarketplaceErp, AiTurn, AiDailyBudget
from tenants.marketplace_erp.modules.marketplace_erp.application.assistant import service, config
from tenants.marketplace_erp.modules.marketplace_erp.application.assistant.schemas import TurnIn

pytestmark = pytest.mark.skipif(not os.getenv('TEST_DATABASE_URL'), reason='TEST_DATABASE_URL tidak tersedia')


@pytest_asyncio.fixture
async def pg(monkeypatch):
    raw = os.environ['TEST_DATABASE_URL'].replace('postgresql://', 'postgresql+asyncpg://')
    schema = 'assistant_' + uuid4().hex[:12]
    root = create_async_engine(raw)
    async with root.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(raw, connect_args={'server_settings': {'search_path': schema}})
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user = UserMarketplaceErp(id='u', nama='Admin', username='admin', role='admin', password_hash='test', session_version=0)
    async with factory() as s:
        s.add(user)
        await s.commit()
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-only')
    monkeypatch.setenv('ERP_AI_ENABLED', 'true')
    try:
        yield factory, user
    finally:
        await engine.dispose()
        async with root.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await root.dispose()


@pytest.mark.asyncio
async def test_concurrent_duplicate_reserves_once(pg):
    factory, user = pg
    payload = TurnIn(operation_id=uuid4(), prompt='Ringkas data toko')
    async def enqueue():
        async with factory() as s:
            return (await service.enqueue(s, user, payload)).id
    ids = await asyncio.gather(enqueue(), enqueue(), enqueue())
    assert len(set(ids)) == 1
    async with factory() as s:
        assert (await s.execute(select(func.count()).select_from(AiTurn))).scalar_one() == 1
        budget = await s.get(AiDailyBudget, config.day())
        assert budget.turns == 1 and budget.reserved_usd == config.TURN_USD
    claims = await asyncio.gather(service.claim(factory), service.claim(factory))
    assert sum(v is not None for v in claims) == 1


@pytest.mark.asyncio
async def test_concurrent_budget_cap_never_over_reserves(pg, monkeypatch):
    factory, user = pg
    monkeypatch.setattr(config, 'DAILY_USD', config.TURN_USD)
    async def enqueue():
        async with factory() as s:
            try:
                return (await service.enqueue(s, user, TurnIn(operation_id=uuid4(), prompt='Ringkas toko'))).id
            except HTTPException as error:
                assert error.status_code == 429
                return None
    ids = await asyncio.gather(enqueue(), enqueue(), enqueue())
    assert sum(v is not None for v in ids) == 1
    async with factory() as s:
        budget = await s.get(AiDailyBudget, config.day())
        assert budget.reserved_usd <= config.DAILY_USD and budget.turns == 1


@pytest.mark.asyncio
async def test_concurrent_confirm_queues_once_and_advisor_reserves_before_model(pg, monkeypatch):
    """One focused check of both cross-process decision and advisor quota locks."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from tenants.marketplace_erp.modules.marketplace_erp.application.assistant import tools
    from tenants.marketplace_erp.modules.marketplace_erp.application.assistant.schemas import DecisionIn
    from tenants.marketplace_erp.modules.marketplace_erp.application import services
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import ads_ai
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import AiToolReceipt
    factory, user = pg
    monkeypatch.setattr(tools.services, 'akun_shopee_pengelolaan', AsyncMock(return_value=SimpleNamespace(id='shop')))
    async with factory() as s:
        turn = await service.enqueue(s, user, TurnIn(operation_id=uuid4(), prompt='jeda iklan 7', mode='perintah', akun_id='shop'))
        result, _ = await service.tool_call(s, user, turn, {'name':'ubah_iklan','input':{'campaign_id':7,'aksi':'pause'}})
        await service.finish(s, turn, 'completed', 'Draf')
        draft_id = result['draft_id']
    async def confirm():
        async with factory() as s:
            return (await service.decide(s, user, draft_id, DecisionIn(decision='confirm'))).id
    ids = await asyncio.gather(confirm(), confirm(), confirm())
    assert len(set(ids)) == 1
    async with factory() as s:
        assert (await s.get(AiToolReceipt,draft_id)).status == 'confirmed'
    monkeypatch.setattr(services, 'ADS_AI_BATAS_HARIAN', 1)
    monkeypatch.setattr(services, 'daftar_kampanye_iklan', AsyncMock(return_value={'hari':7,'saldo':0,'kampanye':[{'campaign_id':'7','status':'ongoing'}]}))
    model = AsyncMock(return_value={'mentah':{'ringkasan':'OK','saran':[]},'token_masuk':1,'token_keluar':1,'model':'claude-sonnet-5-5'})
    monkeypatch.setattr(ads_ai, 'minta_saran', model)
    async def advisor():
        async with factory() as s:
            try:
                return await services.saran_ai_iklan(s, SimpleNamespace(id=None,nama_toko='Toko'), user)
            except HTTPException as error:
                assert error.status_code == 429
                return None
    results = await asyncio.gather(advisor(), advisor())
    assert sum(r is not None for r in results) == 1 and model.await_count == 1
