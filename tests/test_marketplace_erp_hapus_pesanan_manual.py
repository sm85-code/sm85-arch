import json

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    PLATFORM_MANUAL, ItemPesanan, MarketplaceErpBase, Pesanan, PesananHapusLog, ProcessingJob, StokLedger, Produk,
)


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def order(session, platform=PLATFORM_MANUAL, **kw):
    p = Pesanan(platform=platform, id_eksternal=kw.pop("sn", "INT-1"), status=kw.pop("status", "to_ship"), nama_pembeli="A",
                total=0, detail_json="{}", **kw)
    session.add(p)
    await session.flush()
    item = ItemPesanan(pesanan_id=p.id, nama_produk="Rak", model_name="", qty=1, harga_satuan=0, subtotal=0)
    session.add(item)
    await session.flush()
    return p, item


async def blocked(session, pid, text):
    with pytest.raises(HTTPException) as e:
        await services.delete_pesanan(session, pid, oleh="owner")
    assert e.value.status_code == 409 and text in e.value.detail


@pytest.mark.asyncio
async def test_manual_order_deleted_with_audit(factory):
    async with factory() as s:
        p, _ = await order(s)
        await services.delete_pesanan(s, p.id, oleh="owner@x")
        await s.commit()
        assert await s.get(Pesanan, p.id) is None
        log = (await s.execute(select(PesananHapusLog))).scalar_one()
        assert log.id_eksternal == "INT-1" and log.dihapus_oleh == "owner@x" and json.loads(log.snapshot_json)["items"][0]["nama_produk"] == "Rak"


@pytest.mark.asyncio
async def test_marketplace_order_never_deleted(factory):
    async with factory() as s:
        p, _ = await order(s, platform="shopee", status="unpaid")
        await blocked(s, p.id, "Hanya pesanan manual")


@pytest.mark.asyncio
async def test_shipped_blocked_but_cancelled_allowed(factory):
    async with factory() as s:
        p, _ = await order(s, nomor_resi="JX1")
        await blocked(s, p.id, "sudah dikirim")
        p.status = "cancelled"
        await s.flush()
        await services.delete_pesanan(s, p.id)


@pytest.mark.asyncio
async def test_linked_records_block(factory):
    async with factory() as s:
        p, item = await order(s)
        s.add(ProcessingJob(source_item_id=item.id, payload_json="{}"))
        prod = Produk(sku_induk="S1", nama="Rak")
        s.add(prod)
        await s.flush()
        s.add(StokLedger(produk_id=prod.id, qty_delta=-1, reason="reserve", ref_type="pesanan", ref_id=p.id))
        await s.flush()
        await blocked(s, p.id, "Pemetaan Order")
        await blocked(s, p.id, "mutasi stok")
        assert await s.get(Pesanan, p.id) is not None
