"""Disposable-schema tests: never use the production database."""
import os
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tenants.marketplace_erp.modules.marketplace_erp.application import product_analysis as pa
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import AkunMarketplace, ItemPesanan, Pesanan
from tenants.marketplace_erp.adapters.api.v1.product_analysis_router import scope
pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="Disposable Postgres required")

@pytest.mark.asyncio
async def test_status_scope_dates_duplicate_snapshots_and_totals():
    url=os.environ["TEST_DATABASE_URL"].replace("postgresql://","postgresql+asyncpg://",1)
    schema="product_test_"+uuid.uuid4().hex
    setup=create_async_engine(url)
    async with setup.begin() as c:
        await c.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine=create_async_engine(url,connect_args={"server_settings":{"search_path":schema}})
    factory=async_sessionmaker(engine,expire_on_commit=False)
    try:
        async with engine.begin() as c:
            await c.run_sync(MarketplaceErpBase.metadata.create_all)
        async with factory() as s:
            s.add(AkunMarketplace(id="a",platform="shopee",nama_toko="A",id_toko_eksternal="1"))
            s.add(AkunMarketplace(id="b",platform="shopee",nama_toko="B",id_toko_eksternal="9"))
            await s.flush()
            specs=[("ok","a","completed","COMPLETED",2,1),("cancel","a","shipped","CANCELLED",9,1),
                ("refund","a","completed","REFUND_COMPLETED",7,1),("unpaid","a","unpaid","UNPAID",6,1),
                ("other","b","shipped","SHIPPED",3,1),("old","a","completed","COMPLETED",10,0)]
            for key,shop,local,native,qty,day in specs:
                s.add(Pesanan(id=key,id_eksternal=key,platform="shopee",akun_id=shop,status=local,status_marketplace=native,
                    dipesan_at=datetime(2026,11,15 if day else 14,17,tzinfo=timezone.utc),detail_json='{"currency":"IDR"}'))
                await s.flush()
                s.add(ItemPesanan(pesanan_id=key,item_id_eksternal="2",model_id_eksternal="3",nama_produk="Rak",model_name="60",
                    harga_satuan=10,qty=qty,subtotal=qty*10))
            await s.commit()
            # Repeated sync updates the one stored order; it does not add another sale.
            o = await s.get(Pesanan, "ok")
            o.status_marketplace = "COMPLETED"
            await s.commit()
        async with factory() as s:
            await s.execute(text("SET TRANSACTION READ ONLY"))
            d=await pa.read(s,date(2026,11,16),date(2026,11,16),None)
            assert d["totals"]==dict(units=5,sales=Decimal(50))
            assert d["total"]==2
            assert (await pa.read(s,date(2026,11,16),date(2026,11,16),["a"]))["totals"]["units"]==2
            assert (await pa.read(s,date(2026,11,16),date(2026,11,16),None,"b"))["totals"]["units"]==3
            assert (await pa.read(s,date(2026,11,16),date(2026,11,16),[]))["total"]==0
            assert await scope(s,SimpleNamespace(role="admin",id="u")) is None
            with pytest.raises(HTTPException) as exc:
                await scope(s,SimpleNamespace(role="unknown",id="u"))
            assert exc.value.status_code==403
    finally:
        await engine.dispose()
        async with setup.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await setup.dispose()
