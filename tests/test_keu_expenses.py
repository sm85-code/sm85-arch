from datetime import date
from decimal import Decimal

import httpx2 as httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy import select

import test_keu
import test_keu_finance
from tenants.bumi_lestari.adapters.api.v1.keu_router import router
from tenants.bumi_lestari.modules.bumi_lestari.application import keu_accounting as accounting, keu_expenses as expenses, keu_ledger as ledger, keu_services as svc, schemas_keu as old, schemas_keu_finance as finance
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_keu_expenses import CATEGORIES, PengeluaranIn
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import get_current_user_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlKategori
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_keu_finance as f

env = test_keu.env
DAY = date(2026, 10, 6)


@pytest.mark.asyncio
@pytest.mark.parametrize('key', list(CATEGORIES))
async def test_category_posts_correct_account_and_profit_classification(env, key):
    s,u,_,_,account,_=env
    await test_keu_finance.initialize(env)
    tab,_,code=CATEGORIES[key]
    payload=PengeluaranIn(referensi=key,tanggal=DAY,akun_kas_id=account['id'],tab=tab,kategori=key,jumlah='10.25',keterangan='Pembelian sesuai kategori')
    journal=await expenses.create(s,u,payload)
    debit=(await s.execute(select(f.KeuCoa.kode).join(f.KeuJurnalBaris,f.KeuJurnalBaris.coa_id==f.KeuCoa.id).where(
        f.KeuJurnalBaris.jurnal_id==journal['id'],f.KeuJurnalBaris.debet>0))).scalar_one()
    assert debit==code
    report=await ledger.reports(s,DAY,DAY)
    assert Decimal(report['laba_rugi']['hpp'])==(Decimal('10.25') if tab=='bahan' else 0)
    assert Decimal(report['laba_rugi']['beban_operasional'])==(0 if tab=='bahan' else Decimal('10.25'))
    assert Decimal(report['neraca']['selisih'])==0
    assert Decimal(report['arus_kas']['saldo_akhir'])==Decimal('-10.25')
    assert (await expenses.create(s,u,payload))['id']==journal['id']
    await ledger.unpost(s,u,journal['id'],old.BatalIn(alasan='Koreksi pembayaran'))
    report=await ledger.reports(s,DAY,DAY)
    assert Decimal(report['laba_rugi']['shu'])==0 and Decimal(report['arus_kas']['saldo_akhir'])==0
    assert (await expenses.create(s,u,payload))['status']=='dibatalkan'
    with pytest.raises(HTTPException):
        await expenses.create(s,u,payload.model_copy(update={'jumlah':Decimal('11')}))


@pytest.mark.asyncio
async def test_tabs_date_search_literal_wildcards_pagination_and_history(env):
    s,u,_,_,account,_=env
    await test_keu_finance.initialize(env)
    first=await expenses.create(s,u,PengeluaranIn(referensi='cat1',tanggal=DAY,akun_kas_id=account['id'],tab='bahan',kategori='cat',jumlah='5',keterangan='Cat 50% _ Natural'))
    await expenses.create(s,u,PengeluaranIn(referensi='cat2',tanggal=date(2026,10,7),akun_kas_id=account['id'],tab='bahan',kategori='lem',jumlah='6',keterangan='Lem kayu'))
    await expenses.create(s,u,PengeluaranIn(referensi='internet',tanggal=DAY,akun_kas_id=account['id'],tab='operasional',kategori='internet',jumlah='7',keterangan='Langganan internet'))
    rows=await expenses.listing(s,'bahan',DAY,DAY,'50% _','pengerjaan',50,0)
    assert rows['total']==1 and rows['rows'][0]['id']==first['id']
    assert (await expenses.listing(s,'bahan',DAY,date(2026,10,7),'','pengerjaan',1,1))['total']==2
    assert (await expenses.listing(s,'operasional',DAY,DAY,'','pengerjaan',50,0))['total']==1
    await ledger.unpost(s,u,first['id'],old.BatalIn(alasan='Koreksi bahan'))
    assert (await expenses.listing(s,'bahan',DAY,DAY,'','pengerjaan',50,0))['total']==0
    history=await expenses.listing(s,'bahan',DAY,DAY,'','batal',50,0)
    assert history['total']==1 and history['rows'][0]['jumlah']=='5.00'


@pytest.mark.asyncio
async def test_vendor_payment_does_not_double_hpp_and_searches_vendor(env):
    s,u,sal,product,account,vendor=env
    await test_keu_finance.initialize(env)
    order=await svc.create_order(s,u,test_keu.order_payload(sal,product))
    await svc.allocate_vendor(s,u,old.AlokasiVendorIn(item_id=order['items'][0]['id'],vendor_id=vendor['vendor_id'],qty=2,biaya_satuan='5'))
    await svc.order_status(s,u,order['id'],old.PesananStatusIn(status='aktif'))
    await svc.order_status(s,u,order['id'],old.PesananStatusIn(status='selesai'))
    before=await ledger.reports(s,DAY,DAY)
    payment=await accounting.pay_vendor(s,u,finance.BayarVendorIn(referensi='paid',tanggal=DAY,vendor_id=vendor['vendor_id'],akun_kas_id=account['id'],jumlah='10'))
    after=await ledger.reports(s,DAY,DAY)
    assert before['laba_rugi']['hpp']==after['laba_rugi']['hpp']=='10.00'
    assert await accounting.vendor_debt(s,vendor['vendor_id'])==0
    rows=await expenses.listing(s,'vendor',DAY,DAY,'','pengerjaan',50,0)
    assert rows['total']==1 and rows['rows'][0]['id']==payment['id']
    assert (await expenses.listing(s,'operasional',DAY,DAY,'','pengerjaan',50,0))['total']==0
    await ledger.unpost(s,u,payment['id'],old.BatalIn(alasan='Koreksi pelunasan'))
    assert await accounting.vendor_debt(s,vendor['vendor_id'])==10
    assert (await ledger.reports(s,DAY,DAY))['laba_rugi']['hpp']=='10.00'


@pytest.mark.asyncio
async def test_legacy_salary_advertising_future_mapping_and_historical_tab(env):
    s,u,sal,_,account,_=env
    await test_keu_finance.initialize(env)
    for key,name in [('salary','Gaji karyawan'),('ads','Biaya iklan')]:
        s.add(BlKategori(id=key,nama=name,jenis='pengeluaran',aktif=True))
        await s.flush()
        tx=await svc.transaction(s,u,old.TransaksiIn(saluran_id=sal['id'],sumber_ref=key,akun_id=account['id'],kategori_id=key,tanggal=DAY,jenis='keluar',jumlah='4'))
        await svc.post_transaction(s,u,tx['id'])
    assert (await expenses.listing(s,'gaji_iklan',DAY,DAY,'','pengerjaan',50,0))['total']==2
    assert (await expenses.listing(s,'operasional',DAY,DAY,'','pengerjaan',50,0))['total']==0


@pytest.mark.asyncio
async def test_expense_api_role_password_range_and_server_controlled_classification(env):
    s,u,_,_,account,_=env
    await test_keu_finance.initialize(env)
    app=FastAPI()
    app.include_router(router)
    async def db():
        yield s
    async def actor():
        return u
    app.dependency_overrides[get_db_bumi_lestari]=db
    app.dependency_overrides[get_current_user_bumi_lestari]=actor
    payload={'referensi':'x','tanggal':str(DAY),'akun_kas_id':account['id'],'tab':'bahan','kategori':'cat','jumlah':'10','keterangan':'Pembelian cat'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        u.role='staff'
        assert (await client.post('/keu/pengeluaran',json=payload)).status_code==403
        assert (await client.get('/keu/pengeluaran/kategori')).status_code==403
        u.role,u.must_change_password='admin',True
        assert (await client.post('/keu/pengeluaran',json=payload)).status_code==403
        u.must_change_password=False
        for values in [{**payload,'kategori':'gaji'},{**payload,'coa_id':'coa:MODAL'},{**payload,'jumlah':'0'},{**payload,'jumlah':'NaN'},{**payload,'jumlah':'1.001'},{**payload,'tab':'vendor'}]:
            assert (await client.post('/keu/pengeluaran',json=values)).status_code==422
        assert (await client.get('/keu/pengeluaran?tab=bahan&tanggal_awal=2026-10-07&tanggal_akhir=2026-10-06')).status_code==422
        result=await client.post('/keu/pengeluaran',json=payload)
        assert result.status_code==201
        assert (await client.get(f'/keu/pengeluaran?tab=bahan&tanggal_awal={DAY}&tanggal_akhir={DAY}')).json()['total']==1


def test_schema_requires_matching_classification():
    with pytest.raises(ValidationError):
        PengeluaranIn(referensi='x',tanggal=DAY,akun_kas_id='a',tab='operasional',kategori='unknown',jumlah=1,keterangan='Test')


@pytest.mark.asyncio
async def test_historical_completed_vendor_allocations_migrate_without_generator_error(env):
    s,u,sal,product,_,vendor=env
    order=await svc.create_order(s,u,test_keu.order_payload(sal,product))
    await svc.allocate_vendor(s,u,old.AlokasiVendorIn(item_id=order['items'][0]['id'],vendor_id=vendor['vendor_id'],qty=2,biaya_satuan='5'))
    await svc.order_status(s,u,order['id'],old.PesananStatusIn(status='aktif'))
    await svc.order_status(s,u,order['id'],old.PesananStatusIn(status='selesai'))
    await test_keu_finance.initialize(env)
    assert await accounting.vendor_debt(s,vendor['vendor_id'])==10
    assert (await ledger.reports(s,DAY,DAY))['laba_rugi']['hpp']=='10.00'


@pytest.mark.asyncio
async def test_existing_custom_account_collision_cannot_redirect_hpp_to_assets(env):
    s,u,_,_,account,_=env
    s.add(f.KeuCoa(id='collision',kode='HPP-BAHAN',nama='Custom lama',jenis='aset',kelompok='aset_lain',sistem=False))
    await s.flush()
    await test_keu_finance.initialize(env)
    with pytest.raises(HTTPException) as error:
        await expenses.create(s,u,PengeluaranIn(referensi='guard',tanggal=DAY,akun_kas_id=account['id'],tab='bahan',kategori='cat',jumlah='10',keterangan='Pembelian cat'))
    assert error.value.status_code==409
    assert (await s.execute(select(f.KeuJurnal))).first() is None
