import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from tenants.marketplace_erp.modules.marketplace_erp.application import order_bridge as b
from tenants.marketplace_erp.modules.marketplace_erp.application.bridge_presentation import enrich_orders
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import PesananOut
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters.easyboss import Order
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import BridgeOrder, Pesanan, TokoPenjualan


@pytest.mark.asyncio
async def test_existing_bridge_display_uses_source_identity_without_order_mutation():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    try:
        async with engine.begin() as conn:
            await conn.run_sync(MarketplaceErpBase.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            s.add(TokoPenjualan(jenis='internal', platform='tiktokshop', nama='Source Shop', nama_key='source shop',
                               sumber='easyboss', easyboss_shop_id='shop', sinkron_status='synced', aktif=True, keterangan=''))
            await s.commit()
            row = Order(platform='tiktokshop', shop_id='shop', order_id='original-order', complete=True,
                        paid=True, status='ready', source_status='AWAITING_COLLECTION', buyer='s***di',
                        ordered_at='2026-11-01T09:00:00+08:00', deadline='2026-11-09T23:59:00+08:00',
                        carrier='Carrier', tracking_number='TRACK', payment_method='PayLater', city='Kota Tangerang',
                        order_total='1500000', buyer_paid='1522000', currency='IDR', recipient_name='s***di',
                        recipient_phone='(+62)813******31', recipient_address='Indonesia, Banten, jl***',
                        items=[{'item_id':'line', 'product':'Partition', 'variant':'150x20x200', 'quantity':1, 'unit_price':'1490000', 'line_total':'1490000',
                                'image_url':'https://images.example/product.jpg'}])
            assert await b.ingest(s, row) == 'imported'
            await s.commit()
            p = await s.scalar(select(Pesanan).options(selectinload(Pesanan.items)))
            receipt = await s.scalar(select(BridgeOrder))
            # Older imports retain original metadata. New display values live in the bridge read model.
            detail = json.loads(p.detail_json)
            detail['easyboss'].pop('payment_method')
            detail['easyboss'].pop('city')
            for key in ['order_total','buyer_paid','currency','recipient_name','recipient_phone','recipient_address']:
                detail['easyboss'].pop(key)
            for key in ['unit_price','line_total','image_url']:
                detail['easyboss']['items'][0].pop(key)
            p.detail_json = json.dumps(detail)
            await s.commit()
            before = (p.id_eksternal, p.platform, p.status, p.detail_json)
            await enrich_orders(s, [p])
            out = PesananOut.model_validate(p)
            assert out.nomor_pesanan_asli == 'original-order'
            assert out.platform_sumber == 'tiktokshop' and out.nama_toko == 'Source Shop'
            assert out.payment_method == 'PayLater' and out.kota == 'Kota Tangerang'
            assert out.kurir == 'Carrier' and out.nomor_resi == 'TRACK'
            assert out.total == 1500000 and out.dibayar_pembeli == 1522000 and out.currency == 'IDR'
            assert out.items[0].harga_satuan == 1490000 and out.items[0].subtotal == 1490000
            assert out.items[0].foto == 'https://images.example/product.jpg'
            assert out.penerima == 's***di' and out.telepon_penerima == '(+62)813******31'
            assert out.alamat_penerima == 'Indonesia, Banten, jl***'
            assert out.label_status_sumber == 'Menunggu Penyerahan ke Kurir'
            assert p.total == 0 and p.items[0].harga_satuan == 0
            from tenants.marketplace_erp.modules.marketplace_erp.application import services
            assert await s.scalar(select(Pesanan.id).where(services._kondisi_tahap('menunggu_kurir'))) == p.id
            assert await s.scalar(select(Pesanan.id).where(services._kondisi_tahap('perlu_diproses'))) is None
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as denied:
                await services.ubah_status_pesanan(s, p.id, 'shipped')
            assert denied.value.status_code == 409 and p.status == 'to_ship'
            assert out.id_eksternal.startswith('EB-') and out.platform == 'manual'
            assert before == (p.id_eksternal, p.platform, p.status, p.detail_json)
            assert not s.dirty
            # Display-only enrichment does not create a false operational-change review.
            assert await b.ingest(s, row) == 'skipped'
            assert receipt.status == 'imported'
            # An unrelated snapshot cannot override this order's display metadata.
            p._bridge_snapshot = {**row.model_dump(mode='json'), 'order_id': 'wrong', 'payment_method': 'wrong'}
            assert PesananOut.model_validate(p).payment_method == ''
            assert PesananOut.model_validate(p).total_sumber_tersedia is False
            assert PesananOut.model_validate(p).items[0].harga_sumber_tersedia is False
    finally:
        await engine.dispose()


def test_source_line_enrichment_rejects_changed_quantities():
    from tenants.marketplace_erp.modules.marketplace_erp.application.bridge_presentation import merge_display_snapshot
    old = {'platform':'tiktokshop','shop_id':'s','order_id':'o',
           'items':[{'item_id':'l','product':'Chair','variant':'Blue','quantity':1}]}
    latest = {**old,'items':[{**old['items'][0],'quantity':2,'unit_price':'100','line_total':'200'}]}
    assert 'unit_price' not in merge_display_snapshot(old, latest)['items'][0]


def test_native_amount_preserves_zero_and_rejects_invalid_values():
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters.easyboss_mcp import native_amount
    assert native_amount(0) == 0 and native_amount('--') is None
    for value in ['NaN', '-1', 'not-a-price']:
        with pytest.raises(ValueError):
            native_amount(value)
