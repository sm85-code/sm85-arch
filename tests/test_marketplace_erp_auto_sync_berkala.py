"""Background (periodic) order sync."""
import asyncio

import pytest

from tenants.marketplace_erp.modules.marketplace_erp.application import auto_sync, services
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee


def test_interval_default_custom_and_off(monkeypatch):
    monkeypatch.delenv("SHOPEE_AUTO_SYNC_MINUTES", raising=False)
    assert auto_sync.interval_seconds() == 300
    monkeypatch.setenv("SHOPEE_AUTO_SYNC_MINUTES", "2")
    assert auto_sync.interval_seconds() == 120
    monkeypatch.setenv("SHOPEE_AUTO_SYNC_MINUTES", "0")
    assert auto_sync.interval_seconds() == 0
    monkeypatch.setenv("SHOPEE_AUTO_SYNC_MINUTES", "abc")
    assert auto_sync.interval_seconds() == 0


@pytest.mark.asyncio
async def test_round_does_nothing_when_live_sync_is_off(monkeypatch):
    monkeypatch.setattr(erp_shopee, "live_sync_enabled", lambda: False)
    assert await auto_sync.sinkron_semua_toko(None) == []


@pytest.mark.asyncio
async def test_round_syncs_only_connected_shopee_shops(monkeypatch):
    monkeypatch.setattr(erp_shopee, "live_sync_enabled", lambda: True)
    from types import SimpleNamespace as NS

    async def fake_list(session, platform=None):
        return [NS(access_token="t", id_toko_eksternal="1", id="a"), NS(access_token=None, id_toko_eksternal="2", id="b")]

    seen = []

    async def fake_sync(session, akun_list, **kw):
        seen.append(([a.id for a in akun_list], kw))
        return []

    monkeypatch.setattr(services, "list_akun_marketplace", fake_list)
    monkeypatch.setattr(services, "sinkron_semua_pesanan", fake_sync)
    await auto_sync.sinkron_semua_toko(None)
    assert seen[0][0] == ["a"] and seen[0][1]["batas_detik"] == auto_sync._ROUND_BUDGET_SECONDS


@pytest.mark.asyncio
async def test_loop_survives_a_failing_round(monkeypatch):
    rounds = []

    async def boom(session):
        rounds.append(1)
        raise RuntimeError("x")

    class Ctx:
        async def __aenter__(self): return None
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(auto_sync, "sinkron_semua_toko", boom)
    task = asyncio.create_task(auto_sync.jalankan_berkala(lambda: Ctx(), 0.01))
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(rounds) >= 2  # kept going after the first failure
