from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

import test_keu
from tenants.bumi_lestari.modules.bumi_lestari.application import keu_accounting as accounting, keu_inventory as inventory, keu_ledger as ledger, keu_services as svc, schemas_keu as old, schemas_keu_finance as sc
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_keu_finance as f
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlTutupBuku

env = test_keu.env
DAY = date(2026, 10, 6)


async def initialize(env):
    s, u, *_ = env
    return await accounting.initialize(s, u, sc.BukuIn(tanggal_awal=DAY, metode_stok="fifo", tanggal_status="updated_at", histori=True, konfirmasi="AKTIFKAN-BUKU-KEU"))


@pytest.mark.asyncio
async def test_histori_opening_closed_period_and_cash_unpost(env):
    s, u, sal, _, account, _ = env
    await svc.edit_master(s, u, "akun", account["id"], old.AkunIn(kode="KAS", nama="Kas", jenis="kas", saldo_awal="500"))
    payload = old.TransaksiIn(saluran_id=sal["id"], sumber_ref="expense", akun_id=account["id"], kategori_id="expense", tanggal=DAY, jenis="keluar", jumlah="20")
    tx = await svc.transaction(s, u, payload)
    await svc.post_transaction(s, u, tx["id"])
    s.add(BlTutupBuku(periode="2026-10", ditutup_oleh=u.id, snapshot={}))
    await s.flush()
    await initialize(env)
    reports = await ledger.reports(s, DAY, DAY)
    assert Decimal(reports["neraca"]["total_aset"]) == 480
    assert Decimal(reports["laba_rugi"]["shu"]) == -20
    assert Decimal(reports["arus_kas"]["saldo_awal"]) == 500
    assert Decimal(reports["arus_kas"]["saldo_akhir"]) == 480
    assert Decimal(reports["neraca"]["selisih"]) == 0
    with pytest.raises(HTTPException):
        await svc.unpost_transaction(s, u, tx["id"], old.BatalIn(alasan="Koreksi periode tertutup"))
    row = (await s.execute(select(BlTutupBuku))).scalar_one()
    row.status = "dibuka"
    await s.flush()
    await svc.unpost_transaction(s, u, tx["id"], old.BatalIn(alasan="Koreksi kas"))
    reports = await ledger.reports(s, DAY, DAY)
    assert Decimal(reports["neraca"]["total_aset"]) == 500
    assert Decimal(reports["laba_rugi"]["shu"]) == 0


@pytest.mark.asyncio
async def test_transfer_atomic_fee_not_revenue_idempotence_and_unpost(env):
    s, u, _, _, account, _ = env
    second = await svc.create_master(s, u, "akun", old.AkunIn(kode="BANK", nama="Bank", jenis="bank"))
    await initialize(env)
    payload = sc.MutasiIn(referensi="x", tanggal=DAY, akun_asal_id=account["id"], akun_tujuan_id=second["id"], nominal="100", biaya_admin="2")
    journal = await ledger.transfer(s, u, payload)
    assert (await ledger.transfer(s, u, payload))["id"] == journal["id"]
    report = await ledger.reports(s, DAY, DAY)
    assert Decimal(report["laba_rugi"]["pendapatan_bersih"]) == 0
    assert Decimal(report["laba_rugi"]["shu"]) == -2
    assert Decimal(report["arus_kas"]["kelompok"]["mutasi"]["neto"]) == 0
    assert Decimal(report["arus_kas"]["kelompok"]["operasional"]["keluar"]) == 2
    assert Decimal((await svc.dashboard(s))["saldo_kas"]) == -2
    await ledger.unpost(s, u, journal["id"], old.BatalIn(alasan="Salah transfer"))
    assert Decimal((await ledger.reports(s, DAY, DAY))["laba_rugi"]["shu"]) == 0
    assert (await ledger.transfer(s, u, payload))["status"] == "dibatalkan"
    with pytest.raises(HTTPException):
        await ledger.transfer(s, u, payload.model_copy(update={"nominal": Decimal("101")}))


@pytest.mark.asyncio
async def test_order_shipping_to_escrow_original_backfill_and_updated_date(env):
    s, u, sal, product, _, _ = env
    await initialize(env)
    payload = test_keu.order_payload(sal, product).model_copy(update={"status_sumber": "shipped", "sumber_updated_at": datetime(2026,10,6,tzinfo=timezone.utc)})
    order = await svc.create_order(s, u, payload, from_source=True)
    assert order["status"] == "draf"
    assert await accounting.order_balance(s, order["id"], "PIUTANG-KIRIM") == 20
    completed = payload.model_copy(update={"status_sumber": "completed", "sumber_updated_at": datetime(2026,10,7,tzinfo=timezone.utc)})
    await svc.create_order(s, u, completed, from_source=True)
    assert await accounting.order_balance(s, order["id"], "PIUTANG-KIRIM") == 0
    assert await accounting.order_balance(s, order["id"], "PIUTANG-ESCROW") == 20
    escrow = (await s.execute(select(f.KeuJurnal).where(f.KeuJurnal.jenis == "escrow"))).scalar_one()
    assert escrow.tanggal == date(2026,10,7) and escrow.rincian["provenance_tanggal"] == "sumber_updated_at"
    earlier = await ledger.reports(s, DAY, DAY)
    assert next(r["nilai"] for r in earlier["neraca"]["aset"] if r["kelompok"] == "piutang_pengiriman") == "20.00"
    await svc.create_order(s, u, completed, from_source=True)
    assert len((await s.execute(select(f.KeuJurnal))).scalars().all()) == 2
    backfill = completed.model_copy(update={"sumber_ref": "backfill", "nomor": "backfill"})
    new = await svc.create_order(s, u, backfill, from_source=True)
    j = (await s.execute(select(f.KeuJurnal).where(f.KeuJurnal.pesanan_id == new["id"]))).scalar_one()
    assert j.tanggal == DAY and j.rincian["provenance_tanggal"] == "tanggal_pesanan_backfill"


@pytest.mark.asyncio
async def test_settlement_credits_escrow_admin_fee_and_unpost(env):
    s, u, sal, product, account, _ = env
    await initialize(env)
    order = await svc.create_order(s, u, test_keu.order_payload(sal, product).model_copy(update={"status_sumber":"completed"}), from_source=True)
    st = await svc.create_settlement(s, u, old.SettlementIn(saluran_id=sal["id"], sumber_ref="paid", tanggal_cair=DAY, bruto="20", potongan="2", neto="18"))
    await svc.allocate_settlement(s, u, old.AlokasiSettlementIn(settlement_id=st["id"], item_id=order["items"][0]["id"], jumlah="18"))
    await svc.post_settlement(s, u, st["id"], old.PostingSettlementIn(akun_id=account["id"], kategori_id="income"))
    report = await ledger.reports(s, DAY, DAY)
    assert await accounting.order_balance(s, order["id"], "PIUTANG-ESCROW") == 0
    assert Decimal(report["laba_rugi"]["shu"]) == 18
    assert Decimal(report["neraca"]["total_aset"]) == 18
    sale = (await s.execute(select(f.KeuJurnal).where(f.KeuJurnal.jenis == "penjualan"))).scalar_one()
    with pytest.raises(HTTPException):
        await ledger.unpost(s, u, sale.id, old.BatalIn(alasan="Belum unpost settlement"))
    await svc.unpost_settlement(s, u, st["id"], old.BatalIn(alasan="Bukti salah"))
    assert await accounting.order_balance(s, order["id"], "PIUTANG-ESCROW") == 20
    report = await ledger.reports(s, DAY, DAY)
    assert Decimal(report["laba_rugi"]["shu"]) == 20
    assert Decimal(report["arus_kas"]["saldo_akhir"]) == 0
    assert Decimal(report["neraca"]["selisih"]) == 0


@pytest.mark.asyncio
async def test_fifo_batches_hpp_inventory_vendor_debt_and_dependencies(env):
    s, u, sal, product, account, vendor = env
    await initialize(env)
    order = await svc.create_order(s, u, test_keu.order_payload(sal, product, qty=3))
    first = await inventory.receive(s, u, sc.StokMasukIn(referensi="b1", tanggal=DAY, produk_id=product["id"], qty=2, biaya_total="10", sumber="produksi", vendor_id=vendor["vendor_id"]))
    await inventory.receive(s, u, sc.StokMasukIn(referensi="b2", tanggal=DAY, produk_id=product["id"], qty=3, biaya_total="21", sumber="pembelian", akun_kas_id=account["id"]))
    out = await inventory.fulfill(s, u, sc.StokKeluarIn(referensi="use", tanggal=DAY, item_id=order["items"][0]["id"], qty=3))
    movement = (await s.execute(select(f.KeuStokMutasi).where(f.KeuStokMutasi.jurnal_id == out["id"]))).scalar_one()
    assert movement.nilai == 17
    uses = (await s.execute(select(f.KeuStokPemakaian).where(f.KeuStokPemakaian.keluar_id == movement.id))).scalars().all()
    assert sorted((use.qty, use.nilai) for use in uses) == [(1,Decimal("7")),(2,Decimal("10"))]
    assert sum(lot["qty"] for lot in await inventory.batches(s, product["id"])) == 2
    report = await ledger.reports(s, DAY, DAY)
    assert Decimal(report["laba_rugi"]["hpp"]) == 17
    assert Decimal(next(r["nilai"] for r in report["neraca"]["aset"] if r["kelompok"]=="persediaan")) == 14
    assert Decimal(report["neraca"]["total_kewajiban"]) == 10
    with pytest.raises(HTTPException):
        await ledger.unpost(s, u, first["id"], old.BatalIn(alasan="Batch sudah dipakai"))
    with pytest.raises(HTTPException):
        await svc.allocate_vendor(s, u, old.AlokasiVendorIn(item_id=order["items"][0]["id"],vendor_id=vendor["vendor_id"],qty=1,biaya_satuan="5"))
    await ledger.unpost(s, u, out["id"], old.BatalIn(alasan="Koreksi pengambilan"))
    assert sum(lot["qty"] for lot in await inventory.batches(s, product["id"])) == 5
    paid = await accounting.pay_vendor(s, u, sc.BayarVendorIn(referensi="pay",tanggal=DAY,vendor_id=vendor["vendor_id"],akun_kas_id=account["id"],jumlah="10"))
    assert await accounting.vendor_debt(s, vendor["vendor_id"]) == 0
    with pytest.raises(HTTPException):
        await ledger.unpost(s, u, first["id"], old.BatalIn(alasan="Sudah dibayar"))
    await ledger.unpost(s, u, paid["id"], old.BatalIn(alasan="Batalkan pembayaran"))
    await ledger.unpost(s, u, first["id"], old.BatalIn(alasan="Batalkan penerimaan"))
    assert await accounting.vendor_debt(s, vendor["vendor_id"]) == 0
    assert Decimal((await ledger.reports(s, DAY, DAY))["neraca"]["selisih"]) == 0


@pytest.mark.asyncio
async def test_original_category_capital_distribution_not_profit(env):
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlKategori
    s, u, sal, _, account, _ = env
    for key, name, kind, amount in [('capital','Setoran modal','pemasukan','100'),('draw','Prive','pengeluaran','10'),('subscription','Langganan & utilitas','pengeluaran','5'),('production','Biaya produksi / pembelian barang','pengeluaran','10')]:
        s.add(BlKategori(id=key,nama=name,jenis=kind,aktif=True))
        await s.flush()
        tx = await svc.transaction(s,u,old.TransaksiIn(saluran_id=sal['id'],sumber_ref=key,akun_id=account['id'],kategori_id=key,tanggal=DAY,jenis='masuk' if kind=='pemasukan' else 'keluar',jumlah=amount))
        await svc.post_transaction(s,u,tx['id'])
    await initialize(env)
    report = await ledger.reports(s,DAY,DAY)
    assert Decimal(report['laba_rugi']['shu']) == -15
    assert Decimal(report['laba_rugi']['hpp']) == 10
    assert Decimal(report['neraca']['total_aset']) == 75
    assert Decimal(report['arus_kas']['kelompok']['pendanaan']['neto']) == 90
    assert Decimal(report['arus_kas']['kelompok']['operasional']['neto']) == -15


@pytest.mark.asyncio
async def test_finance_api_authorization_validation_cashbook_and_store_proof(env):
    import httpx2 as httpx
    from fastapi import FastAPI
    from tenants.bumi_lestari.adapters.api.v1.keu_router import router
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import get_current_user_bumi_lestari
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
    s,u,_,_,account,_=env
    app=FastAPI()
    app.include_router(router)
    async def db():
        yield s
    async def actor():
        return u
    app.dependency_overrides[get_db_bumi_lestari]=db
    app.dependency_overrides[get_current_user_bumi_lestari]=actor
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        u.role='staff'
        assert (await client.get('/keu/buku')).status_code==403
        u.role='admin'
        assert (await client.post('/keu/buku/aktivasi',json=sc.BukuIn(tanggal_awal=DAY,metode_stok='fifo',tanggal_status='updated_at',histori=True,konfirmasi='AKTIFKAN-BUKU-KEU').model_dump(mode='json'))).status_code==403
        u.must_change_password=True
        assert (await client.get('/keu/piutang')).status_code==403
        u.role,u.must_change_password='owner',False
        await initialize(env)
        body={'referensi':'bad','tanggal':str(DAY),'akun_asal_id':account['id'],'akun_tujuan_id':account['id'],'nominal':'1'}
        assert (await client.post('/keu/mutasi',json=body)).status_code==422
        assert (await client.get('/keu/laporan-keuangan?tanggal_awal=2026-10-07&tanggal_akhir=2026-10-06')).status_code==422
        bank=await svc.create_master(s,u,'akun',old.AkunIn(kode='BANK',nama='Bank',jenis='bank'))
        body.update(akun_tujuan_id=bank['id'],nominal='10',biaya_admin='1')
        posted=await client.post('/keu/mutasi',json=body)
        assert posted.status_code==201
        row=(await client.get(f'/keu/buku-kas?tanggal_awal={DAY}&tanggal_akhir={DAY}&akun_id={account["id"]}')).json()
        assert row['total']==2 and Decimal(row['rows'][-1]['saldo'])==-11
        store=await svc.create_master(s,u,'saluran',old.SaluranIn(nama='Store',sistem='store',akun_ref='store',aktif=True))
        payout={'saluran_id':store['id'],'sumber_ref':'bank-proof','tanggal_cair':str(DAY),'bruto':'20','potongan':'2','neto':'18'}
        assert (await client.post('/keu/settlement',json=payout)).status_code==403
        assert (await client.post('/keu/settlement/manual-bukti',json=payout)).status_code==422
        result=await client.post('/keu/settlement/manual-bukti',json={**payout,'bukti_pencairan':'BANK-001'})
        assert result.status_code==201 and result.json()['rincian']['bukti_pencairan']=='BANK-001'
        assert result.json()['rincian']['provenance']=='konfirmasi_manual_internal'


@pytest.mark.asyncio
async def test_fifo_rounding_exact_batch_cost_and_cancelled_order_exclusion(env):
    s,u,sal,product,account,_=env
    await initialize(env)
    order=await svc.create_order(s,u,test_keu.order_payload(sal,product,qty=3))
    await inventory.receive(s,u,sc.StokMasukIn(referensi='cent',tanggal=DAY,produk_id=product['id'],qty=3,biaya_total='0.01',sumber='pembelian',akun_kas_id=account['id']))
    entries=[]
    for n in range(3):
        entries.append(await inventory.fulfill(s,u,sc.StokKeluarIn(referensi=f'cent-{n}',tanggal=DAY,item_id=order['items'][0]['id'],qty=1)))
    costs=(await s.execute(select(f.KeuStokMutasi.nilai).where(f.KeuStokMutasi.jenis=='keluar'))).scalars().all()
    assert sum(costs,Decimal('0'))==Decimal('0.01')
    assert sum(batch['qty'] for batch in await inventory.batches(s,product['id']))==0
    assert Decimal((await ledger.reports(s,DAY,DAY))['neraca']['selisih'])==0
    with pytest.raises(HTTPException):
        await ledger.unpost(s,u,entries[0]['id'],old.BatalIn(alasan='Harus koreksi berurutan'))
    for entry in reversed(entries):
        await ledger.unpost(s,u,entry['id'],old.BatalIn(alasan='Koreksi FIFO'))
    assert sum(batch['qty'] for batch in await inventory.batches(s,product['id']))==3
