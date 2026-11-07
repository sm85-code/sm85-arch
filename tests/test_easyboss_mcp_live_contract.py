import json

import httpx
import pytest

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import easyboss as eb
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters.easyboss_mcp import MCPGateway, normalize


def package():
    return {'shopId': 'shop', 'platformOrderSn': 'order', 'opOrderPackageId': 'pkg',
        'isSplit': 0, 'itemQuantity': '2', 'orderInfo': {'platform': 'tiktok', 'shopId': 'shop',
        'platformOrderSn': 'order', 'opOrderId': 'eb-order', 'platformOrderStatus': 'AWAITING_COLLECTION',
        'gmtPay': '2026-11-07 10:00:00', 'isCod': '0', 'buyerUsername': 'Customer',
        'gmtOrderStart': '2026-11-07 09:59:00', 'gmtLastDelivery': '2026-11-09 12:00:00', 'itemQuantity': '2'},
        'items': {'line': {'opOrderItemId': 'line', 'opOrderId': 'eb-order', 'shopId': 'shop',
                          'quantity': '2', 'title': 'Chair', 'skuSubName': 'Blue'}}}


def test_native_mapping():
    row = normalize(package(), ('tiktokshop', 'shop', 'order'))
    assert row.paid is True and row.complete and row.status == 'ready'
    assert row.items[0].quantity == 2 and row.items[0].variant == 'Blue'
    assert row.ordered_at.hour == 1 and row.deadline.hour == 4


@pytest.mark.parametrize('change,field,expected', [
    ({'isSplit': 1}, 'complete', False), ({'itemQuantity': '3'}, 'complete', False),
    ({'isOnHoldOrder': True}, 'status', 'hold')])
def test_incomplete_or_hold(change, field, expected):
    p = package()
    p.update(change)
    assert getattr(normalize(p, ('tiktokshop', 'shop', 'order')), field) == expected


def test_masked_buyer_cod_and_unknown_status():
    p = package()
    p['orderInfo'].update(buyerUsername='s***di', isCod='1')
    row = normalize(p, ('tiktokshop', 'shop', 'order'))
    assert row.buyer == 's***di' and row.paid is None and row.status == 'ready' and row.cod_eligible is False
    p['orderInfo'].update(isCod='0', platformOrderStatus='CANCELLED')
    assert normalize(p, ('tiktokshop', 'shop', 'order')).status == 'cancelled'


@pytest.mark.parametrize('field,value', [('shopId', 'wrong'), ('quantity', '1.5'), ('opOrderId', 'other')])
def test_invalid_items_rejected(field, value):
    p = package()
    p['items']['line'][field] = value
    assert isinstance(normalize(p, ('tiktokshop', 'shop', 'order')), eb.RejectedOrder)


class Fake(MCPGateway):
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.arguments = []

    async def call(self, name, arguments):
        self.calls.append(name)
        self.arguments.append(arguments)
        return next(self.responses)


@pytest.mark.asyncio
async def test_split_detail_never_used():
    g = Fake([{'total': 2, 'pesanan': [{}, {}]}])
    assert isinstance(await g.one('tiktok', 'shop', 'order'), eb.RejectedOrder)
    assert g.calls == ['eb_pesanan_list']


@pytest.mark.asyncio
async def test_package_change_aborts():
    g = Fake([{'total': 1, 'pesanan': [{'shopId': 'shop', 'no_pesanan': 'order', 'id': 'old'}]}, package()])
    with pytest.raises(eb.BridgeProblem, match='package_changed'):
        await g.one('tiktok', 'shop', 'order')


@pytest.mark.asyncio
async def test_unstable_pagination_aborts_before_details():
    row = {'id': 'p', 'shopId': 'shop', 'no_pesanan': 'order', 'platform': 'tiktok'}
    g = Fake([{'total': 2, 'pesanan': [row]}, {'total': 3, 'pesanan': [row]}])
    with pytest.raises(eb.BridgeProblem, match='unstable_pagination'):
        await g.orders(None)
    assert 'eb_pesanan_detail' not in g.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('sse', [False, True])
async def test_transport_and_allowlist(sse):
    def handler(request):
        body = json.loads(request.content)
        assert body['method'] == 'tools/call'
        payload = {'jsonrpc': '2.0', 'id': body['id'], 'result': {'content': [{'type': 'text', 'text': json.dumps({'per_toko': []}) if body['params']['name'] == 'eb_pesanan_hitung' else '[]'}]}}
        return (httpx.Response(200, text='data: ' + json.dumps(payload) + '\n\n',
                               headers={'content-type': 'text/event-stream'}) if sse else httpx.Response(200, json=payload))
    async with httpx.AsyncClient(base_url='https://mcp.example/mcp', transport=httpx.MockTransport(handler)) as c:
        g = MCPGateway(c)
        assert await g.shops() == []
        with pytest.raises(eb.BridgeProblem, match='not_allowed'):
            await g.call('eb_kirim_barang', {})


def test_factory_and_url_policy(monkeypatch):
    monkeypatch.setenv('EASYBOSS_BRIDGE_MCP_URL', 'http://external.example/mcp')
    monkeypatch.setenv('EASYBOSS_BRIDGE_TOKEN', 'synthetic')
    monkeypatch.setenv('EASYBOSS_BRIDGE_CONTRACT_VERIFIED', '1')
    assert eb.configured()
    with pytest.raises(eb.BridgeProblem, match='invalid_mcp_url'):
        eb.client()
    monkeypatch.delenv('EASYBOSS_BRIDGE_TOKEN')
    assert not eb.configured()


@pytest.mark.asyncio
async def test_normalized_orders_reach_manual_workflow_and_missing_buyer_is_blocked():
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from tenants.marketplace_erp.modules.marketplace_erp.application import order_bridge as bridge
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import BridgeOrder, ItemPesanan, Pesanan, TokoPenjualan

    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    try:
        async with engine.begin() as conn:
            await conn.run_sync(MarketplaceErpBase.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add(TokoPenjualan(jenis='internal', platform='tiktokshop', nama='Synthetic',
                nama_key='synthetic', keterangan='', aktif=True, sumber='easyboss', easyboss_shop_id='shop',
                sinkron_status='synced'))
            await session.commit()
            p = package()
            p['orderInfo']['buyerUsername'] = ''
            assert await bridge.ingest(session, normalize(p, ('tiktokshop', 'shop', 'order'))) == 'failed'
            await session.commit()
            receipt = await session.scalar(select(BridgeOrder))
            assert receipt.error == 'missing_buyer'
            assert await session.scalar(select(Pesanan)) is None
            p['orderInfo']['buyerUsername'] = 's***di'
            row = normalize(p, ('tiktokshop', 'shop', 'order'))
            assert await bridge.ingest(session, row) == 'imported'
            await session.commit()
            assert await bridge.ingest(session, row) == 'skipped'
            await session.commit()
            orders = list((await session.scalars(select(Pesanan))).all())
            items = list((await session.scalars(select(ItemPesanan))).all())
            assert len(orders) == 1 and len(items) == 1
            assert orders[0].nama_pembeli == 's***di'
            assert json.loads(orders[0].detail_json)['easyboss']['items'][0]['quantity'] == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_active_scan_and_tracked_orders_include_moved_orders():
    g = Fake([{'total': 0, 'pesanan': []}] * 6 + [
        {'total': 1, 'pesanan': [{'id': 'pkg', 'shopId': 'shop', 'no_pesanan': 'order'}]}, package()])
    g.tracked_orders = [('tiktokshop', 'shop', 'order')]
    rows = await g.orders(None)
    assert len(rows) == 1 and rows[0].order_id == 'order'
    assert rows[0].status == 'ready' and rows[0].paid is True
    assert g.calls == ['eb_pesanan_list'] * 7 + ['eb_pesanan_detail']


@pytest.mark.asyncio
async def test_empty_active_queues_do_not_scan_historical_orders():
    g = Fake([{'total': 0, 'pesanan': []}] * 6)
    assert await g.orders(None) == []
    assert g.calls == ['eb_pesanan_list'] * 6


def lazada_cod_package():
    p = package()
    p['orderInfo'].update(platform='lazada', platformOrderStatus='ready_to_ship', isCod='1',
                          paymentMethod='COD', gmtPay=None)
    p['appPackageTab'] = 'submitPlatform'
    p['appPackageOperateStatus'] = 'ship_success'
    p['isShipped'] = '1'
    return p


def test_lazada_cod_ready_is_eligible_without_claiming_payment_or_dispatch():
    row = normalize(lazada_cod_package(), ('lazada', 'shop', 'order'))
    assert row.status == 'ready' and row.complete
    assert row.cod is True and row.cod_eligible is True and row.paid is None
    p = lazada_cod_package()
    p['isPendingStatus'] = True
    assert normalize(p, ('lazada','shop','order')).status == 'hold'


@pytest.mark.asyncio
async def test_lazada_submit_platform_queue_is_scanned():
    listing = {'total':1, 'pesanan':[{'id':'pkg','shopId':'shop','no_pesanan':'order','platform':'lazada'}]}
    g = Fake([{'total':0,'pesanan':[]}] * 5 + [listing, listing, lazada_cod_package()])
    rows = await g.orders(None)
    assert len(rows) == 1 and rows[0].cod_eligible and rows[0].paid is None
    assert any(a.get('tab') == 'submitPlatform' and a.get('platform') == 'lazada' for a in g.arguments)


@pytest.mark.asyncio
async def test_email_shop_directory_name_uses_alias_for_same_stable_id():
    g = Fake([[{'platform':'lazada','shopId':'shop','platformShopId':'market','platformShopName':'account@example.com',
                'authStatus':'valid','status':'normal','platformStatus':'normal'}],
              {'per_toko':[{'platform':'lazada','shopId':'shop','shopName':'Bumi Ternak'}]}])
    rows = await g.shops()
    assert rows[0].name == 'Bumi Ternak' and rows[0].shop_id == 'shop'


@pytest.mark.parametrize('native,canonical,phase', [('IN_TRANSIT','shipped','dikirim'), ('DELIVERED','completed','selesai'), ('COMPLETED','completed','selesai')])
def test_native_lifecycle_without_delivery_timestamp(native, canonical, phase):
    from tenants.marketplace_erp.modules.marketplace_erp.application.bridge_status import native_state, projected_status
    p = package()
    p['orderInfo'].update(platformOrderStatus=native, gmtDelivery=None, gmtFinish=None)
    row = normalize(p, ('tiktokshop', 'shop', 'order'))
    assert row.status == canonical
    assert native_state('tiktokshop', native)[1] == phase
    assert projected_status({'platform':'tiktokshop', 'source_status':native}, 'to_ship') == canonical
