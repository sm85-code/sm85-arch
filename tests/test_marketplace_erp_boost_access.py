"""Manual boost belongs to advanced catalog, never to automation permissions."""
import pytest

from tests.test_marketplace_erp_peta_akses import app_for
from tenants.marketplace_erp.modules.marketplace_erp.application import peta_akses


@pytest.mark.asyncio
async def test_manual_boost_catalog_writer_and_read_only_staff(sesi_peran):
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import RoleHalaman

    # Simulated catalog writer: regression fixture, not the owner's live grants.
    sesi_peran.add(RoleHalaman(role_id='owner', halaman='katalog_lanjutan', tingkat='tulis'))
    sesi_peran.add(RoleHalaman(role_id='staff', halaman='katalog_lanjutan', tingkat='baca'))
    await sesi_peran.commit()
    path = '/akun/shop-1/produk-boost'
    assert peta_akses.cari(path, 'POST')[0] == 'katalog_lanjutan'
    async with app_for('owner', sesi_peran, 'admin') as client:
        assert (await client.get(path)).status_code == 200
        assert (await client.post(path)).status_code == 200
        assert (await client.post('/akun/shop-1/otomasi-produk')).status_code == 403
    async with app_for('staff', sesi_peran, 'admin') as client:
        assert (await client.get(path)).status_code == 200
        assert (await client.post(path)).status_code == 403


@pytest.mark.asyncio
async def test_real_boost_route_denial_includes_server_diagnostic(sesi_peran, monkeypatch, caplog):
    import httpx
    from fastapi import FastAPI
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from tenants.marketplace_erp.adapters.api.v1 import commerce_router as routes
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp

    app = FastAPI()
    app.include_router(routes.router)
    async def db():
        yield sesi_peran
    app.dependency_overrides[get_db_marketplace_erp] = db
    app.dependency_overrides[get_current_user_marketplace_erp] = lambda: SimpleNamespace(id='u', role='staff')
    provider = AsyncMock()
    monkeypatch.setattr(routes.engagement, 'boost', provider)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for method in ('GET', 'POST'):
            kwargs = {'json': {'katalog_ids': ['product-1']}} if method == 'POST' else {}
            response = await client.request(method, '/akun/shop-1/produk-boost', **kwargs)
            assert response.status_code == 403
            report = response.json()['detail']['diagnostic']
            assert report['source'] == 'erp_permission'
            assert report['account_id'] == 'shop-1'
            assert report['request_id'] is None  # never reached Shopee, do not invent an ID
            assert report['diagnostic_id'] in caplog.text
    provider.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['get_boosted_list', 'boost_item'])
async def test_shopee_permission_keeps_original_code_without_raw_secret(monkeypatch, name, caplog):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastapi import HTTPException
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee_engagement as adapter
    error = adapter.provider.ShopeeAPIError(adapter.BASE + name, 'error_api_permission', 'No permission SECRET', 'shopee-trace', response={'ignored': True})
    request = AsyncMock(side_effect=error)
    monkeypatch.setattr(adapter.provider, 'signed_shop_request', request)
    with pytest.raises(HTTPException) as caught:
        await adapter.call(None, SimpleNamespace(id='shop-1', access_token='SECRET'), name)
    assert caught.value.status_code == 403
    report = caught.value.detail['diagnostic']
    assert report['source'] == 'shopee_permission'
    assert report['error'] == 'error_api_permission'
    assert report['request_id'] == 'shopee-trace'
    assert report['account_id'] == 'shop-1'
    assert 'SECRET' not in str(caught.value.detail) + caplog.text
    request.assert_awaited_once()
