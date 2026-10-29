"""Guarded ads writes. Shopee is mocked; a write action must not run unless the guard says so."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio

from tenants.marketplace_erp.modules.marketplace_erp.application import agen_aksi, agen_guardrail, peta_akses
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import shopee_iklan

PENGATURAN = {
    "response": {"campaign_list": [{
        "campaign_id": 7,
        "common_info": {"campaign_budget": 50000, "campaign_status": "ongoing", "ad_name": "Kursi"},
        "auto_bidding_info": {"roas_target": 4},
    }]}
}
ORANG = SimpleNamespace(id="user-1", username="owner", nama="Owner")


def _pasang(monkeypatch, calls):
    state = {"budget": 50000, "status": "ongoing", "roas": 4}

    async def fake(session, akun, aksi, *, params=None, body=None):
        calls.append((aksi, body))
        if aksi == "pengaturan_kampanye":
            return {"response": {"campaign_list": [{
                "campaign_id": 7,
                "common_info": {"campaign_budget": state["budget"], "campaign_status": state["status"], "ad_name": "Kursi"},
                "auto_bidding_info": {"roas_target": state["roas"]},
            }]}}
        if aksi == "ubah_manual":
            if body["edit_action"] == "change_budget":
                state["budget"] = body["budget"]
            elif body["edit_action"] == "change_roas_target":
                state["roas"] = body["roas_target"]
            elif body["edit_action"] == "pause":
                state["status"] = "paused"
            elif body["edit_action"] == "resume":
                state["status"] = "ongoing"
            return {"response": [{"campaign_id": body["campaign_id"]}]}
        raise AssertionError(aksi)

    monkeypatch.setattr(shopee_iklan, "panggil", fake)


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


def test_percent_cap_is_inclusive_at_the_limit():
    assert agen_aksi.persen_ubah(Decimal("50000"), Decimal("60000")) == 20
    assert peta_akses.cari("/api/marketplace-erp/akun/ABC/iklan/agen/aksi", "POST") == ("iklan", "tulis")


@pytest.mark.asyncio
async def test_dry_run_reads_but_does_not_write(sesi, monkeypatch):
    calls = []
    _pasang(monkeypatch, calls)
    akun = SimpleNamespace(id="toko-1", platform="shopee", nama_toko="Toko A")
    hasil = await agen_aksi.terapkan(
        sesi, akun, agen_aksi.AksiIn(jenis="anggaran", campaign_id=7, nilai=Decimal("55000"), alasan="coba"), ORANG,
    )
    assert hasil["kering"] is True and hasil["diterapkan"] is False and hasil["butuh_persetujuan"] is True
    assert hasil["sebelum"]["anggaran"] == 50000 and hasil["sesudah"]["anggaran"] == 55000
    assert [aksi for aksi, _ in calls] == ["pengaturan_kampanye"]


@pytest.mark.asyncio
async def test_real_write_needs_approval_and_stays_inside_caps(sesi, monkeypatch):
    from fastapi import HTTPException

    calls = []
    _pasang(monkeypatch, calls)
    akun = SimpleNamespace(id="toko-1", platform="shopee", nama_toko="Toko A")
    with pytest.raises(HTTPException) as tolak:
        await agen_aksi.terapkan(
            sesi, akun, agen_aksi.AksiIn(jenis="anggaran", campaign_id=7, nilai=Decimal("55000"), kering=False), ORANG,
        )
    assert tolak.value.status_code == 409
    assert [aksi for aksi, _ in calls] == ["pengaturan_kampanye"]

    calls.clear()
    hasil = await agen_aksi.terapkan(
        sesi, akun,
        agen_aksi.AksiIn(jenis="anggaran", campaign_id=7, nilai=Decimal("60000"), kering=False, persetujuan="ya",
                         alasan="naik 20", request_id="req-aksi"),
        ORANG,
    )
    assert hasil["diterapkan"] is True
    assert calls[-1][0] == "ubah_manual"
    assert calls[-1][1]["edit_action"] == "change_budget" and calls[-1][1]["budget"] == 60000
    ulang = await agen_aksi.terapkan(
        sesi, akun, agen_aksi.AksiIn(jenis="anggaran", campaign_id=7, nilai=Decimal("1000"), kering=False,
                                    persetujuan="ya", request_id="req-aksi"), ORANG,
    )
    assert ulang["diulang"] is True and ulang["sesudah"]["anggaran"] == 60000
    assert [aksi for aksi, _ in calls].count("ubah_manual") == 1

    calls.clear()
    with pytest.raises(HTTPException) as besar:
        await agen_aksi.terapkan(
            sesi, akun, agen_aksi.AksiIn(jenis="anggaran", campaign_id=7, nilai=Decimal("73000"), kering=False, persetujuan="ya"),
            ORANG,
        )
    assert besar.value.status_code == 422
    assert calls == [("pengaturan_kampanye", None)] or calls[0][0] == "pengaturan_kampanye"
    assert all(aksi != "ubah_manual" for aksi, _ in calls)


@pytest.mark.asyncio
async def test_autonomy_writes_within_caps_and_stop_blocks_everything(sesi, monkeypatch):
    from fastapi import HTTPException

    calls = []
    _pasang(monkeypatch, calls)
    akun = SimpleNamespace(id="toko-1", platform="shopee", nama_toko="Toko A")
    await agen_guardrail.simpan_batas(
        sesi, "toko-1", agen_guardrail.BatasIn(otonomi=True, anggaran_harian_maks=Decimal("58000"), min_hari_antar_perubahan=1), ORANG,
    )
    await sesi.commit()
    with pytest.raises(HTTPException) as cap:
        await agen_aksi.terapkan(
            sesi, akun, agen_aksi.AksiIn(jenis="anggaran", campaign_id=7, nilai=Decimal("59000"), kering=False), ORANG,
        )
    assert cap.value.status_code == 422
    calls.clear()
    jeda = await agen_aksi.terapkan(sesi, akun, agen_aksi.AksiIn(jenis="jeda", campaign_id=7, kering=False), ORANG)
    assert jeda["diterapkan"] is True and calls[-1][1]["edit_action"] == "pause"
    calls.clear()
    pertama = await agen_aksi.terapkan(
        sesi, akun, agen_aksi.AksiIn(jenis="anggaran", campaign_id=7, nilai=Decimal("56000"), kering=False, request_id="naik"),
        ORANG,
    )
    assert pertama["diterapkan"] is True
    calls.clear()
    with pytest.raises(HTTPException) as jeda_hari:
        await agen_aksi.terapkan(
            sesi, akun, agen_aksi.AksiIn(jenis="anggaran", campaign_id=7, nilai=Decimal("57000"), kering=False, request_id="naik-2"),
            ORANG,
        )
    assert jeda_hari.value.status_code == 409 and all(a != "ubah_manual" for a, _ in calls)

    await agen_guardrail.simpan_stop(sesi, None, agen_guardrail.StopIn(aktif=True, alasan="berhenti"), ORANG)
    await sesi.commit()
    calls.clear()
    with pytest.raises(HTTPException) as stop:
        await agen_aksi.terapkan(
            sesi, akun, agen_aksi.AksiIn(jenis="lanjut", campaign_id=7, kering=False, persetujuan="ya"), ORANG,
        )
    assert stop.value.status_code == 409 and calls == []


@pytest.mark.asyncio
async def test_rollback_restores_the_audited_budget_and_needs_approval(sesi, monkeypatch):
    from fastapi import HTTPException

    calls = []
    _pasang(monkeypatch, calls)
    akun = SimpleNamespace(id="toko-1", platform="shopee", nama_toko="Toko A")
    hasil = await agen_aksi.terapkan(
        sesi, akun,
        agen_aksi.AksiIn(jenis="anggaran", campaign_id=7, nilai=Decimal("55000"), kering=False, persetujuan="ya", request_id="ubah"),
        ORANG,
    )
    with pytest.raises(HTTPException) as belum:
        await agen_aksi.kembalikan(sesi, akun, hasil["audit_id"], agen_aksi.KembaliIn(kering=False), ORANG)
    assert belum.value.status_code == 409
    calls.clear()
    balik = await agen_aksi.kembalikan(
        sesi, akun, hasil["audit_id"], agen_aksi.KembaliIn(kering=False, persetujuan="ya", alasan="salah", request_id="balik"), ORANG,
    )
    assert balik["diterapkan"] is True and balik["jenis"] == "kembalikan"
    assert calls[-1][1]["edit_action"] == "change_budget" and calls[-1][1]["budget"] == 50000
