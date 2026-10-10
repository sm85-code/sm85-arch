"""Notifications never mutate orders or buyer read status; every count is scoped."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1 import notification_router as api, commerce_router
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import AkunMarketplace, NotificationRead, Pesanan, StaffAkunMarketplace, UserMarketplaceErp


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        users = [UserMarketplaceErp(id=name, nama=name, username=name, password_hash='test', role=role) for name, role in [('admin', 'admin'), ('staff', 'staff'), ('other', 'staff')]]
        session.add_all(users + [AkunMarketplace(id=k, nama_toko=k, platform='shopee') for k in ['a', 'b']])
        await session.flush()
        session.add(StaffAkunMarketplace(user_id='staff', akun_id='a'))
        for i, shop, status, marketplace in [('p', 'a', 'to_ship', 'READY_TO_SHIP'), ('c', 'a', 'to_ship', 'IN_CANCEL'), ('foreign', 'b', 'to_ship', 'READY_TO_SHIP'), ('done', 'a', 'completed', 'COMPLETED'), ('waiting', 'a', 'to_ship', 'PROCESSED')]:
            session.add(Pesanan(id=i, platform='shopee', id_eksternal='ORDER-'+i, akun_id=shop, status=status, status_marketplace=marketplace))
        await session.commit()
        yield session, {u.id: u for u in users}
    await engine.dispose()


@pytest.mark.asyncio
async def test_scope_priority_no_double_count_and_empty_access(db):
    s, users = db
    result = await api.notifications(1, s, users['staff'])
    assert result['unread'] == 2
    assert [r['id'] for r in result['items']] == ['c', 'p']
    assert result['items'][0]['title'] == 'Permintaan pembatalan'
    assert result['items'][0]['href'] == '/pesanan/c'
    assert (await api.notifications(1, s, users['other']))['items'] == []
    assert (await api.notifications(1, s, users['admin']))['unread'] == 3


@pytest.mark.asyncio
async def test_acknowledgement_private_idempotent_and_does_not_mark_sources(db):
    s, users = db
    body = api.ReadIn(keys=['order:p:process', 'order:foreign:process', 'chat:a:thread:msg1'])
    await api.acknowledge(body, s, users['staff'])
    await api.acknowledge(body, s, users['staff'])
    assert (await api.notifications(1, s, users['staff']))['unread'] == 1
    assert (await api.notifications(1, s, users['admin']))['unread'] == 3
    keys = set((await s.scalars(select(NotificationRead.key))).all())
    assert keys == {'order:p:process', 'chat:a:thread:msg1'}
    assert (await s.get(Pesanan, 'p')).status == 'to_ship'
    assert (await s.get(Pesanan, 'c')).status_marketplace == 'IN_CANCEL'
    assert (await api.read_status(api.ReadIn(keys=['chat:a:thread:msg1', 'chat:a:thread:msg2']), s, users['staff']))['keys'] == ['chat:a:thread:msg1']
    assert (await api.read_status(body, s, users['other']))['keys'] == []


@pytest.mark.asyncio
async def test_all_read_covers_unloaded_pages_but_never_unassigned_orders(db):
    s, users = db
    s.add_all([Pesanan(id=f'extra{i}', platform='shopee', id_eksternal=f'EXTRA{i}', akun_id='a', status='to_ship') for i in range(65)])
    await s.commit()
    result = await api.notifications(1, s, users['staff'])
    assert len(result['items']) == 50 and result['ada_lagi'] and result['unread'] == 67
    await api.acknowledge(api.ReadIn(semua_pesanan=True), s, users['staff'])
    assert (await api.notifications(2, s, users['staff']))['unread'] == 0
    assert 'order:foreign:process' not in (await s.scalars(select(NotificationRead.key))).all()
    order = await s.get(Pesanan, 'c')
    order.status = 'cancelled'
    await s.commit()
    assert all(r['id'] != 'c' for r in (await api.notifications(1, s, users['staff']))['items'])


@pytest.mark.asyncio
async def test_shop_connection_uses_success_watermark_without_returning_tokens(monkeypatch):
    stamp = datetime(2026, 10, 10, tzinfo=timezone.utc)
    rows = [SimpleNamespace(id='a', platform='shopee', status='terhubung', id_toko_eksternal='123', access_token='SECRET', watermark_sinkron_pesanan=stamp, sinkron_penuh_pesanan_at=None)]
    monkeypatch.setattr(commerce_router.services, 'list_akun_marketplace', AsyncMock(return_value=rows))
    result = await commerce_router.connections(None, SimpleNamespace(role='admin'))
    assert result[0]['sinkron_pesanan_at'] == stamp
    assert result[0]['otorisasi_tersedia'] is True
    assert 'SECRET' not in str(result) and 'access_token' not in result[0]
    rows[0].status = 'token_kadaluarsa'
    assert (await commerce_router.connections(None, None))[0]['perlu_otorisasi_ulang'] is True


@pytest.mark.asyncio
async def test_http_permissions_and_bounded_read_payload(db):
    import httpx
    from fastapi import FastAPI
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
    s, users = db
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(commerce_router.router)
    app.dependency_overrides[api.get_db_marketplace_erp] = lambda: s
    app.dependency_overrides[get_current_user_marketplace_erp] = lambda: users['staff']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/notifikasi')).json()['unread'] == 2
        assert (await client.get('/koneksi-toko')).status_code == 403
        assert (await client.post('/notifikasi/dibaca', json={'keys': ['bad-key']})).status_code == 422
        assert (await client.post('/notifikasi/dibaca', json={'keys': ['chat:a:t:m'] * 1001})).status_code == 422
        assert (await client.get('/notifikasi?halaman=0')).status_code == 422
        assert (await client.post('/notifikasi/dibaca', json={'keys': ['order:p:process']})).status_code == 200
        assert (await client.get('/notifikasi')).json()['unread'] == 1
