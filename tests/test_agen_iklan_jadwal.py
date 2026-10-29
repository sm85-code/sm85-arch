"""Dayparting is off until a row and the loop are both enabled. Shopee writes are mocked."""
from datetime import datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError

from tenants.marketplace_erp.modules.marketplace_erp.application import agen_guardrail, agen_jadwal, peta_akses
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import shopee_iklan

ORANG = SimpleNamespace(id="user-1", username="owner", nama="Owner")


@pytest_asyncio.fixture
async def sesi():
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import AkunMarketplace

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        s.add(AkunMarketplace(id="toko-1", platform="shopee", nama_toko="Toko A"))
        await s.commit()
        yield s
    await engine.dispose()


def _pasang(monkeypatch, calls):
    async def fake(session, akun, aksi, *, params=None, body=None):
        calls.append((aksi, body))
        if aksi == "pengaturan_kampanye":
            return {"response": {"campaign_list": [{
                "campaign_id": 7,
                "common_info": {"campaign_budget": 50000, "campaign_status": "ongoing"},
                "auto_bidding_info": {},
                "manual_bidding_info": {"selected_keywords": []},
            }]}}
        if aksi == "ubah_manual":
            return {"response": [{"campaign_id": body["campaign_id"]}]}
        raise AssertionError(aksi)

    monkeypatch.setattr(shopee_iklan, "panggil", fake)


def test_the_loop_is_off_by_default_and_the_route_is_an_ads_write(monkeypatch):
    monkeypatch.delenv("AGEN_IKLAN_JADWAL_MENIT", raising=False)
    assert agen_jadwal.interval_seconds() == 0
    monkeypatch.setenv("AGEN_IKLAN_JADWAL_MENIT", "bukan")
    assert agen_jadwal.interval_seconds() == 0
    monkeypatch.setenv("AGEN_IKLAN_JADWAL_MENIT", "15")
    assert agen_jadwal.interval_seconds() == 900
    assert peta_akses.cari("/api/marketplace-erp/akun/ABC/iklan/agen/jadwal", "PUT") == ("iklan", "tulis")
    with pytest.raises(ValidationError):
        agen_jadwal.JadwalIn(campaign_id=7, jam=[])


@pytest.mark.asyncio
async def test_only_an_enabled_row_pauses_outside_its_hours_and_resumes_inside(sesi, monkeypatch):
    calls = []
    _pasang(monkeypatch, calls)
    mati = await agen_jadwal.simpan(sesi, "toko-1", agen_jadwal.JadwalIn(campaign_id=7, jam=[10, 11]))
    assert mati["aktif"] is False
    await sesi.commit()
    assert await agen_jadwal.sekali(sesi, datetime(2026, 10, 29, 9, tzinfo=agen_jadwal.WIB)) == []
    assert calls == []
    with pytest.raises(HTTPException):
        await agen_jadwal.simpan(sesi, "toko-1", agen_jadwal.JadwalIn(campaign_id=7, jam=[10, 24], aktif=True))
    nyala = await agen_jadwal.simpan(sesi, "toko-1", agen_jadwal.JadwalIn(campaign_id=7, jam=[10, 11], aktif=True))
    assert nyala["aktif"] is True and nyala["jam"] == [10, 11]
    await sesi.commit()
    luar = await agen_jadwal.sekali(sesi, datetime(2026, 10, 29, 9, 5, tzinfo=agen_jadwal.WIB))
    assert luar == [{"campaign_id": "7", "jenis": "jeda", "gagal": None}]
    assert calls[-1][1]["edit_action"] == "pause"
    calls.clear()
    assert await agen_jadwal.sekali(sesi, datetime(2026, 10, 29, 9, 6, tzinfo=agen_jadwal.WIB)) == []
    dalam = await agen_jadwal.sekali(sesi, datetime(2026, 10, 29, 10, 1, tzinfo=agen_jadwal.WIB))
    assert dalam[0]["jenis"] == "lanjut" and calls[-1][1]["edit_action"] == "resume"


@pytest.mark.asyncio
async def test_stop_blocks_the_scheduler_before_a_shopee_write(sesi, monkeypatch):
    calls = []
    _pasang(monkeypatch, calls)
    await agen_jadwal.simpan(sesi, "toko-1", agen_jadwal.JadwalIn(campaign_id=7, jam=[10], aktif=True))
    await agen_guardrail.simpan_stop(sesi, None, agen_guardrail.StopIn(aktif=True, alasan="berhenti"), ORANG)
    await sesi.commit()
    hasil = await agen_jadwal.sekali(sesi, datetime(2026, 10, 29, 9, tzinfo=agen_jadwal.WIB))
    assert hasil[0]["gagal"] and calls == []
