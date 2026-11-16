"""Disposable-schema PostgreSQL Summary integration; never point at a production DB."""
import os
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import summary
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import AkunMarketplace, ItemPesanan, Pesanan

pytestmark = pytest.mark.skipif(not os.getenv('TEST_DATABASE_URL'), reason='Disposable Postgres required')


@pytest.mark.asyncio
async def test_pg_readonly_real_items_date_filter_reconciliation_and_no_mutations():
    url = os.environ['TEST_DATABASE_URL'].replace('postgresql://', 'postgresql+asyncpg://', 1)
    schema = 'summary_test_' + uuid.uuid4().hex
    setup = create_async_engine(url)
    async with setup.begin() as c:
        await c.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as c:
            await c.run_sync(MarketplaceErpBase.metadata.create_all)
        async with factory() as s:
            s.add(AkunMarketplace(id='shop', platform='shopee', nama_toko='Fixture', id_toko_eksternal='native'))
            await s.flush()
            for key, at, state in [('a', datetime(2026,11,15,17,tzinfo=timezone.utc), 'completed'),
                                   ('b', datetime(2026,11,16,16,59,tzinfo=timezone.utc), 'cancelled'),
                                   ('c', datetime(2026,11,16,17,tzinfo=timezone.utc), 'completed')]:
                s.add(Pesanan(id=key, platform='shopee', id_eksternal=key, akun_id='shop', status=state,
                              dipesan_at=at, detail_json='{"currency":"IDR"}', total=Decimal('999999')))
                await s.flush()
                s.add(ItemPesanan(pesanan_id=key, nama_produk='Fixture', harga_satuan=Decimal('10.01'), qty=3, subtotal=Decimal('30.03')))
            await s.commit()
        async with factory() as s:
            result = await summary.read(s, date(2026,11,16), date(2026,11,16))
            assert await s.scalar(text('SHOW transaction_read_only')) == 'on'
            assert await s.scalar(text('SHOW statement_timeout')) == '5s'
            assert result['totals']['orders'] == 2
            assert result['totals']['gross'] == Decimal('60.06')
            assert result['totals']['units'] == 3
            assert result['totals']['cancelled'] == 1
            # Prove the connection rejects writes in this DISPOSABLE schema.
            with pytest.raises(DBAPIError):
                await s.execute(text("INSERT INTO mpe_pengaturan_ui (id,teks) VALUES ('forbidden','x')"))
            await s.rollback()
        async with factory() as s:
            assert (await s.scalars(select(Pesanan.total))).all() == [Decimal('999999.00')] * 3
    finally:
        await engine.dispose()
        async with setup.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await setup.dispose()
