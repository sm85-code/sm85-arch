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
from tenants.marketplace_erp.modules.marketplace_erp.application.assistant.schemas import TurnIn, DecisionIn


@pytest_asyncio.fixture
async def setup(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
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


async def confirmed_call(session, user, turn, block):
    proposal, stop = await service.tool_call(session, user, turn, block)
    if stop or "draft_id" not in proposal:
        return proposal, stop
    await service.finish(session, turn, 'completed', 'Draf siap')
    execution = await service.decide(session, user, proposal['draft_id'], DecisionIn(decision='confirm'))
    draft = await session.get(AiToolReceipt, proposal['draft_id'])
    result = await service.tool_call(session, user, execution, block, authorization=draft)
    await service.finish(session, execution, 'unknown' if result[1] else 'completed', 'Hasil')
    return result


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
        assert a == b and not stop and not stop2 and execute.await_count == 0
        await confirmed_call(s, user, turn, block)
        assert execute.await_count == 1
        execute.side_effect = HTTPException(504, 'timeout')
        block['input']['aksi'] = 'resume'
        result, stop = await confirmed_call(s, user, turn, block)
        assert stop and result['error'] == 'timeout'
        draft = (await s.execute(select(AiToolReceipt).where(AiToolReceipt.turn_id == turn.id, AiToolReceipt.tool == 'ubah_iklan', AiToolReceipt.arguments_json.contains('resume')))).scalar_one()
        execution = await s.get(AiTurn, json.loads(draft.result_json)['execution_turn_id'])
        _, stop = await service.tool_call(s, user, execution, block, authorization=draft)
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
        await confirmed_call(s, user, turn, {'name': 'ubah_iklan', 'input': {'campaign_id': 7, 'aksi': 'pause'}})
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
        first, _ = await service.tool_call(s, user, turn, a)
        second, _ = await service.tool_call(s, user, turn, b)
        assert first['draft_id'] == second['draft_id'] and execute.await_count == 0
        await confirmed_call(s, user, turn, b)
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
        _, stop = await confirmed_call(s, user, turn, {'name': 'ubah_produk', 'input': {'katalog_id': 'foreign-product', 'perubahan': {'item_name': 'new'}}})
        assert not stop
        receipt = (await s.execute(select(AiToolReceipt).where(AiToolReceipt.status == 'rejected'))).scalar_one()
        assert receipt.status == 'rejected' and 'toko' in receipt.result_json
    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_truncated_answer_keeps_text_but_never_executes_tool(setup, monkeypatch):
    factory, user = setup
    monkeypatch.setattr(tools.services, 'akun_shopee_pengelolaan', AsyncMock(return_value=SimpleNamespace(id='shop')))
    monkeypatch.setattr(service, 'model_call', AsyncMock(return_value=response(
        {'type': 'text', 'text': '**Temuan awal**\nProduk A perlu diperiksa.'},
        {'type': 'tool_use', 'id': 'cut', 'name': 'ubah_iklan', 'input': {'campaign_id': 7, 'aksi': 'pause'}}, stop='max_tokens')))
    execute = AsyncMock()
    monkeypatch.setattr(tools, 'execute', execute)
    async with factory() as s:
        turn = await service.enqueue(s, user, message(mode='perintah', akun_id='shop'))
        ident = turn.id
    await service.claim(factory)
    await service.process(factory, ident)
    await service.release(factory, ident)
    async with factory() as s:
        turn = await s.get(AiTurn, ident)
        assert turn.status == 'partial'
        assert '**Temuan awal**' in turn.answer and 'belum lengkap' in turn.answer
        assert turn.active_key is None and turn.output_tokens == 50
        assert not (await s.execute(select(AiToolReceipt))).scalars().all()
        assert (await s.get(AiDailyBudget, config.day())).reserved_usd == 0
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_sales_summary_explains_cancelled_orders_outside_revenue_total(setup, monkeypatch):
    factory, user = setup
    monkeypatch.setattr(tools.services, 'laporan_dashboard', AsyncMock(return_value={
        'total_pesanan': 26, 'total_omzet': Decimal('14160386'),
        'per_tahap': [{'tahap': 'selesai', 'jumlah': 14}, {'tahap': 'dikirim', 'jumlah': 12}, {'tahap': 'dibatalkan', 'jumlah': 7}],
        'per_hari': [],
    }))
    from datetime import date
    async with factory() as s:
        turn = await service.enqueue(s, user, message())
        result = await tools.execute(s, user, turn, 'ringkasan_usaha', tools.SummaryPeriod(mulai=date(2026,10,1), selesai=date(2026,10,9)), 'f')
        assert result['total_pesanan'] == 26
        assert 'dibatalkan' not in result['definisi']['tahap_dihitung_omzet']
        assert 'per_hari' not in result

@pytest.mark.asyncio
async def test_product_search_exposes_catalogue_photo_and_real_shop_link(setup, monkeypatch):
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import AkunMarketplace
    factory, user = setup
    monkeypatch.setattr(tools.services, 'list_katalog_shopee', AsyncMock(return_value={
        'items': [{'id': 'p1', 'akun_id': 's1', 'item_id': '1234', 'nama': 'Partisi',
                   'foto_utama': 'https://cf.shopee.co.id/file/photo', 'deskripsi': 'not exposed'}], 'total': 1}))
    async with factory() as session:
        session.add(AkunMarketplace(id='s1', platform='shopee', nama_toko='Toko', id_toko_eksternal='5678'))
        await session.flush()
        turn = SimpleNamespace(mode='tanya', akun_id='s1', session_version=user.session_version)
        result = await tools.execute(session, user, turn, 'cari_produk', tools.validate('cari_produk', {'q': 'Partisi'}, 'f'), 'f')
        assert result['items'][0]['url_produk'] == 'https://shopee.co.id/product/5678/1234'
        assert result['items'][0]['foto_utama'] == 'https://cf.shopee.co.id/file/photo'
        assert 'deskripsi' not in result['items'][0]


@pytest.mark.asyncio
async def test_floating_context_survives_enqueue_and_is_reference_not_authorization(setup, monkeypatch):
    factory, user = setup
    payload = message(context={'path': '/pesanan/order-1', 'resource_id': 'order-1'})
    model = AsyncMock(return_value=response({'type': 'text', 'text': 'Siap.'}))
    monkeypatch.setattr(service, 'model_call', model)
    async with factory() as s:
        turn = await service.enqueue(s, user, payload)
        assert json.loads(turn.context_json) == {'path': '/pesanan/order-1', 'resource_id': 'order-1'}
        assert turn.mode == 'tanya'
        assert (await service.enqueue(s, user, payload)).id == turn.id
        with pytest.raises(HTTPException) as error:
            await service.enqueue(s, user, payload.model_copy(update={'context': payload.context.model_copy(update={'path': '/katalog/item-1'})}))
        assert error.value.status_code == 409
        ident = turn.id
    assert await service.claim(factory) == ident
    await service.process(factory, ident)
    messages = model.call_args.args[1]
    assert any('referensi saja' in m['content'] and 'order-1' in m['content'] for m in messages if isinstance(m['content'], str))
    assert not any(t['name'].startswith('ubah_') for t in model.call_args.args[2])


def test_floating_context_cannot_carry_external_url_or_arbitrary_data():
    from pydantic import ValidationError
    for context in [{'path': 'https://example.com'}, {'path': '//example.com'}, {'path': '/pesanan', 'tokens': 'secret'}, {'path': '/pesanan', 'resource_id': '../other'}]:
        with pytest.raises(ValidationError):
            message(context=context)


@pytest.mark.asyncio
async def test_context_read_tools_pin_shop_and_reject_foreign_order(monkeypatch):
    user = SimpleNamespace(role='admin', session_version=1)
    turn = SimpleNamespace(session_version=1, mode='tanya', akun_id='shop')
    monkeypatch.setattr(tools.services, 'akun_shopee_pengelolaan', AsyncMock(return_value=SimpleNamespace(id='shop')))
    read = AsyncMock(return_value={'shop_name': 'Shop'})
    monkeypatch.setattr(tools.commerce, 'settings_read', read)
    assert await tools.execute(None, user, turn, 'pengaturan_toko', tools.ShopSettingsRead(bagian='profil'), 'f') == {'shop_name': 'Shop'}
    assert read.call_args.args[1].id == 'shop'
    monkeypatch.setattr(tools.services, 'get_pesanan', AsyncMock(return_value=SimpleNamespace(akun_id='other')))
    with pytest.raises(HTTPException) as error:
        await tools.execute(None, user, turn, 'detail_pesanan', tools.OrderRef(pesanan_id='foreign'), 'f')
    assert error.value.status_code == 403
    send = AsyncMock()
    monkeypatch.setattr(tools.chat, 'send', send)
    monkeypatch.setattr(tools.chat, 'conversation', AsyncMock(side_effect=HTTPException(403, 'Percakapan berasal dari toko lain')))
    with pytest.raises(HTTPException) as error:
        await tools.execute(None, user, SimpleNamespace(session_version=1,mode='perintah',akun_id='shop'), 'balas_chat', tools.ChatReply(conversation_id='foreign',text='Halo'), 'f')
    assert error.value.status_code == 403
    send.assert_not_awaited()
    definitions = tools.definitions(False)
    assert {'kotak_masuk_chat', 'riwayat_chat', 'pengaturan_toko', 'detail_pesanan', 'ringkasan_pendapatan'} <= {d['name'] for d in definitions}
    assert all(not tools.TOOLS[d['name']][1] for d in definitions)


@pytest.mark.asyncio
async def test_internal_read_works_without_model_key_and_preserves_period(setup, monkeypatch):
    from tenants.marketplace_erp.modules.marketplace_erp.application.assistant import internal
    factory, user = setup
    monkeypatch.delenv('ANTHROPIC_API_KEY')
    model = AsyncMock()
    monkeypatch.setattr(service, 'model_call', model)
    monkeypatch.setattr(tools.services, 'list_akun_marketplace', AsyncMock(return_value=[SimpleNamespace(id='s', nama_toko='Toko', platform='shopee')]))
    async with factory() as s:
        turn = await service.enqueue(s, user, TurnIn(operation_id=uuid4(), prompt='daftar toko'))
        ident = turn.id
        assert turn.model == internal.MODEL and turn.reserved_usd == 0
    assert await service.claim(factory) == ident
    await service.process(factory, ident)
    await service.release(factory, ident)
    async with factory() as s:
        assert (await s.get(AiTurn, ident)).status == 'completed'
        assert (await s.get(AiDailyBudget, config.day())).turns == 0
    model.assert_not_awaited()
    assert internal.plan('jangan jeda iklan 7') is None
    assert internal.plan('ringkasan penjualan 2025 dibanding 2026') is None
    assert internal.plan('ringkasan penjualan kemarin')['input']['selesai'] != str(config.day())
    assert internal.plan('ubah budget iklan 7 menjadi 50 ribu')['input']['budget'] == '50000'


@pytest.mark.asyncio
async def test_confirm_is_owned_bound_to_draft_and_replayed_without_llm(setup, monkeypatch):
    factory, user = setup
    monkeypatch.setattr(tools.services, 'akun_shopee_pengelolaan', AsyncMock(return_value=SimpleNamespace(id='shop')))
    execute = AsyncMock(return_value={'ok': True})
    monkeypatch.setattr(tools, 'execute', execute)
    async with factory() as s:
        turn = await service.enqueue(s, user, TurnIn(operation_id=uuid4(), prompt='jeda iklan 7', mode='perintah', akun_id='shop'))
        ident = turn.id
    await service.claim(factory)
    await service.process(factory, ident)
    await service.release(factory, ident)
    execute.assert_not_awaited()
    async with factory() as s:
        draft = (await s.execute(select(AiToolReceipt))).scalar_one()
        with pytest.raises(HTTPException) as error:
            await service.decide(s, SimpleNamespace(id='other',role='admin'), draft.id, DecisionIn(decision='confirm'))
        assert error.value.status_code == 404
        await s.rollback()
        draft = (await s.execute(select(AiToolReceipt))).scalar_one()
        execution = await service.decide(s, user, draft.id, DecisionIn(decision='confirm'))
        again = await service.decide(s, user, draft.id, DecisionIn(decision='confirm'))
        assert execution.id == again.id and execution.reserved_usd == 0
        executed_id = execution.id
    await service.claim(factory)
    await service.process(factory, executed_id)
    await service.release(factory, executed_id)
    assert execute.await_count == 1
    async with factory() as s:
        assert (await s.get(AiTurn, executed_id)).status == 'completed'
        replay = await service.decide(s, user, draft.id, DecisionIn(decision='confirm'))
        assert replay.id == executed_id
    assert execute.await_count == 1


@pytest.mark.asyncio
async def test_all_variants_requires_separate_consent_and_draft_expiry(setup, monkeypatch):
    factory, user = setup
    monkeypatch.setattr(tools.services, 'akun_shopee_pengelolaan', AsyncMock(return_value=SimpleNamespace(id='shop')))
    execute = AsyncMock()
    monkeypatch.setattr(tools, 'execute', execute)
    async with factory() as s:
        turn = await service.enqueue(s, user, TurnIn(operation_id=uuid4(), prompt='Jangan ubah semua varian', mode='perintah', akun_id='shop'))
        result, _ = await service.tool_call(s, user, turn, {'name':'ubah_produk','input':{'katalog_id':'p','perubahan':{'weight':1,'apply_to_all_models':True}}})
        await service.finish(s, turn, 'completed', 'Draf')
        with pytest.raises(HTTPException) as error:
            await service.decide(s, user, result['draft_id'], DecisionIn(decision='confirm'))
        assert error.value.status_code == 422
        draft = await s.get(AiToolReceipt, result['draft_id'])
        draft.created_at = service.now() - timedelta(minutes=11)
        await s.commit()
        with pytest.raises(HTTPException) as error:
            await service.decide(s, user, result['draft_id'], DecisionIn(decision='confirm',all_variants=True))
        assert error.value.status_code == 409
        await service.decide(s, user, result['draft_id'], DecisionIn(decision='cancel'))
        assert draft.status == 'cancelled'
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_large_rejected_receipt_keeps_status_request_id_and_original_result(setup, monkeypatch):
    factory, user = setup
    monkeypatch.setattr(tools.services, 'akun_shopee_pengelolaan', AsyncMock(return_value=SimpleNamespace(id='shop')))
    original = {'ok':False,'request_id':'provider-123','details':'x'*25000}
    monkeypatch.setattr(tools, 'execute', AsyncMock(return_value=original))
    async with factory() as s:
        turn = await service.enqueue(s, user, message(mode='perintah',akun_id='shop'))
        result, stop = await confirmed_call(s, user, turn, {'name':'ubah_iklan','input':{'campaign_id':7,'aksi':'pause'}})
        assert not stop and result['ok'] is False and result['request_id']=='provider-123' and result['terpotong']
        receipt = (await s.execute(select(AiToolReceipt).where(AiToolReceipt.status=='rejected'))).scalar_one()
        assert json.loads(receipt.result_json)==original


@pytest.mark.asyncio
async def test_sol_delegates_coding_without_granting_write_access(setup, monkeypatch):
    factory, user = setup
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only')
    monkeypatch.setattr(config, 'MANAGER_MODEL', 'gpt-6.1-sol')
    monkeypatch.setattr(config, 'INPUT_RATE', Decimal('3'))
    monkeypatch.setattr(config, 'OUTPUT_RATE', Decimal('15'))
    monkeypatch.setattr(config, 'TURN_USD', Decimal('1'))
    monkeypatch.setattr(config, 'DAILY_USD', Decimal('2'))
    model = AsyncMock(side_effect=[
        response({'type': 'tool_use', 'id': 'd1', 'name': 'delegasi_sonnet', 'input': {'bidang': 'coding', 'tugas': 'Analisis masalah stok'}}),
        response({'type': 'tool_use', 'id': 'w1', 'name': 'ubah_iklan', 'input': {'campaign_id': 7, 'aksi': 'pause'}}),
        response({'type': 'text', 'text': 'Susun reproduksi masalah; belum mengubah kode.'}),
        response({'type': 'text', 'text': 'Rencana perbaikan tersedia; tidak ada perubahan dijalankan.'}),
    ])
    monkeypatch.setattr(service, 'model_call', model)
    execute = AsyncMock()
    monkeypatch.setattr(tools, 'execute', execute)
    async with factory() as s:
        turn = await service.enqueue(s, user, message())
        ident = turn.id
        assert turn.model == 'gpt-6.1-sol'
    assert await service.claim(factory) == ident
    await service.process(factory, ident)
    await service.release(factory, ident)
    async with factory() as s:
        turn = await s.get(AiTurn, ident)
        receipt = (await s.execute(select(AiToolReceipt))).scalar_one()
        assert turn.status == 'completed' and receipt.tool == 'delegasi_sonnet'
        assert receipt.status == 'succeeded' and not receipt.is_write
        assert turn.cost_usd == Decimal('0.0035')  # Two Sol + two Sonnet calls, each at its own rates.
        budget = await s.get(AiDailyBudget, config.day())
        assert budget.spent_usd == turn.cost_usd and budget.reserved_usd == 0 and budget.turns == 1
    assert [call.args[0] for call in model.call_args_list] == ['gpt-6.1-sol', config.MODEL, config.MODEL, 'gpt-6.1-sol']
    assert all(not tools.TOOLS[d['name']][1] for d in model.call_args_list[1].args[2])
    assert 'delegasi_sonnet' not in {d['name'] for d in model.call_args_list[1].args[2]}
    assert 'Tool di luar bidang' in model.call_args_list[2].args[1][-1]['content'][0]['content']
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_sol_drafts_write_and_requires_existing_confirmation(setup, monkeypatch):
    factory, user = setup
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only')
    monkeypatch.setattr(config, 'MANAGER_MODEL', 'gpt-6.1-sol')
    monkeypatch.setattr(tools.services, 'akun_shopee_pengelolaan', AsyncMock(return_value=SimpleNamespace(id='shop')))
    monkeypatch.setattr(service, 'model_call', AsyncMock(side_effect=[
        response({'type': 'tool_use', 'id': 'w1', 'name': 'ubah_iklan', 'input': {'campaign_id': 7, 'aksi': 'pause'}}),
        response({'type': 'text', 'text': 'Periksa draf dahulu.'}),
    ]))
    execute = AsyncMock(return_value={'ok': True})
    monkeypatch.setattr(tools, 'execute', execute)
    async with factory() as s:
        turn = await service.enqueue(s, user, message(mode='perintah', akun_id='shop'))
        ident = turn.id
    assert await service.claim(factory) == ident
    await service.process(factory, ident)
    await service.release(factory, ident)
    execute.assert_not_awaited()
    async with factory() as s:
        draft = (await s.execute(select(AiToolReceipt))).scalar_one()
        assert draft.status == 'draft'
        execution = await service.decide(s, user, draft.id, DecisionIn(decision='confirm'))
        execution_id = execution.id
        assert execution.model == 'internal-execution' and execution.reserved_usd == 0
    assert await service.claim(factory) == execution_id
    await service.process(factory, execution_id)
    await service.release(factory, execution_id)
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_delegation_cannot_exceed_parent_reservation(setup, monkeypatch):
    factory, user = setup
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only')
    monkeypatch.setattr(config, 'MANAGER_MODEL', 'gpt-6.1-sol')
    monkeypatch.setattr(config, 'INPUT_RATE', Decimal('100'))
    monkeypatch.setattr(config, 'OUTPUT_RATE', Decimal('100'))
    model = AsyncMock(side_effect=[
        response({'type': 'tool_use', 'id': 'd1', 'name': 'delegasi_sonnet', 'input': {'bidang': 'coding', 'tugas': 'Susun rencana'}}),
        response({'type': 'text', 'text': 'Cadangan spesialis tidak cukup.'}),
    ])
    monkeypatch.setattr(service, 'model_call', model)
    async with factory() as s:
        turn = await service.enqueue(s, user, message())
        ident = turn.id
    assert await service.claim(factory) == ident
    await service.process(factory, ident)
    await service.release(factory, ident)
    assert model.await_count == 2
    assert all(call.args[0] == 'gpt-6.1-sol' for call in model.call_args_list)
    async with factory() as s:
        receipt = (await s.execute(select(AiToolReceipt))).scalar_one()
        assert receipt.status == 'failed' and Decimal(json.loads(receipt.result_json)['cost_usd']) == 0
        turn = await s.get(AiTurn, ident)
        budget = await s.get(AiDailyBudget, config.day())
        assert budget.spent_usd == turn.cost_usd and budget.reserved_usd == 0
