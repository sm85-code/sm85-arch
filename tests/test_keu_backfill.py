"""Historical range overrides the cursor without provider writes or local status resets."""
from datetime import date, datetime, timedelta, timezone

import httpx2
import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import test_keu
from tenants.bumi_lestari.modules.bumi_lestari.application import keu_services as svc, keu_sync, schemas_keu as sc
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_keu as m

env = test_keu.env


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,status", [("store", "dikirim"), ("store", "selesai"), ("marketplace_erp", "shipped"), ("marketplace_erp", "completed")])
async def test_backfill_completed_source_starts_local_workflow_and_never_writes_back(env, monkeypatch, kind, status):
    s, user, _, _, account, vendor = env
    ordered = datetime(2026, 8, 31, 17, 30, tzinfo=timezone.utc)
    updated = datetime(2026, 9, 2, 10, tzinfo=timezone.utc)
    released = datetime(2026, 9, 3, 10, tzinfo=timezone.utc)
    watermark = datetime(2026, 10, 1, tzinfo=timezone.utc)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    if kind == "store":
        from tenants.store.modules.store.infrastructure import database, models
        base = database.StoreBase
        product = models.ProdukStore(id="catalog", nama="Sumber", harga=10)
        product.varian = [models.VarianProduk(id="variant", nama="L", sku="BACKFILL-L")]
        source_order = models.PesananStore(id="historical", user_id="buyer", status=status, total=20, created_at=ordered, updated_at=updated)
        source_order.items = [models.ItemPesanan(id="line", produk_id="catalog", varian_id="variant", nama_produk="Sumber", nama_varian="L", qty=2, harga_satuan=10, subtotal=20)]
        rows = [product, source_order]
    else:
        from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import database, models
        base = database.MarketplaceErpBase
        source_order = models.Pesanan(id="historical", akun_id="acct", platform="shopee", id_eksternal="BACKFILL-SN", status=status,
            total=20, dipesan_at=ordered, created_at=updated, updated_at=updated)
        source_order.items = [models.ItemPesanan(id="line", nama_produk="Sumber", model_name="L", model_sku="BACKFILL-L", item_sku="BACKFILL", qty=2, harga_satuan=10, subtotal=20)]
        rows = [models.AkunMarketplace(id="acct", platform="shopee", nama_toko="Backfill"), source_order,
            models.SettlementPesanan(id="payment", akun_id="acct", order_sn="BACKFILL-SN", penjualan=20, jumlah_cair=18, dirilis_at=released, diambil_at=updated)]
    reads = []
    async def forbidden_http(*args, **kwargs):
        raise AssertionError("Backfill must not call a provider")
    monkeypatch.setattr(httpx2.AsyncClient, "request", forbidden_http)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(base.metadata.create_all)
        async with maker.begin() as source:
            source.add_all(rows)
        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def observe(conn, cursor, statement, parameters, context, executemany):
            reads.append(statement)
        monkeypatch.setattr(database, "SessionLocal", maker)
        sal = await svc.create_master(s, user, "saluran", sc.SaluranIn(nama="Backfill", sistem=kind, akun_ref="store" if kind == "store" else "acct", aktif=True))
        cursor = m.KeuCursor(saluran_id=sal["id"], entitas="order", watermark_at=watermark, watermark_ref="latest")
        s.add(cursor)
        await s.flush()
        assert (await keu_sync.pull(s, user, sal["id"], "order"))["dibaca"] == 0
        dates = {"tanggal_awal": date(2026, 9, 1), "tanggal_akhir": date(2026, 9, 1)}
        pulled = await keu_sync.pull(s, user, sal["id"], "order", **dates)
        assert pulled["dibaca"] == pulled["terproses"] == 1 and pulled["gagal"] == 0
        order = (await s.execute(select(m.KeuPesanan).where(m.KeuPesanan.saluran_id == sal["id"]))).scalar_one()
        assert order.status == "draf" and order.status_sumber == status and order.tanggal == date(2026, 9, 1)
        item = (await s.execute(select(m.KeuItem).where(m.KeuItem.pesanan_id == order.id))).scalar_one()
        product = await s.get(m.KeuProduk, item.produk_id)
        assert product.sku == "BACKFILL-L" and product.status == "draf"
        assert not (await s.execute(select(m.KeuTransaksi))).first()
        await svc.save_product(s, user, product.id, sc.ProdukEditIn(nama="Alias", jenis="kayu", biaya_acuan="5"))
        await svc.allocate_vendor(s, user, sc.AlokasiVendorIn(item_id=item.id, vendor_id=vendor["vendor_id"], qty=2, biaya_satuan="5"))
        await svc.order_status(s, user, order.id, sc.PesananStatusIn(status="aktif"))
        await svc.order_status(s, user, order.id, sc.PesananStatusIn(status="selesai"))
        if kind == "marketplace_erp":
            settlement_dates = {"tanggal_awal": date(2026, 9, 3), "tanggal_akhir": date(2026, 9, 3)}
            assert (await keu_sync.pull(s, user, sal["id"], "settlement", **settlement_dates))["terproses"] == 1
            settlement = (await s.execute(select(m.KeuSettlement).where(m.KeuSettlement.saluran_id == sal["id"]))).scalar_one()
            assert settlement.status == "draf" and settlement.tanggal_cair == date(2026, 9, 3)
            assert not (await s.execute(select(m.KeuTransaksi))).first()
            await svc.allocate_settlement(s, user, sc.AlokasiSettlementIn(settlement_id=settlement.id, item_id=item.id, jumlah="18"))
            await svc.post_settlement(s, user, settlement.id, sc.PostingSettlementIn(akun_id=account["id"], kategori_id="income"))
            posted = (await s.execute(select(m.KeuTransaksi))).scalar_one()
            assert posted.tanggal == date(2026, 9, 3) and str(posted.jumlah) == "18.00" and posted.status == "terkirim"
            repeated_settlement = await keu_sync.pull(s, user, sal["id"], "settlement", **settlement_dates)
            assert repeated_settlement["terproses"] == repeated_settlement["gagal"] == 0
        else:
            with pytest.raises(HTTPException):
                await keu_sync.pull(s, user, sal["id"], "settlement", **dates)
            assert not (await s.execute(select(m.KeuTransaksi))).first()
        # Simulate a new source revision; backfill still must not rewind completed local work.
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
        revision_time = updated + timedelta(days=5)
        async with maker.begin() as source:
            revised = await source.get(type(source_order), "historical")
            revised.updated_at = revision_time
        event.listen(engine.sync_engine, "before_cursor_execute", observe)
        revised_pull = await keu_sync.pull(s, user, sal["id"], "order", **dates)
        assert revised_pull["terproses"] == 1 and revised_pull["gagal"] == 0
        assert order.status == "selesai" and product.nama == "Alias"
        repeated = await keu_sync.pull(s, user, sal["id"], "order", **dates)
        assert repeated["terproses"] == repeated["gagal"] == 0
        assert order.status == "selesai" and order.status_sumber == status and product.nama == "Alias"
        assert cursor.watermark_at == watermark and cursor.watermark_ref == "latest"
        assert all(statement.lstrip().upper().startswith("SELECT") for statement in reads), reads
        async with maker() as source:
            unchanged = await source.get(type(source_order), "historical")
            assert unchanged.status == status and unchanged.updated_at.replace(tzinfo=timezone.utc) == revision_time
    finally:
        await engine.dispose()
