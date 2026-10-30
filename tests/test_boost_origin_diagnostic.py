"""Origin middleware must keep denying writes while making boost rejections traceable."""
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

import main


@pytest.mark.asyncio
async def test_boost_origin_rejection_has_safe_server_diagnostic(monkeypatch, caplog):
    monkeypatch.setattr(main, 'CORS_ORIGINS', ['https://erp.example.test'])
    monkeypatch.setattr(main, 'origin_allowed', lambda origin: False)
    app = FastAPI()
    write = AsyncMock()

    @app.post('/api/marketplace-erp/akun/{id}/produk-boost')
    async def boost(id: str):
        await write()
        return {}

    app.add_middleware(main.CsrfOriginMiddleware)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/api/marketplace-erp/akun/shop-1/produk-boost', headers={'origin': 'https://PRIVATE.invalid', 'cookie': 'token=SECRET'}, json={'katalog_ids': ['mock']})
        unrelated = await client.post('/unrelated', headers={'origin': 'https://PRIVATE.invalid'})
    assert response.status_code == 403
    detail = response.json()['detail']
    assert isinstance(detail, dict)
    diagnostic = detail['diagnostic']
    assert diagnostic['source'] == 'erp_origin'
    assert diagnostic['account_id'] == 'shop-1'
    assert diagnostic['request_id'] is None
    assert diagnostic['diagnostic_id'] in caplog.text
    assert 'SECRET' not in response.text + caplog.text
    assert 'PRIVATE' not in response.text + caplog.text
    assert unrelated.json() == {'detail': 'Permintaan lintas situs ditolak'}
    write.assert_not_awaited()
