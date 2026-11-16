import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import LaporanKeuanganManual, LaporanManualAudit
from tenants.marketplace_erp.modules.marketplace_erp.application import financial_manual as m


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as c:
        await c.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def put(db, period, rows, version=0):
    return await m.simpan_struktur(db, period, m.KasStructure(version=version, rows=rows), SimpleNamespace(id='admin'))


@pytest.mark.asyncio
async def test_structure_lifecycle_and_immutable_figures(db):
    period = '2099-01'
    rows = (await m.struktur(db, period))['rows']
    rows += [dict(id='c:parent', label='Baru', parent_id=None, aktif=True, urut=8),
             dict(id='c:shop', label='Lazada', parent_id='k_toko', aktif=True, urut=9)]
    saved = await put(db, period, rows)
    rows[-1]['label'] = 'Lazada baru'
    rows[-1]['parent_id'] = 'c:parent'
    rows[-1]['urut'] = 1
    await put(db, period, rows, saved['version'])
    rows[-1]['aktif'] = False
    await put(db, period, rows, 2)
    rows[-1]['aktif'] = True
    saved = await put(db, period, rows, 3)
    saved['lembar']['sel']['c:shop|2099-01-04'] = '456'
    await m.simpan(db, period, m.Simpan(version=4, lembar=m.Lembar(**saved['lembar'])), SimpleNamespace(id='admin'))
    rows[-1]['label'] = 'Nama terbaru'
    await put(db, period, rows, 5)
    assert (await m.muat(db, period))['lembar']['sel'] == {'c:shop|2099-01-04': '456'}
    audits = (await db.scalars(select(LaporanManualAudit))).all()
    assert len(audits) == 5
    assert any('456' in a.data_json for a in audits)
    rows[-1]['parent_id'] = 'k_cat'
    with pytest.raises(HTTPException, match='bulan berikutnya'):
        await put(db, period, rows, 6)
    await db.rollback()
    # New month inherits only structure; moving cannot alter the saved earlier month.
    new = (await m.struktur(db, '2099-02'))['rows']
    next(r for r in new if r['id'] == 'c:shop')['parent_id'] = 'k_cat'
    await put(db, '2099-02', new)
    assert next(r for r in (await m.struktur(db, period))['rows'] if r['id'] == 'c:shop')['parent_id'] == 'c:parent'
    assert (await m.muat(db, '2099-02'))['lembar']['sel'] == {}


@pytest.mark.asyncio
async def test_invalid_links_duplicate_delete_and_stale(db):
    rows = (await m.struktur(db, '2099-03'))['rows']
    await put(db, '2099-03', rows)
    for bad in [rows+rows[:1], rows[1:], [dict(r, parent_id='k_toko') if r['id']=='k_toko' else r for r in rows], rows+[dict(id='c:x', label='x', parent_id='missing', urut=1)]]:
        with pytest.raises(HTTPException):
            await put(db, '2099-03', bad, 1)
    with pytest.raises(HTTPException) as err:
        await put(db, '2099-03', rows, 0)
    assert err.value.status_code == 409
    with pytest.raises(HTTPException):
        await put(db, '2020-01', rows)
    stored = await db.get(LaporanKeuanganManual, '2099-03')
    assert json.loads(stored.data_json)['sel'] == {}


@pytest.mark.asyncio
async def test_first_child_preserves_standalone_manual_amount(db):
    rows = (await m.struktur(db, '2099-05'))['rows']
    rows.append(dict(id='c:p', label='Pengelola', parent_id=None, urut=10))
    saved = await put(db, '2099-05', rows)
    saved['lembar']['sel']['c:p|2099-05-03'] = '700'
    await m.simpan(db, '2099-05', m.Simpan(version=1, lembar=m.Lembar(**saved['lembar'])), SimpleNamespace(id='admin'))
    rows.append(dict(id='c:sub', label='Baru', parent_id='c:p', urut=11))
    result = await put(db, '2099-05', rows, 2)
    assert result['lembar']['sel']['c:p|2099-05-03'] == '700'
    assert result['lembar']['sel']['c:legacy-c_p|2099-05-03'] == '700'
    assert len([r for r in result['lembar']['baris'] if r['grup'] == 'anak:c:p']) == 2
