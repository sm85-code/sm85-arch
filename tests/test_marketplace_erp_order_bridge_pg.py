"""Real PostgreSQL locking/atomicity in a disposable schema; CI runs this before merge."""
import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import order_bridge as b
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import BridgeOrder, Pesanan
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.seeder import _self_heal_columns

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="Disposable Postgres required")


@pytest_asyncio.fixture
async def factory():
    url = os.environ["TEST_DATABASE_URL"].replace("postgresql://", "postgresql+asyncpg://", 1)
    schema = "bridge_test_" + uuid.uuid4().hex
    setup = create_async_engine(url)
    async with setup.begin() as c:
        await c.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    async with engine.begin() as c:
        await c.run_sync(MarketplaceErpBase.metadata.create_all)
        await _self_heal_columns(c)
        await _self_heal_columns(c)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with setup.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await setup.dispose()


class FixtureGateway:
    async def shops(self):
        from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters.easyboss import Shop
        return [Shop(platform="lazada", shop_id="fixture-shop", name="Fixture", active=True)]

    async def orders(self, since):
        from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters.easyboss import Order
        return [Order.model_validate({"platform": "lazada", "shop_id": "fixture-shop", "order_id": "fixture-order",
            "complete": True, "paid": True, "status": "ready", "source_status": "fixture_only",
            "buyer": "Fixture", "ordered_at": "2026-11-01T12:00:00+08:00", "deadline": "2026-11-02T12:00:00+08:00",
            "items": [{"item_id": "fixture-item", "product": "Fixture", "quantity": 1}]})]


@pytest.mark.asyncio
async def test_database_lock_survives_commits_and_releases(factory):
    async with factory() as s1, factory() as s2:
        async with b.exclusive(s1):
            await s1.commit()
            with pytest.raises(HTTPException) as exc:
                async with b.exclusive(s2):
                    pass
            assert exc.value.status_code == 409
        async with b.exclusive(s2):
            pass


@pytest.mark.asyncio
async def test_concurrent_import_and_restart_never_duplicate(factory):
    g = FixtureGateway()
    async with factory() as s:
        preview = await b.sync_shops(s, g)
        await b.sync_shops(s, g, preview=False, expected=preview["fingerprint"])
    entered, release = asyncio.Event(), asyncio.Event()

    class Blocking(FixtureGateway):
        async def orders(self, since):
            entered.set()
            await release.wait()
            return await super().orders(since)

    async with factory() as s1, factory() as s2:
        task = asyncio.create_task(b.run_orders(s1, Blocking()))
        await asyncio.wait_for(entered.wait(), 10)
        with pytest.raises(HTTPException):
            await b.run_orders(s2, g)
        release.set()
        assert (await task)["imported"] == 1
    async with factory() as restarted:
        assert (await b.run_orders(restarted, g))["skipped"] == 1
        assert await restarted.scalar(select(func.count()).select_from(Pesanan)) == 1
        assert await restarted.scalar(select(func.count()).select_from(BridgeOrder)) == 1


@pytest.mark.asyncio
async def test_failure_after_flush_rolls_back_order_and_source(factory, monkeypatch):
    g = FixtureGateway()
    async with factory() as s:
        preview = await b.sync_shops(s, g)
        await b.sync_shops(s, g, preview=False, expected=preview["fingerprint"])
        original = b.pesanan_manual.buat

        async def fail(session, payload):
            await original(session, payload)
            raise TimeoutError()

        monkeypatch.setattr(b.pesanan_manual, "buat", fail)
        assert (await b.run_orders(s, g))["failed"] == 1
        assert await s.scalar(select(func.count()).select_from(Pesanan)) == 0
        monkeypatch.setattr(b.pesanan_manual, "buat", original)
        assert (await b.run_orders(s, g, retry=True))["imported"] == 1


@pytest.mark.asyncio
async def test_old_shop_columns_migrate_without_replacing_rows(factory):
    from pathlib import Path
    async with factory() as session:
        await session.execute(text("INSERT INTO mpe_toko_penjualan (id,nama,nama_key,jenis,platform,keterangan,aktif,sumber,sinkron_status,created_at,updated_at) VALUES ('legacy','Web App','web app','internal','webapp','',true,'manual','manual',now(),now())"))
        # Emulate old schema only inside this disposable test schema.
        for column in ("sumber", "easyboss_shop_id", "marketplace_shop_id", "sinkron_status", "sinkron_at"):
            await session.execute(text(f'ALTER TABLE mpe_toko_penjualan DROP COLUMN "{column}" CASCADE'))
        script = Path("scripts/migrations/20261106_order_bridge.sql").read_text()
        statements = [x.strip() for x in script.split(";") if x.strip()]
        # Strip introductory comments before transaction markers.
        statements = ["\n".join(line for line in x.splitlines() if not line.startswith("--")).strip() for x in statements]
        for _ in range(2):
            for statement in statements:
                if statement and statement not in ("BEGIN", "COMMIT"):
                    await session.execute(text(statement))
        row = (await session.execute(text("SELECT id,nama,sumber,sinkron_status FROM mpe_toko_penjualan WHERE id='legacy'"))).one()
        assert tuple(row) == ("legacy", "Web App", "manual", "manual")
        await session.commit()


@pytest.mark.asyncio
async def test_internal_assignments_on_postgres(factory):
    from test_marketplace_erp_internal_assignment import test_mixed_assignments_backfill_scope_and_revoke
    from test_marketplace_erp_pesanan_dashboard import _toko
    async with factory() as session:
        shops = await _toko(session, "Native A"), await _toko(session, "Native B")
        await test_mixed_assignments_backfill_scope_and_revoke(session, shops)
