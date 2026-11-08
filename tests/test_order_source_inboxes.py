"""Inbox split preserves order identity and enforces independent page and shop access."""
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import select

from tenants.marketplace_erp.modules.marketplace_erp.application import akses, services
from tenants.marketplace_erp.modules.marketplace_erp.application.order_source import source_condition
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import (
    get_current_user_marketplace_erp, require_roles_marketplace_erp,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    AkunMarketplace, BridgeOrder, Pesanan, PesananTokoPenjualan, Role, RoleHalaman, TokoPenjualan,
)


async def seed(s):
    s.add(AkunMarketplace(id='native', platform='shopee', nama_toko='Shopee'))
    for platform in ['lazada', 'tiktokshop']:
        s.add(TokoPenjualan(id=platform, jenis='internal', platform=platform, nama=platform,
                           nama_key=platform, sumber='easyboss', easyboss_shop_id=platform, aktif=True))
    await s.flush()
    s.add(Pesanan(id='shopee', platform='shopee', akun_id='native', id_eksternal='S', status='to_ship'))
    s.add(Pesanan(id='manual', platform='manual', id_eksternal='M', status='to_ship'))
    for platform in ['lazada', 'tiktokshop']:
        s.add(Pesanan(id=platform, platform='manual', id_eksternal=platform, status='to_ship'))
        s.add(BridgeOrder(platform=platform, shop_id=platform, toko_id=platform,
                          order_id=platform, pesanan_id=platform, status='imported', source_status='ready_to_ship' if platform == 'lazada' else 'AWAITING_COLLECTION', snapshot_json='{}'))
        s.add(PesananTokoPenjualan(pesanan_id=platform, toko_id=platform))
    await s.commit()


@pytest.mark.asyncio
async def test_lists_counts_shops_and_assignments_have_the_same_source_boundary(sesi_peran):
    s = sesi_peran
    await seed(s)
    before = set((await s.scalars(select(Pesanan.id))).all())
    for source, ids, shops in [('shopee', {'shopee'}, {'native'}),
                               ('easyboss', {'lazada', 'tiktokshop'}, {'internal:lazada', 'internal:tiktokshop'})]:
        page = await services.daftar_pesanan(s, sumber=source)
        summary = await services.ringkasan_pesanan(s, sumber=source)
        assert {p.id for p in page['items']} == ids
        assert page['total'] == summary['tahap']['semua'] == len(ids)
        assert {r['akun_id'] for r in summary['toko']} == shops
    scoped = await services.daftar_pesanan(s, sumber='easyboss', akun_diizinkan={'internal:lazada'})
    assert [p.id for p in scoped['items']] == ['lazada']
    assert (await services.daftar_pesanan(s, sumber='easyboss', akun_diizinkan=set()))['total'] == 0
    assert not (await services.daftar_pesanan(s, sumber='shopee', akun_id='internal:lazada'))['items']
    assert set((await s.scalars(select(Pesanan.id))).all()) == before
    with pytest.raises(Exception):
        source_condition('all')


@pytest.mark.asyncio
async def test_real_guard_checks_source_for_single_and_mixed_bulk_requests(sesi_peran):
    s = sesi_peran
    await seed(s)
    s.add(Role(id='eb', nama='EasyBoss only', akses_toko='semua'))
    s.add(RoleHalaman(role_id='eb', halaman='pesanan_easyboss', tingkat='baca'))
    await s.commit()
    app = FastAPI()
    guard = Depends(require_roles_marketplace_erp('staff'))

    @app.get('/api/marketplace-erp/pesanan/{pesanan_id}', dependencies=[guard])
    async def detail(pesanan_id: str):
        return {'id': pesanan_id}

    @app.post('/api/marketplace-erp/pesanan/resi-gabungan', dependencies=[guard])
    async def bulk():
        return {'ok': True}

    async def db():
        yield s

    async def user():
        return SimpleNamespace(id='u', role='eb')

    app.dependency_overrides[get_db_marketplace_erp] = db
    app.dependency_overrides[get_current_user_marketplace_erp] = user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        assert (await c.get('/api/marketplace-erp/pesanan/lazada')).status_code == 200
        assert (await c.get('/api/marketplace-erp/pesanan/shopee')).status_code == 403
        assert (await c.post('/api/marketplace-erp/pesanan/resi-gabungan', json={'pesanan_ids': ['lazada']})).status_code == 403
        (await s.get(RoleHalaman, ('eb', 'pesanan_easyboss'))).tingkat = 'tulis'
        await s.commit()
        assert (await c.post('/api/marketplace-erp/pesanan/resi-gabungan', json={'pesanan_ids': ['lazada']})).status_code == 200
        assert (await c.post('/api/marketplace-erp/pesanan/resi-gabungan', json={'pesanan_ids': ['lazada', 'shopee']})).status_code == 403


@pytest.mark.asyncio
async def test_role_split_copies_existing_rights_once_and_preserves_revocation(sesi_peran):
    s = sesi_peran
    row = await s.get(RoleHalaman, ('owner', 'pesanan_easyboss'))
    assert row.tingkat == 'tulis'
    await s.delete(row)
    await akses.pastikan_bawaan(s)
    await s.flush()
    assert await s.get(RoleHalaman, ('owner', 'pesanan_easyboss')) is None


@pytest.mark.asyncio
async def test_http_inboxes_ignore_spoofed_source_query_and_use_separate_permissions(sesi_peran):
    from tenants.marketplace_erp.adapters.api.v1.marketplace_erp_router import marketplace_erp_router
    s = sesi_peran
    await seed(s)
    s.add(Role(id='eb-reader', nama='EB Reader', akses_toko='semua'))
    s.add(RoleHalaman(role_id='eb-reader', halaman='pesanan_easyboss', tingkat='baca'))
    await s.commit()
    app = FastAPI()
    app.include_router(marketplace_erp_router, prefix='/api/marketplace-erp')
    who = SimpleNamespace(id='u', role='admin')

    async def db():
        yield s

    async def user():
        return who

    app.dependency_overrides[get_db_marketplace_erp] = db
    app.dependency_overrides[get_current_user_marketplace_erp] = user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        for path, ids in [('pesanan', {'shopee'}), ('pesanan-easyboss', {'lazada', 'tiktokshop'})]:
            response = await c.get(f'/api/marketplace-erp/{path}/daftar?sumber=all')
            assert response.status_code == 200, response.text
            assert {p['id'] for p in response.json()['items']} == ids
            summary = await c.get(f'/api/marketplace-erp/{path}/ringkasan')
            assert summary.json()['tahap']['semua'] == len(ids)
        who.role = 'eb-reader'
        assert (await c.get('/api/marketplace-erp/pesanan-easyboss/daftar')).status_code == 200
        assert (await c.get('/api/marketplace-erp/pesanan/daftar?sumber=easyboss')).status_code == 403
