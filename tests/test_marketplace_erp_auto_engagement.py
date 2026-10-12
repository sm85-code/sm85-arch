"""Focused standing-authorization and duplicate/uncertain-write checks."""
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from tenants.marketplace_erp.modules.marketplace_erp.application import auto_engagement as worker
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import AkunMarketplace, UserMarketplaceErp, EngagementPolicy, EngagementAttempt, KatalogShopee
from tenants.marketplace_erp.adapters.api.v1.engagement_automation_router import SettingsIn, save


@pytest_asyncio.fixture
async def factory(monkeypatch):
    monkeypatch.setenv('SHOPEE_TOKEN_ENCRYPTION_KEYS', Fernet.generate_key().decode())
    monkeypatch.setattr(worker.erp_shopee, 'live_sync_enabled', lambda: True)
    monkeypatch.setattr(worker.erp_shopee, 'pastikan_token_segar', AsyncMock())
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(UserMarketplaceErp(id='admin', username='admin', nama='Admin', role='admin', password_hash='test'))
        session.add(AkunMarketplace(id='shop', platform='shopee', nama_toko='Shop', status='terhubung', access_token='mock-token', id_toko_eksternal='1'))
        session.add(EngagementPolicy(akun_id='shop', replies_enabled=True, replies_enabled_at=worker.now()-timedelta(minutes=1), high_template='Terima kasih', low_template='Mohon hubungi kami', boost_enabled=True, boost_catalog_ids='["product"]', enabled_by='admin'))
        session.add(KatalogShopee(id='product', akun_id='shop', item_id='7', nama='Produk', status='NORMAL'))
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_new_ratings_choose_templates_once_old_and_answered_are_skipped(factory, monkeypatch):
    fresh = {'item_id': '7', 'comment_id': '1', 'rating_star': 5, 'create_time': int(worker.now().timestamp()), 'reply': ''}
    async def read(*args, **kwargs):
        return {'items': [{**fresh, 'comment_id': str(kwargs['comment_id']), 'rating_star': 1 if kwargs['comment_id'] == 2 else 5}]}
    sent = []
    async def send(session, shop, item, payload, before_send):
        await before_send()
        sent.append(payload.comment_list[0].comment)
        return {'ok': True, 'request_id': 'trace'}
    monkeypatch.setattr(worker.engagement, 'comments', read)
    monkeypatch.setattr(worker.engagement, 'reply', send)
    for review in [fresh, fresh, {**fresh, 'comment_id': '2', 'rating_star': 1}, {**fresh, 'comment_id': '3', 'create_time': int((worker.now()-timedelta(days=1)).timestamp())}, {**fresh, 'comment_id': '4', 'reply': 'Sudah'}]:
        await worker.run_review(factory, 'shop', review)
    assert sent == ['Terima kasih', 'Mohon hubungi kami']


@pytest.mark.asyncio
async def test_unknown_boost_is_not_replayed_even_when_schedule_is_due(factory, monkeypatch):
    monkeypatch.setattr(worker.engagement, 'boosted', AsyncMock(return_value={'items': []}))
    async def send(*args, before_send):
        await before_send()
        raise HTTPException(504, {'message': 'Timeout', 'diagnostic': {'request_id': 'trace'}})
    mocked = AsyncMock(side_effect=send)
    monkeypatch.setattr(worker.engagement, 'boost', mocked)
    await worker.run_boost(factory, 'shop')
    async with factory() as session:
        rule = await session.get(EngagementPolicy, 'shop')
        rule.next_boost_at = worker.now()-timedelta(minutes=1)
        await session.commit()
    await worker.run_boost(factory, 'shop')
    assert mocked.await_count == 1
    async with factory() as session:
        rows = (await session.execute(worker.select(EngagementAttempt))).scalars().all()
        assert len(rows) == 1 and rows[0].status == 'unknown'


@pytest.mark.asyncio
async def test_revoked_policy_and_cooldown_prevent_send(factory, monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(worker.engagement, 'boost', send)
    monkeypatch.setattr(worker.engagement, 'boosted', AsyncMock(return_value={'items': [{'item_id': '7', 'cool_down_second': 600}]}))
    await worker.run_boost(factory, 'shop')
    send.assert_not_awaited()
    async with factory() as session:
        rule = await session.get(EngagementPolicy, 'shop')
        assert worker.utc(rule.next_boost_at) > worker.now()
        rule.replies_enabled = False
        await session.commit()
    await worker.run_review(factory, 'shop', {'item_id': '7', 'comment_id': '1', 'rating_star': 5, 'create_time': int(worker.now().timestamp()), 'reply': ''})
    async with factory() as session:
        assert await session.get(EngagementAttempt, ('shop', 'reply:1')) is None


@pytest.mark.asyncio
async def test_settings_reject_incomplete_templates_and_foreign_product(factory):
    with pytest.raises(ValidationError):
        SettingsIn(replies_enabled=True, high_template=' ', low_template='x')
    async with factory() as session:
        user = await session.get(UserMarketplaceErp, 'admin')
        with pytest.raises(HTTPException) as error:
            await save('shop', SettingsIn(boost_enabled=True, boost_catalog_ids=['foreign']), session, user)
        assert error.value.status_code == 422
        await session.rollback()
        user = await session.get(UserMarketplaceErp, 'admin')
        await save('shop', SettingsIn(replies_enabled=True, high_template='Terima kasih', low_template='Hubungi kami'), session, user)
        policy = await session.get(EngagementPolicy, 'shop')
        assert policy.replies_enabled and json.loads(policy.boost_catalog_ids) == []
