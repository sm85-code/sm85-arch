import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import UserMarketplaceErp, AiTurn, AiToolReceipt, AiDailyBudget, AiWorkerLease
from tenants.marketplace_erp.modules.marketplace_erp.application.assistant import service, tools, config
from tenants.marketplace_erp.modules.marketplace_erp.application.assistant.schemas import TurnIn


@pytest_asyncio.fixture
async def setup(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-only')
    monkeypatch.setenv('ERP_AI_ENABLED', 'true')
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        user = UserMarketplaceErp(id='admin', nama='Admin', username='admin', role='admin', session_version=0, password_hash='test')
        s.add(user)
        await s.commit()
    yield factory, user
    await engine.dispose()


def message(**kwargs):
    return TurnIn(operation_id=uuid4(), prompt='Ringkas toko saya', **kwargs)


def response(*blocks, stop='end_turn'):
    return SimpleNamespace(content=[Block(b) for b in blocks], usage=SimpleNamespace(input_tokens=100, output_tokens=50), stop_reason=stop)


class Block:
    def __init__(self, data):
        self.data = data

    def model_dump(self, **kwargs):
        return self.data


@pytest.mark.asyncio
async def test_enqueue_dedup_payload_conflict_and_budget(setup, monkeypatch):
    factory, user = setup
    payload = message()
    async with factory() as s:
        first = await service.enqueue(s, user, payload)
        replay = await service.enqueue(s, user, payload)
        assert first.id == replay.id
        budget = await s.get(AiDailyBudget, config.day())
        assert budget.turns == 1 and budget.reserved_usd == config.TURN_USD
        with pytest.raises(HTTPException) as error:
            await service.enqueue(s, user, payload.model_copy(update={'prompt': 'Different'}))
        assert error.value.status_code == 409
        monkeypatch.setattr(config, 'DAILY_USD', config.TURN_USD)
        with pytest.raises(HTTPException) as error:
            await service.enqueue(s, user, message())
        assert error.value.status_code == 429


@pytest.mark.asyncio
async def test_queued_turn_model_loop_usage_and_read_receipt(setup, monkeypatch):
    factory, user = setup
    model = AsyncMock(side_effect=[response({'type': 'tool_use', 'id': 'tool1', 'name': 'daftar_toko', 'input': {}}, stop='tool_use'), response({'type': 'text', 'text': 'Toko A tersedia.'})])
    monkeypatch.setattr(service, 'model_call', model)
    monkeypatch.setattr(tools.services, 'list_akun_marketplace', AsyncMock(return_value=[SimpleNamespace(id='s1', nama_toko='Toko A', platform='shopee')]))
    async with factory() as s:
        turn = await service.enqueue(s, user, message())
        ident = turn.id
    assert await service.claim(factory) == ident
    await service.process(factory, ident)
    await service.release(factory, ident)
    async with factory() as s:
        turn = await s.get(AiTurn, ident)
        assert turn.status == 'completed' and turn.answer == 'Toko A tersedia.'
        assert turn.input_tokens == 200 and turn.output_tokens == 100
        receipt = (await s.execute(select(AiToolReceipt))).scalar_one()
        assert receipt.status == 'succeeded' and not receipt.is_write
        assert json.loads(receipt.result_json) == [{'id': 's1', 'nama': 'Toko A', 'platform': 'shopee'}]
        budget = await s.get(AiDailyBudget, config.day())
        assert budget.reserved_usd == 0 and budget.spent_usd == turn.cost_usd
    assert all(not t['name'].startswith('ubah') for t in model.call_args_list[0].args[2])


@pytest.mark.asyncio
async def test_write_replay_and_transport_failure_halts(setup, monkeypatch):
    factory, user = setup
    monkeypatch.setattr(tools.services, 'akun_shopee_pengelolaan', AsyncMock(return_value=SimpleNamespace(id='shop')))
    execute = AsyncMock(return_value={'ok': True, 'request_id': 'r'})
    monkeypatch.setattr(tools, 'execute', execute)
    async with factory() as s:
        turn = await service.enqueue(s, user, message(mode='perintah', akun_id='shop'))
        block = {'name': 'ubah_iklan', 'input': {'campaign_id': 7, 'aksi': 'pause'}}
        a, stop = await service.tool_call(s, user, turn, block)
        b, stop2 = await service.tool_call(s, user, turn, block)
        assert a == b and not stop and not stop2 and execute.await_count == 1
        execute.side_effect = HTTPException(504, 'timeout')
        block['input']['aksi'] = 'resume'
        result, stop = await service.tool_call(s, user, turn, block)
        assert stop and result['error'] == 'timeout'
        _, stop = await service.tool_call(s, user, turn, block)
        assert stop and execute.await_count == 2
        receipt = (await s.execute(select(AiToolReceipt).where(AiToolReceipt.status == 'unknown'))).scalar_one()
        assert receipt.is_write


@pytest.mark.asyncio
async def test_read_mode_blocks_mutation_and_revoked_admin(setup, monkeypatch):
    factory, user = setup
    execute = AsyncMock()
    monkeypatch.setattr(tools, 'execute', execute)
    async with factory() as s:
        turn = await service.enqueue(s, user, message())
        result, stop = await service.tool_call(s, user, turn, {'name': 'ubah_iklan', 'input': {'campaign_id': 7, 'aksi': 'pause'}})
        assert 'Mode Tanya' in result['error']
        stored = await s.get(UserMarketplaceErp, user.id)
        stored.role = 'staff'
        await s.commit()
        _, stop = await service.tool_call(s, user, turn, {'name': 'daftar_toko', 'input': {}})
        assert stop
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_lease_never_replays_write(setup):
    factory, user = setup
    async with factory() as s:
        turn = await service.enqueue(s, user, message())
        ident = turn.id
    await service.claim(factory)
    async with factory() as s:
        lease = await s.get(AiWorkerLease, 'global')
        lease.expires_at = service.now() - timedelta(seconds=1)
        s.add(AiToolReceipt(id='receipt', turn_id=ident, fingerprint='f', tool='ubah_iklan', is_write=True, status='running', arguments_json='{}', result_json='{}'))
        await s.commit()
    assert await service.claim(factory) is None
    async with factory() as s:
        turn = await s.get(AiTurn, ident)
        assert turn.status == 'unknown' and turn.active_key is None
        assert (await s.get(AiToolReceipt, 'receipt')).status == 'unknown'
        budget = await s.get(AiDailyBudget, config.day())
        assert budget.reserved_usd == 0 and budget.spent_usd == config.TURN_USD


@pytest.mark.asyncio
async def test_no_key_and_cross_user_history(setup, monkeypatch):
    factory, user = setup
    async with factory() as s:
        turn = await service.enqueue(s, user, message())
        with pytest.raises(HTTPException) as error:
            await service.owned_conversation(s, SimpleNamespace(id='other'), turn.conversation_id)
        assert error.value.status_code == 404
        monkeypatch.delenv('ANTHROPIC_API_KEY')
        with pytest.raises(HTTPException) as error:
            await service.enqueue(s, user, message())
        assert error.value.status_code == 503


def test_fixed_tool_surface_no_nomination_urls_or_other_tenants():
    assert 'buat_gmv' in tools.TOOLS
    assert not any('pendaftaran' in n or 'url' in n for n in tools.TOOLS)
    assert not any(d['input_schema'].get('properties', {}).get('reference_id') for d in tools.definitions(True))
    assert config.MODEL.startswith('claude-sonnet')
    assert config.cost(1000000, 1000000) == Decimal(12)


@pytest.mark.asyncio
async def test_unknown_write_blocks_new_turn_same_shop(setup, monkeypatch):
    factory, user = setup
    monkeypatch.setattr(tools.services, 'akun_shopee_pengelolaan', AsyncMock(return_value=SimpleNamespace(id='shop')))
    execute = AsyncMock(side_effect=HTTPException(504, 'timeout'))
    monkeypatch.setattr(tools, 'execute', execute)
    async with factory() as s:
        turn = await service.enqueue(s, user, message(mode='perintah', akun_id='shop'))
        await service.tool_call(s, user, turn, {'name': 'ubah_iklan', 'input': {'campaign_id': 7, 'aksi': 'pause'}})
        await service.finish(s, turn, 'unknown', 'periksa dulu')
        new = await service.enqueue(s, user, message(mode='perintah', akun_id='shop'))
        result, stop = await service.tool_call(s, user, new, {'name': 'ubah_iklan', 'input': {'campaign_id': 7, 'aksi': 'resume'}})
        assert stop and 'belum pasti' in result['error']
        assert execute.await_count == 1


@pytest.mark.asyncio
async def test_admin_routes_enforce_role_and_ownership(setup):
    import httpx
    from fastapi import FastAPI
    from tenants.marketplace_erp.adapters.api.v1.assistant_router import router
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
    factory, user = setup
    app = FastAPI()
    app.include_router(router)
    current = user
    async def auth():
        return current
    async def db():
        async with factory() as s:
            yield s
    app.dependency_overrides[get_current_user_marketplace_erp] = auth
    app.dependency_overrides[get_db_marketplace_erp] = db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for role in ('owner', 'staff'):
            current = SimpleNamespace(role=role, id=role)
            assert (await client.get('/asisten/status')).status_code == 403
            assert (await client.post('/asisten/pesan', json=message().model_dump(mode='json'))).status_code == 403
        current = user
        sent = await client.post('/asisten/pesan', json=message().model_dump(mode='json'))
        assert sent.status_code == 202
        ident = sent.json()['conversation_id']
        assert (await client.get('/asisten/percakapan/'+ident)).json()['items'][0]['status'] == 'queued'
        current = SimpleNamespace(role='admin', id='other-admin')
        assert (await client.get('/asisten/percakapan/'+ident)).status_code == 404


@pytest.mark.asyncio
async def test_semantic_receipt_ignores_key_order_and_decimal_format(setup, monkeypatch):
    factory, user = setup
    monkeypatch.setattr(tools.services, 'akun_shopee_pengelolaan', AsyncMock(return_value=SimpleNamespace(id='shop')))
    execute = AsyncMock(return_value={'ok': True})
    monkeypatch.setattr(tools, 'execute', execute)
    async with factory() as s:
        turn = await service.enqueue(s, user, message(mode='perintah', akun_id='shop'))
        a = {'name': 'ubah_iklan', 'input': {'campaign_id': 7, 'aksi': 'change_budget', 'budget': '10000.00'}}
        b = {'name': 'ubah_iklan', 'input': {'budget': 10000, 'aksi': 'change_budget', 'campaign_id': '7', 'roas_target': None}}
        await service.tool_call(s, user, turn, a)
        await service.tool_call(s, user, turn, b)
        assert execute.await_count == 1


@pytest.mark.asyncio
async def test_real_tool_refuses_product_from_other_shop(setup, monkeypatch):
    factory, user = setup
    monkeypatch.setattr(tools.services, 'akun_shopee_pengelolaan', AsyncMock(return_value=SimpleNamespace(id='selected')))
    monkeypatch.setattr(tools.services, 'get_katalog_shopee', AsyncMock(return_value=(SimpleNamespace(akun_id='other', item_id='7'), 'Other shop')))
    write = AsyncMock()
    monkeypatch.setattr(tools.management, 'update_item', write)
    async with factory() as s:
        turn = await service.enqueue(s, user, message(mode='perintah', akun_id='selected'))
        _, stop = await service.tool_call(s, user, turn, {'name': 'ubah_produk', 'input': {'katalog_id': 'foreign-product', 'perubahan': {'item_name': 'new'}}})
        assert not stop
        receipt = (await s.execute(select(AiToolReceipt))).scalar_one()
        assert receipt.status == 'rejected' and 'toko' in receipt.result_json
    write.assert_not_awaited()
