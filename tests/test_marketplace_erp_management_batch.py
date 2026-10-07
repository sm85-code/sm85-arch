"""Critical contracts for discount mutations and return acceptance, without live shop actions."""
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1 import marketplace_erp_router as router
from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn, PromosiIn, PromosiProdukIn, ShopeeProdukEditIn
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee, erp_shopee_promotions as promo, erp_shopee_returns as returns
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import KatalogShopee

DISCOUNT = {"discount_id": 66512366666549900, "discount_name": "Promo", "status": "ongoing", "start_time": 100, "end_time": 200}
RETURN = {"return_sn": "RET", "order_sn": "ORDER", "status": "REQUESTED", "return_solution": 1, "refund_amount": 0, "currency": "IDR"}


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_read_contracts_keep_large_ids_variants_and_pagination(monkeypatch):
    async def fake(*args, **kwargs):
        if args[2].endswith('get_discount_list'):
            assert kwargs['params'] == {'discount_status': 'all', 'page_no': 2, 'page_size': 40}
            return {'response': {'discount_list': [DISCOUNT], 'more': True}}
        return {'response': {**DISCOUNT, 'more': True, 'item_list': [{'item_id': 10, 'item_name': 'Kaos', 'model_list': [
            {'model_id': 20, 'model_name': 'Merah M', 'model_original_price': 100, 'model_promotion_price': 50}
        ]}]}}
    monkeypatch.setattr(promo, 'signed_shop_request', fake)
    page = await promo.daftar(None, None, halaman=2)
    assert page['items'][0]['id'] == '66512366666549900' and page['ada_lagi']
    detail = await promo.detail(None, None, '66512366666549900')
    assert detail['barang'][0]['model_id'] == '20' and detail['barang'][0]['harga_promo'] == '50'


@pytest.mark.asyncio
async def test_create_checks_schedule_and_confirms_provider_identity(monkeypatch):
    seen = []
    async def fake(*args, **kwargs):
        seen.append(kwargs['body'])
        return {'response': {'discount_id': DISCOUNT['discount_id']}, 'request_id': 'created'}
    monkeypatch.setattr(promo, 'signed_shop_request', fake)
    now = int(time.time())
    with pytest.raises(HTTPException) as exc:
        await promo.buat(None, None, PromosiIn(nama='Promo', mulai_at=now, selesai_at=now+7200))
    assert exc.value.status_code == 422 and not seen
    result = await promo.buat(None, None, PromosiIn(nama='Promo', mulai_at=now+7200, selesai_at=now+10800))
    assert result['id'] == '66512366666549900' and seen[0]['discount_name'] == 'Promo'


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,model,expected', [('tambah', None, 'item_promotion_price'), ('ubah', '20', 'model_list'), ('hapus', '20', 'model_id')])
async def test_item_request_contract_and_per_item_errors(monkeypatch, operation, model, expected):
    seen = []
    async def fake(*args, **kwargs):
        seen.append(kwargs['body'])
        return {'response': {'discount_id': 1, 'count': 0, 'error_list': [{'item_id': 10, 'model_id': 20, 'fail_error': 'discount.error_price', 'fail_message': 'Invalid price'}]}, 'request_id': 'partial'}
    monkeypatch.setattr(promo, 'signed_shop_request', fake)
    p = PromosiProdukIn(operasi=operation, katalog_id='local', harga=Decimal('50'), batas_pembelian=0)
    result = await promo.barang(None, None, '1', '10', model, p)
    body = seen[0] if operation == 'hapus' else seen[0]['item_list'][0]
    assert expected in body
    assert not result['ok'] and result['gagal'][0]['fail_error'] == 'discount.error_price' and result['request_id'] == 'partial'
    if operation == 'ubah':
        assert body['model_list'] == [{'model_id': 20, 'model_promotion_price': 50.0}]
        assert 'model_promotion_stock' not in body['model_list'][0]


@pytest.mark.asyncio
async def test_unconfirmed_write_is_error_with_request_id(monkeypatch):
    async def fake(*args, **kwargs):
        return {'response': {'discount_id': 1, 'count': 0}, 'request_id': 'missing-success'}
    monkeypatch.setattr(promo, 'signed_shop_request', fake)
    with pytest.raises(erp_shopee.ShopeeAPIError, match='missing-success'):
        await promo.barang(None, None, '1', '10', None, PromosiProdukIn(operasi='tambah', katalog_id='local', harga=Decimal('5')))


@pytest.mark.asyncio
async def test_cross_shop_and_missing_variant_rejected_before_provider_write(session, monkeypatch):
    a = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform='shopee', nama_toko='A'))
    b = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform='shopee', nama_toko='B'))
    await services.simpan_katalog_shopee(session, a, [erp_shopee.normalisasi_katalog({'item_id': 10, 'item_name': 'Kaos', 'has_model': True})])
    k = (await session.execute(select(KatalogShopee))).scalar_one()
    async def unexpected(*args, **kwargs):
        pytest.fail('Must not reach provider mutation')
    monkeypatch.setattr(promo, 'signed_shop_request', unexpected)
    payload = PromosiProdukIn(operasi='tambah', katalog_id=k.id, harga=Decimal('5'))
    for shop in [b.id, a.id]:
        with pytest.raises(HTTPException) as exc:
            await services.kelola_barang_promosi(session, shop, '1', payload)
        assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_return_confirmation_and_failed_readback_remain_success(session, monkeypatch):
    a = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform='shopee', nama_toko='A'))
    calls = []
    async def fake(*args, **kwargs):
        calls.append(args[2])
        if args[2].endswith('/confirm'):
            assert kwargs['body'] == {'return_sn': 'RET'}
            return {'response': {'return_sn': 'RET'}, 'request_id': 'accepted'}
        if len(calls) > 2:
            raise erp_shopee.ShopeeAPIError('read', 'error_server', 'failed', 'read-id')
        return {'response': RETURN}
    monkeypatch.setattr(returns, 'signed_shop_request', fake)
    result = await services.konfirmasi_retur_marketplace(session, a.id, 'RET')
    assert result['ok'] and result['request_id'] == 'accepted' and 'read-id' in result['warnings'][0]
    assert len([p for p in calls if p.endswith('/confirm')]) == 1


@pytest.mark.asyncio
async def test_unassigned_staff_cannot_confirm_returns(session, monkeypatch):
    a = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform='shopee', nama_toko='A'))
    with pytest.raises(HTTPException) as exc:
        await router.konfirmasi_retur_marketplace(a.id, 'RET', session, SimpleNamespace(id='staff', role='staff'))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_description_patch_never_sends_price_stock_or_dimensions(monkeypatch):
    seen = []
    async def fake(*args, **kwargs):
        seen.append(kwargs['body'])
        return {'response': {'item_id': 12}}
    monkeypatch.setattr(erp_shopee, 'signed_shop_request', fake)
    payload = ShopeeProdukEditIn(deskripsi='Deskripsi baru')
    await erp_shopee.ubah_info_produk(None, None, 12, {'description': payload.deskripsi})
    assert seen == [{'item_id': 12, 'description': 'Deskripsi baru'}]
