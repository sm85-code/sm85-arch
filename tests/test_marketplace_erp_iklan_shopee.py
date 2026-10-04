"""Shopee Ads performance per shop (v2.ads.get_all_cpc_ads_daily_performance + get_total_balance)."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase

# Shape from open.shopee.com (dates are DD-MM-YYYY).
HARI = {
    "date": "17-03-2021", "impression": 1000, "clicks": 50, "ctr": 0.05, "direct_order": 4, "broad_order": 6,
    "direct_item_sold": 5, "broad_item_sold": 8, "direct_gmv": 400000, "broad_gmv": 600000, "expense": 100000,
    "direct_roas": 4.0, "broad_roas": 6.0,
}


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _toko(session, nama):
    return await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko=nama))


def test_normalisasi_reads_the_shopee_day_and_survives_bad_input():
    r = erp_shopee.normalisasi_iklan_harian(HARI)
    assert r["tanggal"] == date(2021, 3, 17) and r["expense"] == Decimal(100000) and r["direct_gmv"] == Decimal(400000)
    assert (r["impression"], r["clicks"], r["direct_order"], r["broad_order"]) == (1000, 50, 4, 6)
    assert erp_shopee.normalisasi_iklan_harian({"date": "kemarin"}) is None
    assert erp_shopee.normalisasi_iklan_harian({"date": "01-02-2021"})["expense"] == Decimal(0)  # missing numbers are 0


def test_date_windows_respect_shopees_limits():
    d = date
    assert erp_shopee.potong_rentang_iklan(d(2026, 9, 1), d(2026, 9, 28)) == [(d(2026, 9, 1), d(2026, 9, 28))]  # 28 days: one call
    janela = erp_shopee.potong_rentang_iklan(d(2026, 8, 1), d(2026, 9, 30))
    assert all((b - a).days <= 27 for a, b in janela) and janela[0][0] == d(2026, 8, 1) and janela[-1][1] == d(2026, 9, 30)
    assert all(a != b for a, b in janela)  # Shopee refuses start == end
    assert erp_shopee.potong_rentang_iklan(d(2026, 9, 5), d(2026, 9, 5)) == [(d(2026, 9, 4), d(2026, 9, 5))]  # one day is widened
    # 29 days: the lone last day is widened to the day before, still never longer than a month
    assert erp_shopee.potong_rentang_iklan(d(2026, 9, 1), d(2026, 9, 29))[-1] == (d(2026, 9, 28), d(2026, 9, 29))


@pytest.mark.asyncio
async def test_sync_asks_shopee_in_valid_windows_with_shopees_date_format_and_keeps_only_the_period(monkeypatch):
    monkeypatch.setattr(erp_shopee, "SHOPEE_LIVE_SYNC", True)
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_ID", "1")
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_KEY", "k")
    dipanggil = []

    async def fake(session, akun, path, *, params=None, **_):
        dipanggil.append((path, dict(params or {})))
        if path == erp_shopee._PATH_ADS_SALDO:
            return {"response": {"total_balance": 250000.5, "data_timestamp": 1_790_000_000}}
        # Shopee answers the widened day too: it must be dropped, it is outside the asked period
        return {"response": [{**HARI, "date": params["start_date"]}, {**HARI, "date": params["end_date"], "expense": 7}]}

    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    akun = SimpleNamespace(access_token="at", id_toko_eksternal="5")
    hasil = await erp_shopee.sync_iklan_toko(None, akun, date(2026, 9, 5), date(2026, 9, 5))

    harian = [p for path, p in dipanggil if path == erp_shopee._PATH_ADS_HARIAN]
    assert harian == [{"start_date": "04-09-2026", "end_date": "05-09-2026"}]
    assert [h["tanggal"] for h in hasil["hari"]] == [date(2026, 9, 5)] and hasil["hari"][0]["expense"] == Decimal(7)
    assert hasil["saldo"]["saldo"] == Decimal("250000.5") and hasil["saldo"]["data_at"].year >= 2026


@pytest.mark.asyncio
async def test_saved_days_are_summed_per_shop_with_ratios_balance_and_sorting(session):
    a, b = await _toko(session, "Toko A"), await _toko(session, "Toko B")

    def hari(tgl, biaya, gmv, klik=50, tayang=1000):
        return {**erp_shopee.normalisasi_iklan_harian({**HARI, "date": tgl}), "expense": Decimal(biaya), "direct_gmv": Decimal(gmv), "clicks": klik, "impression": tayang}

    assert await services.simpan_iklan_harian_toko(
        session, a, [hari("01-09-2026", 100, 400), hari("02-09-2026", 200, 200)], {"saldo": Decimal(5000), "data_at": None}
    ) == {"baru": 2, "diperbarui": 0}
    await services.simpan_iklan_harian_toko(session, b, [hari("01-09-2026", 50, 500)])
    assert await services.simpan_iklan_harian_toko(session, a, [hari("01-09-2026", 120, 480)]) == {"baru": 0, "diperbarui": 1}  # refreshed

    ring = await services.ringkasan_iklan_toko(session)
    per_toko = {t["nama_toko"]: t for t in ring["toko"]}
    assert per_toko["Toko A"]["expense"] == Decimal(320) and per_toko["Toko A"]["direct_gmv"] == Decimal(680)
    assert per_toko["Toko A"]["roas_langsung"] == Decimal("2.1250") and per_toko["Toko A"]["ctr"] == Decimal("0.0500")
    assert per_toko["Toko A"]["saldo"] == Decimal(5000) and per_toko["Toko B"]["saldo"] is None
    assert ring["total"]["expense"] == Decimal(370) and ring["total"]["saldo"] == Decimal(5000)
    sempit = await services.ringkasan_iklan_toko(session, dari=date(2026, 9, 2))
    assert [t["nama_toko"] for t in sempit["toko"]] == ["Toko A"] and sempit["toko"][0]["expense"] == Decimal(200)

    semua = await services.list_iklan_harian_toko(session)
    assert semua["total"] == 3 and semua["items"][0]["tanggal"] == date(2026, 9, 2)  # newest first
    assert (await services.list_iklan_harian_toko(session, akun_id=b.id))["total"] == 1
    assert [i["nama_toko"] for i in (await services.list_iklan_harian_toko(session, urut="biaya:desc"))["items"]][:1] == ["Toko A"]
    for kunci in services.KUNCI_URUT_IKLAN_TOKO:  # every column sorts without an error
        await services.list_iklan_harian_toko(session, urut=f"{kunci}:asc")
    # a shop with zero spend has no ROAS (not a division error)
    await services.simpan_iklan_harian_toko(session, b, [hari("05-09-2026", 0, 0)])
    assert (await services.list_iklan_harian_toko(session, akun_id=b.id, urut="roas:desc"))["total"] == 2


@pytest.mark.asyncio
async def test_sinkron_iklan_akun_validates_days_and_uses_the_wib_calendar(session, monkeypatch):
    akun = await _toko(session, "Toko A")
    diminta = []

    async def fake(sesi, a, dari, sampai):
        diminta.append((dari, sampai))
        return {"hari": [erp_shopee.normalisasi_iklan_harian({**HARI, "date": "01-09-2026"})], "saldo": {"saldo": Decimal(9), "data_at": None}}

    monkeypatch.setattr(erp_shopee, "sync_iklan_toko", fake)
    out = await services.sinkron_iklan_akun(session, akun, 30)
    assert out["baru"] == 1 and out["saldo"] == Decimal(9) and (diminta[0][1] - diminta[0][0]).days == 29
    wib_hari_ini = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=7))).date()
    assert diminta[0][1] == wib_hari_ini
    for hari in (0, 181):
        with pytest.raises(HTTPException) as exc:
            await services.sinkron_iklan_akun(session, akun, hari)
        assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_deleting_a_shop_removes_its_ads_rows(session):
    a = await _toko(session, "Toko A")
    await services.simpan_iklan_harian_toko(session, a, [erp_shopee.normalisasi_iklan_harian(HARI)], {"saldo": Decimal(1), "data_at": None})
    await services.delete_akun_marketplace(session, a.id)
    assert (await services.list_iklan_harian_toko(session))["total"] == 0 and (await services.ringkasan_iklan_toko(session))["toko"] == []
