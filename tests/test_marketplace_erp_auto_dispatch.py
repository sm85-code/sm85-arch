from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import AkunMarketplace, Pesanan, AutoOrderPolicy, AutoOrderAttempt, UserMarketplaceErp
from tenants.marketplace_erp.modules.marketplace_erp.application import auto_orders
from tenants.marketplace_erp.modules.marketplace_erp.application.assistant import tools


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as c:
        await c.run_sync(MarketplaceErpBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(UserMarketplaceErp(id='admin', username='admin', nama='Admin', role='admin', password_hash='test'))
        s.add(AkunMarketplace(id='shop', platform='shopee', nama_toko='Shop', status='terhubung'))
        s.add(AutoOrderPolicy(akun_id='shop', enabled=True, enabled_by='admin', enabled_at=auto_orders.now()-timedelta(minutes=1)))
        for ident, minutes in [('old', 5), ('new', 0)]:
            s.add(Pesanan(id=ident, akun_id='shop', platform='shopee', id_eksternal=ident, status='to_ship', status_marketplace='READY_TO_SHIP', dipesan_at=auto_orders.now()-timedelta(minutes=minutes)))
        await s.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_claim_excludes_backlog_and_never_replays(factory):
    async with factory() as s:
        assert await auto_orders.claim(s) == 'new'
        assert await auto_orders.claim(s) is None
        attempt = await s.get(AutoOrderAttempt, 'new')
        attempt.created_at = auto_orders.now()-timedelta(minutes=11)
        await s.commit()
        assert await auto_orders.claim(s) is None
        assert (await s.get(AutoOrderAttempt, 'new')).status == 'unknown'


@pytest.mark.asyncio
@pytest.mark.parametrize('available, fails, expected', [(False, False, 'blocked'), (True, True, 'unknown'), (True, False, 'succeeded')])
async def test_dropoff_only_and_uncertain_not_replayed(factory, monkeypatch, available, fails, expected):
    monkeypatch.setattr(auto_orders.services, 'get_settings', AsyncMock(return_value={'gudang_aktif': False}))
    monkeypatch.setattr(auto_orders.erp_shopee, 'opsi_pengiriman', AsyncMock(return_value={'opsi': [{'metode': 'dropoff', 'tersedia': available, 'wajib': []}]}))
    send = AsyncMock(side_effect=TimeoutError() if fails else None)
    monkeypatch.setattr(auto_orders.services, 'proses_pesanan_marketplace', send)
    async with factory() as s:
        assert await auto_orders.claim(s) == 'new'
    await auto_orders.run_one(factory, 'new')
    await auto_orders.run_one(factory, 'new')
    async with factory() as s:
        assert (await s.get(AutoOrderAttempt, 'new')).status == expected
        assert await auto_orders.claim(s) is None
    assert send.call_count == int(available)
    if available:
        assert send.call_args.args[2] == {'metode': 'dropoff'}


@pytest.mark.asyncio
async def test_extra_tools_reject_cross_shop_before_provider(monkeypatch):
    monkeypatch.setattr(tools.services, 'akun_shopee_pengelolaan', AsyncMock(return_value=SimpleNamespace(id='one')))
    monkeypatch.setattr(tools.services, 'get_katalog_shopee', AsyncMock(return_value=(SimpleNamespace(akun_id='two'), 'Other')))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await tools.execute(None, SimpleNamespace(role='admin', session_version=0), SimpleNamespace(session_version=0, mode='perintah', akun_id='one'), 'hapus_produk_shopee', tools.validate('hapus_produk_shopee', {'katalog_id': 'other'}, 'ref'), 'hash')
    assert exc.value.status_code == 403


def test_new_commands_remain_read_only_in_question_mode():
    available = {d['name'] for d in tools.definitions(False)}
    assert 'pelacakan_pesanan' in available
    assert 'publikasikan_produk' not in available
    assert 'hapus_produk_shopee' not in available
    publication = next(d for d in tools.definitions(True) if d['name'] == 'publikasikan_produk')
    assert 'operation_id' not in publication['input_schema']['properties']


@pytest.mark.asyncio
async def test_manual_dispatch_blocked_while_automatic_result_unknown(factory):
    from fastapi import HTTPException
    async with factory() as s:
        await auto_orders.claim(s)
        row = await s.get(AutoOrderAttempt, 'new')
        row.status = 'unknown'
        await s.commit()
        with pytest.raises(HTTPException) as exc:
            await auto_orders.services.proses_pesanan_marketplace(s, 'new', {'metode': 'dropoff'})
        assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_unmapped_warehouse_order_is_not_sent(factory, monkeypatch):
    monkeypatch.setattr(auto_orders.services, 'get_settings', AsyncMock(return_value={'gudang_aktif': True}))
    send = AsyncMock()
    monkeypatch.setattr(auto_orders.services, 'proses_pesanan_marketplace', send)
    async with factory() as s:
        await auto_orders.claim(s)
    await auto_orders.run_one(factory, 'new')
    async with factory() as s:
        assert (await s.get(AutoOrderAttempt, 'new')).status == 'blocked'
    send.assert_not_called()
