"""Incremental order sync: only ask Shopee for what changed since the last successful sync."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase

NOW = 1_800_000_000
PENUH = NOW - erp_shopee._ORDER_WINDOW_SECONDS


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


# --- the time window --------------------------------------------------------------------


def test_window_is_the_whole_15_days_without_a_watermark():
    assert erp_shopee.jendela_sinkron_pesanan(None, NOW) == (PENUH, NOW)


def test_window_starts_at_the_watermark_minus_the_overlap():
    dari = datetime.fromtimestamp(NOW - 300, tz=timezone.utc)  # synced 5 minutes ago
    assert erp_shopee.jendela_sinkron_pesanan(dari, NOW) == (NOW - 300 - 600, NOW)


def test_window_never_reaches_further_back_than_shopee_allows():
    assert erp_shopee.jendela_sinkron_pesanan(datetime.fromtimestamp(NOW - 40 * 86400, tz=timezone.utc), NOW)[0] == PENUH


def test_naive_watermark_is_read_as_utc():
    naif = datetime.fromtimestamp(NOW - 300, tz=timezone.utc).replace(tzinfo=None)
    assert erp_shopee.jendela_sinkron_pesanan(naif, NOW)[0] == NOW - 900


@pytest.mark.asyncio
async def test_sync_pesanan_sends_the_narrow_window_to_shopee(monkeypatch):
    monkeypatch.setattr(erp_shopee, "SHOPEE_LIVE_SYNC", True)
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_ID", "1")
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_KEY", "k")
    asked = []

    async def fake(session, akun, path, *, params=None, **_):
        asked.append((path, dict(params)))
        return {"response": {"more": False, "order_list": []}}

    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    dari = datetime.now(timezone.utc) - timedelta(minutes=5)
    rows = await erp_shopee.sync_pesanan(None, SimpleNamespace(access_token="at", id_toko_eksternal="5"), dari=dari)

    assert rows == [] and len(asked) == 1  # nothing changed: ONE call, no detail calls at all
    params = asked[0][1]
    assert params["time_to"] - params["time_from"] < 20 * 60  # ~15 minutes instead of 15 days


# --- when the service reads incrementally vs fully ----------------------------------------


async def _toko(session):
    return await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="TES"))


def _fake_adapter(monkeypatch, gagal=False):
    dipanggil = []

    async def fake(session, akun, lewati_resi=frozenset(), dari=None):
        dipanggil.append(dari)
        if gagal:
            raise HTTPException(status_code=502, detail="Shopee gagal")
        return []

    monkeypatch.setattr(erp_shopee, "sync_pesanan", fake)
    return dipanggil


@pytest.mark.asyncio
async def test_first_sync_is_full_then_incremental(session, monkeypatch):
    akun, dipanggil = await _toko(session), _fake_adapter(monkeypatch)

    h1 = await services.sinkron_pesanan_akun(session, akun)
    assert h1["penuh"] is True and dipanggil[-1] is None
    assert akun.watermark_sinkron_pesanan is not None and akun.sinkron_penuh_pesanan_at is not None
    watermark = akun.watermark_sinkron_pesanan

    h2 = await services.sinkron_pesanan_akun(session, akun)
    assert h2["penuh"] is False and dipanggil[-1] == watermark  # asks only for changes since the last sync
    assert akun.watermark_sinkron_pesanan >= watermark


@pytest.mark.asyncio
async def test_full_read_returns_after_six_hours_and_on_demand(session, monkeypatch):
    akun, dipanggil = await _toko(session), _fake_adapter(monkeypatch)
    await services.sinkron_pesanan_akun(session, akun)

    akun.sinkron_penuh_pesanan_at = datetime.now(timezone.utc) - timedelta(hours=7)
    assert (await services.sinkron_pesanan_akun(session, akun))["penuh"] is True  # safety-net full read
    assert dipanggil[-1] is None
    assert (await services.sinkron_pesanan_akun(session, akun))["penuh"] is False  # and incremental again

    assert (await services.sinkron_pesanan_akun(session, akun, penuh=True))["penuh"] is True  # manual click
    assert dipanggil[-1] is None


@pytest.mark.asyncio
async def test_a_failed_sync_does_not_move_the_watermark(session, monkeypatch):
    akun, dipanggil = await _toko(session), _fake_adapter(monkeypatch)
    await services.sinkron_pesanan_akun(session, akun)
    watermark, penuh_at = akun.watermark_sinkron_pesanan, akun.sinkron_penuh_pesanan_at

    _fake_adapter(monkeypatch, gagal=True)
    with pytest.raises(HTTPException):
        await services.sinkron_pesanan_akun(session, akun)
    assert (akun.watermark_sinkron_pesanan, akun.sinkron_penuh_pesanan_at) == (watermark, penuh_at)  # retried next time
    assert len(dipanggil) == 1


@pytest.mark.asyncio
async def test_automatic_sync_of_all_shops_is_incremental(session, monkeypatch):
    akun, dipanggil = await _toko(session), _fake_adapter(monkeypatch)
    akun.access_token, akun.id_toko_eksternal = "at", "5"
    await services.sinkron_semua_pesanan(session, [akun], jeda_detik=0)
    await services.sinkron_semua_pesanan(session, [akun], jeda_detik=0)
    assert dipanggil[0] is None and dipanggil[1] is not None  # second round only asks for the delta
