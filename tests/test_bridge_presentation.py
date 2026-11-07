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
                        items=[{'item_id':'line', 'product':'Partition', 'variant':'150x20x200', 'quantity':1}])
            assert await b.ingest(s, row) == 'imported'
            await s.commit()
            p = await s.scalar(select(Pesanan).options(selectinload(Pesanan.items)))
            receipt = await s.scalar(select(BridgeOrder))
            # Older imports retain original metadata. New display values live in the bridge read model.
            detail = json.loads(p.detail_json)
            detail['easyboss'].pop('payment_method')
            detail['easyboss'].pop('city')
            p.detail_json = json.dumps(detail)
            await s.commit()
            before = (p.id_eksternal, p.platform, p.status, p.detail_json)
            await enrich_orders(s, [p])
            out = PesananOut.model_validate(p)
            assert out.nomor_pesanan_asli == 'original-order'
            assert out.platform_sumber == 'tiktokshop' and out.nama_toko == 'Source Shop'
            assert out.payment_method == 'PayLater' and out.kota == 'Kota Tangerang'
            assert out.kurir == 'Carrier' and out.nomor_resi == 'TRACK'
            assert out.id_eksternal.startswith('EB-') and out.platform == 'manual'
            assert before == (p.id_eksternal, p.platform, p.status, p.detail_json)
            assert not s.dirty
            # Display-only enrichment does not create a false operational-change review.
            assert await b.ingest(s, row) == 'skipped'
            assert receipt.status == 'imported'
            # An unrelated snapshot cannot override this order's display metadata.
            p._bridge_snapshot = {**row.model_dump(mode='json'), 'order_id': 'wrong', 'payment_method': 'wrong'}
            assert PesananOut.model_validate(p).payment_method == ''
    finally:
        await engine.dispose()
