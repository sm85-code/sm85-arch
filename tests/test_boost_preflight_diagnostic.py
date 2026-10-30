"""Real POST route and ownership checks; only external provider is mocked."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from tenants.marketplace_erp.adapters.api.v1 import commerce_router as routes
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp

PREFLIGHT = '/api/v2/product/get_item_base_info'
BOOST = '/api/v2/product/boost_item'


@pytest.fixture
def preflight_app(sesi_peran, monkeypatch):
    app = FastAPI()
    app.include_router(routes.router, prefix='/api/marketplace-erp')

    async def db():
        yield sesi_peran

    app.dependency_overrides[get_db_marketplace_erp] = db
    app.dependency_overrides[get_current_user_marketplace_erp] = lambda: SimpleNamespace(id='u', role='admin')
    monkeypatch.setattr(routes, 'account', AsyncMock(return_value=SimpleNamespace(id='shop-1', access_token='MOCK-SECRET')))
    monkeypatch.setattr(routes.services, 'get_katalog_shopee', AsyncMock(return_value=(SimpleNamespace(akun_id='shop-1', item_id=7), None)))
    provider = AsyncMock()
    monkeypatch.setattr(routes.engagement.provider, 'signed_shop_request', provider)
    return app, provider


async def post(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        return await client.post('/api/marketplace-erp/akun/shop-1/produk-boost', json={'katalog_ids': ['p']})


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['error_api_permission', 'common.error_api_permission'])
@pytest.mark.parametrize('request_id', ['mock-preflight-trace', None])
async def test_preflight_permission_is_correlated_and_never_writes(preflight_app, caplog, code, request_id):
    app, provider = preflight_app
    provider.side_effect = routes.engagement.provider.ShopeeAPIError(PREFLIGHT, code, 'Denied access_token=MOCK-SECRET', request_id, response={'private': 'MOCK-SECRET'})
    result = await post(app)
    assert result.status_code == 403
    report = result.json()['detail']['diagnostic']
    assert report['source'] == 'shopee_permission'
    assert report['endpoint'] == PREFLIGHT
    assert report['error'] == code
    assert report['request_id'] == request_id
    assert report['account_id'] == 'shop-1'
    UUID(report['diagnostic_id'])
    assert report['diagnostic_id'] in caplog.text
    assert 'MOCK-SECRET' not in result.text + caplog.text
    provider.assert_awaited_once()
    assert provider.await_args.args[2] == PREFLIGHT
    assert provider.await_args.kwargs.get('method', 'GET') == 'GET'


@pytest.mark.asyncio
@pytest.mark.parametrize('error,status,code', [
    (TimeoutError('access_token=MOCK-SECRET'), 502, 'transport_or_decode_error'),
    (HTTPException(504, 'access_token=MOCK-SECRET'), 504, 'http_504'),
    (routes.engagement.provider.ShopeeNotConfigured('access_token=MOCK-SECRET'), 503, 'not_configured'),
    (routes.engagement.provider.ShopeeAPIError(PREFLIGHT, 'incomplete_response', 'Invalid MOCK-SECRET', None), 502, 'incomplete_response'),
])
async def test_preflight_failure_is_safe_and_never_writes(preflight_app, caplog, error, status, code):
    app, provider = preflight_app
    provider.side_effect = error
    result = await post(app)
    assert result.status_code == status
    report = result.json()['detail']['diagnostic']
    assert report['endpoint'] == PREFLIGHT
    assert report['source'] == 'shopee'
    assert report['error'] == code
    assert report['request_id'] is None
    assert report['account_id'] == 'shop-1'
    UUID(report['diagnostic_id'])
    assert report['diagnostic_id'] in caplog.text
    assert 'MOCK-SECRET' not in result.text + caplog.text
    assert 'belum dikirim' in result.json()['detail']['message']
    provider.assert_awaited_once()
    assert provider.await_args.args[2] == PREFLIGHT


@pytest.mark.asyncio
async def test_success_validates_provider_item_before_single_mock_write(preflight_app):
    app, provider = preflight_app
    provider.side_effect = [
        {'response': {'item_list': [{'item_id': 7}]}},
        {'error': '', 'request_id': 'mock-boost-trace', 'response': {'success_list': {'item_id_list': [7]}, 'failure_list': []}},
    ]
    result = await post(app)
    assert result.status_code == 200
    assert result.json()['ok'] is True
    assert [c.args[2] for c in provider.await_args_list] == [PREFLIGHT, BOOST]
    assert provider.await_args_list[0].kwargs.get('method', 'GET') == 'GET'
    assert provider.await_args_list[1].kwargs['method'] == 'POST'


@pytest.mark.asyncio
async def test_missing_provider_item_still_blocks_mock_write(preflight_app):
    app, provider = preflight_app
    provider.return_value = {'response': {'item_list': [{'item_id': 999}]}}
    result = await post(app)
    assert result.status_code == 404
    provider.assert_awaited_once()
    assert provider.await_args.args[2] == PREFLIGHT


@pytest.mark.asyncio
@pytest.mark.parametrize('denial', ['permission', 'ownership'])
async def test_erp_denial_never_reaches_provider(preflight_app, monkeypatch, denial, caplog):
    app, provider = preflight_app
    if denial == 'permission':
        app.dependency_overrides[get_current_user_marketplace_erp] = lambda: SimpleNamespace(id='u', role='staff')
    else:
        monkeypatch.setattr(routes.services, 'get_katalog_shopee', AsyncMock(return_value=(SimpleNamespace(akun_id='other', item_id=7), None)))
    result = await post(app)
    assert result.status_code == 403
    report = result.json()['detail']['diagnostic']
    assert report['source'] == ('erp_permission' if denial == 'permission' else 'erp_account_mismatch')
    assert report['account_id'] == 'shop-1'
    assert report['request_id'] is None
    assert report['diagnostic_id'] in caplog.text
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_later_item_failure_blocks_before_send_and_all_writes(monkeypatch):
    provider = AsyncMock(side_effect=[
        {'response': {'item_list': [{'item_id': 7}]}},
        routes.engagement.provider.ShopeeAPIError(PREFLIGHT, 'error_api_permission', 'Denied', None),
    ])
    monkeypatch.setattr(routes.engagement.provider, 'signed_shop_request', provider)
    before_send = AsyncMock()
    with pytest.raises(HTTPException) as caught:
        await routes.engagement.boost(None, SimpleNamespace(id='shop-1'), ['7', '8'], before_send=before_send)
    assert caught.value.status_code == 403
    assert provider.await_count == 2
    assert all(c.args[2] == PREFLIGHT and c.kwargs.get('method', 'GET') == 'GET' for c in provider.await_args_list)
    before_send.assert_not_awaited()
