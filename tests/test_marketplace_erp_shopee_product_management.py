"""Documented item mutations and safe partial catalogue refresh."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn, ShopeeProdukEditIn
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import KatalogShopee


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


def _item(item_id, name, price):
    return {"item_id": item_id, "item_name": name, "price_info": [{"current_price": price}]}


@pytest.mark.asyncio
async def test_edit_only_sends_changed_name_and_explicit_empty_sku(monkeypatch):
    seen = []
    async def request(session, account, path, **kwargs):
        seen.append((path, kwargs))
        return {'response': {'item_id': 12}, 'request_id': 'req'}
    monkeypatch.setattr(erp_shopee, 'signed_shop_request', request)
    await erp_shopee.ubah_info_produk(None, SimpleNamespace(), 12, {'item_sku': ''})
    assert seen == [('/api/v2/product/update_item', {'method': 'POST', 'body': {'item_id': 12, 'item_sku': ''}})]
    with pytest.raises(HTTPException):
        await erp_shopee.ubah_info_produk(None, SimpleNamespace(), 12, {'weight': 2})
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['rejected', 'missing', 'wrong_status', 'success'])
async def test_unlist_requires_exact_per_item_confirmation(monkeypatch, mode):
    async def request(session, account, path, **kwargs):
        assert kwargs['body'] == {'item_list': [{'item_id': 12, 'unlist': True}]}
        return {'request_id': 'req-123', 'response': {
            'failure_list': [{'item_id': 12, 'failed_reason': 'Under promotion'}] if mode == 'rejected' else [],
            'success_list': [{'item_id': 12, 'unlist': mode != 'wrong_status'}] if mode in {'success', 'wrong_status'} else [],
        }}
    monkeypatch.setattr(erp_shopee, 'signed_shop_request', request)
    if mode == 'success':
        assert (await erp_shopee.ubah_status_produk(None, None, 12, True))['request_id'] == 'req-123'
    else:
        with pytest.raises(erp_shopee.ShopeeAPIError) as exc:
            await erp_shopee.ubah_status_produk(None, None, 12, True)
        assert 'req-123' in exc.value.detail
        if mode == 'rejected':
            assert 'Under promotion' in exc.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize('refresh_fails', [False, True])
async def test_confirmed_mutation_refresh_never_deletes_other_products(session, monkeypatch, refresh_fails):
    account = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform='shopee', nama_toko='A'))
    rows = [erp_shopee.normalisasi_katalog(_item(i, f'P{i}', 100)) for i in [12, 13]]
    await services.simpan_katalog_shopee(session, account, rows)
    catalog = (await session.execute(select(KatalogShopee).where(KatalogShopee.item_id == '12'))).scalar_one()
    async def mutate(*args):
        return {'request_id': 'req-123'}
    async def refresh(*args):
        if refresh_fails:
            raise HTTPException(status_code=502, detail='Read temporarily unavailable')
        return erp_shopee.normalisasi_katalog(_item(12, 'Updated', 100))
    monkeypatch.setattr(erp_shopee, 'ubah_info_produk', mutate)
    monkeypatch.setattr(erp_shopee, 'ambil_satu_produk', refresh)
    result = await services.kelola_produk_shopee(session, catalog.id, fields={'item_name': 'Updated'})
    assert result['ok'] is True and result['snapshot_diperbarui'] is not refresh_fails
    assert len((await session.execute(select(KatalogShopee))).scalars().all()) == 2
    assert catalog.nama == ('P12' if refresh_fails else 'Updated')
    assert bool(result['warnings']) == refresh_fails


@pytest.mark.parametrize('fields', [{}, {'nama': ' '}, {'stok': 7}, {'nama': None}])
def test_edit_rejects_empty_or_unsupported_changes(fields):
    with pytest.raises(ValidationError):
        ShopeeProdukEditIn.model_validate(fields)


@pytest.mark.asyncio
async def test_confirmed_status_with_stale_snapshot_is_reported_as_warning(session, monkeypatch):
    account = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform='shopee', nama_toko='A'))
    snapshot = erp_shopee.normalisasi_katalog({**_item(12, 'Product', 100), 'item_status': 'NORMAL'})
    await services.simpan_katalog_shopee(session, account, [snapshot])
    row = (await session.execute(select(KatalogShopee))).scalar_one()
    async def mutate(*args):
        return {'request_id': 'confirmed'}
    async def read(*args):
        return snapshot
    monkeypatch.setattr(erp_shopee, 'ubah_status_produk', mutate)
    monkeypatch.setattr(erp_shopee, 'ambil_satu_produk', read)
    result = await services.kelola_produk_shopee(session, row.id, unlist=True)
    assert result['ok'] is True and result['snapshot_diperbarui'] is False
    assert result['warnings'] and row.status == 'NORMAL'


@pytest.mark.asyncio
async def test_provider_permission_error_never_updates_local_catalogue_or_shop_status(session, monkeypatch):
    account = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform='shopee', nama_toko='A'))
    await services.simpan_katalog_shopee(session, account, [erp_shopee.normalisasi_katalog(_item(12, 'Before', 100))])
    row = (await session.execute(select(KatalogShopee))).scalar_one()
    original_status = account.status
    async def reject(*args):
        raise erp_shopee.ShopeeAPIError('/api/v2/product/update_item', 'error_auth', 'Not authorized', 'request-denied')
    async def should_not_read(*args):
        raise AssertionError('No refresh after rejected mutation')
    monkeypatch.setattr(erp_shopee, 'ubah_info_produk', reject)
    monkeypatch.setattr(erp_shopee, 'ambil_satu_produk', should_not_read)
    with pytest.raises(erp_shopee.ShopeeAPIError) as exc:
        await services.kelola_produk_shopee(session, row.id, fields={'item_name': 'After'})
    assert 'request-denied' in exc.value.detail
    assert row.nama == 'Before' and account.status == original_status
