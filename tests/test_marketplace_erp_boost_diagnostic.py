"""Exercise real boost routes while every provider operation is mocked."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from tenants.marketplace_erp.adapters.api.v1 import commerce_router as routes
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp


@pytest.fixture
def boost_app(sesi_peran, monkeypatch):
    app = FastAPI()
    app.include_router(routes.router, prefix='/api/marketplace-erp')

    async def db():
        yield sesi_peran

    app.dependency_overrides[get_db_marketplace_erp] = db
    app.dependency_overrides[get_current_user_marketplace_erp] = lambda: SimpleNamespace(id='u', role='admin')
    monkeypatch.setattr(routes, 'account', AsyncMock(return_value=SimpleNamespace(id='shop-1')))
    monkeypatch.setattr(routes.services, 'get_katalog_shopee', AsyncMock(return_value=(SimpleNamespace(akun_id='shop-1', item_id=7), None)))
    # No real read, token refresh or write in HTTP tests.
    monkeypatch.setattr(routes.engagement, 'boost', AsyncMock(return_value={'ok': True}))
    monkeypatch.setattr(routes.engagement, 'boosted', AsyncMock(return_value={'items': []}))
    return app


@pytest.mark.asyncio
async def test_authorized_manual_boost_calls_only_mock_once(boost_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boost_app), base_url='http://test') as client:
        result = await client.post('/api/marketplace-erp/akun/shop-1/produk-boost', json={'katalog_ids': ['p']})
    assert result.status_code == 200
    routes.engagement.boost.assert_awaited_once()
    assert routes.engagement.boost.call_args.args[2] == [7]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['scope', 'mismatch', 'unknown'])
async def test_denials_never_send_boost_and_have_correlation(boost_app, monkeypatch, kind, caplog):
    source = {'scope': 'erp_account_scope', 'mismatch': 'erp_account_mismatch', 'unknown': 'erp_forbidden'}[kind]
    if kind == 'mismatch':
        monkeypatch.setattr(routes.services, 'get_katalog_shopee', AsyncMock(return_value=(SimpleNamespace(akun_id='other', item_id=7), None)))
    else:
        message = 'Akses ditolak untuk akun marketplace ini' if kind == 'scope' else 'PRIVATE SECRET'
        monkeypatch.setattr(routes, 'account', AsyncMock(side_effect=HTTPException(403, message)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boost_app), base_url='http://test') as client:
        result = await client.post('/api/marketplace-erp/akun/shop-1/produk-boost', json={'katalog_ids': ['p']})
    assert result.status_code == 403
    diagnostic = result.json()['detail']['diagnostic']
    assert diagnostic['source'] == source
    assert diagnostic['account_id'] == 'shop-1'
    assert diagnostic['diagnostic_id'] in caplog.text
    assert 'SECRET' not in result.text + caplog.text
    routes.engagement.boost.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_403_error_code_survives_real_transport_decoder(monkeypatch):
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee as provider
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee_engagement as adapter
    monkeypatch.setattr(provider, 'sign_request', lambda *a, **kw: 'MOCK-SIGN')
    monkeypatch.setattr(provider, '_partner_id_int', lambda: 123)
    import requests
    monkeypatch.setattr(requests, 'get', lambda *a, **kw: SimpleNamespace(status_code=403, json=lambda: {'error': 'error_api_permission', 'message': 'Denied access_token=MOCK-TOKEN', 'request_id': 'mock-shopee-trace'}))
    async def mock_signed(session, account, path, **kw):
        return await provider._call_shop_api(access_token='MOCK-TOKEN', shop_id='123', api_path=path, **kw)
    monkeypatch.setattr(provider, 'signed_shop_request', mock_signed)
    with pytest.raises(HTTPException) as caught:
        await adapter.boosted(None, SimpleNamespace(id='shop-1', access_token='MOCK-TOKEN'))
    assert caught.value.status_code == 403
    diagnostic = caught.value.detail['diagnostic']
    assert diagnostic['error'] == 'error_api_permission'
    assert diagnostic['request_id'] == 'mock-shopee-trace'
    assert 'MOCK-TOKEN' not in str(caught.value.detail)
