"""AI ads advisor: what is sent to the model, what is kept from its answer, quota and cost bookkeeping."""
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import ads_ai
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase


def kamp(id_, status="ongoing", bidding="manual", biaya=90000, **o):
    return {
        "campaign_id": id_, "nama": f"Kampanye {id_}", "status": status, "bidding": bidding, "anggaran": o.get("anggaran", 50000),
        "roas_target": 4 if bidding == "auto" else None,
        "kata_kunci": [{"kata": "kursi", "tipe": "broad", "bid": 450}] if bidding == "manual" else [],
        "kinerja": {"expense": biaya, "impression": 8000, "clicks": 120, "ctr": 0.015, "direct_order": o.get("pesanan", 0), "direct_gmv": o.get("gmv", 0), "roas": o.get("roas", 0)},
    }


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


def test_only_running_and_paused_campaigns_are_sent_biggest_spender_first_and_capped():
    daftar = [kamp("1", biaya=10), kamp("2", biaya=500), kamp("3", status="ended"), kamp("4", status="paused", biaya=100)]
    assert [k["campaign_id"] for k in ads_ai.ringkas_kampanye(daftar)] == ["2", "4", "1"]
    banyak = [kamp(str(i), biaya=i) for i in range(100)]
    assert len(ads_ai.ringkas_kampanye(banyak)) == ads_ai.KAMPANYE_MAKS
    assert ads_ai.ringkas_kampanye([kamp("1")])[0]["kata_kunci"] == [{"kata": "kursi", "tipe": "broad", "bid": 450.0}]


def test_suggestions_for_unknown_campaigns_or_illegal_actions_are_dropped():
    ringkas = ads_ai.ringkas_kampanye([kamp("1"), kamp("2", status="paused"), kamp("3", bidding="auto")])

    def s(**o):
        return {"campaign_id": "1", "tindakan": "pause", "nilai": None, "kata": None, "prioritas": "tinggi", "alasan": "120 klik tanpa pesanan", **o}

    baik = [s(), s(campaign_id="2", tindakan="resume"), s(tindakan="change_budget", nilai=80000), s(campaign_id="3", tindakan="change_roas_target", nilai=6),
            s(tindakan="ubah_bid", nilai=300, kata="kursi"), s(tindakan="hapus_kata_kunci", kata="kursi"), s(tindakan="perhatikan")]
    buruk = [s(campaign_id="999"), s(campaign_id="2"), s(campaign_id="1", tindakan="resume"), s(tindakan="change_budget", nilai=500000),
             s(tindakan="change_budget", nilai=0), s(tindakan="change_roas_target", nilai=6), s(tindakan="ubah_bid", nilai=300, kata="tidak-ada"),
             s(campaign_id="3", tindakan="hapus_kata_kunci", kata="kursi"), s(alasan=" "), s(tindakan="delete")]
    hasil = ads_ai.validasi_saran({"saran": baik + buruk}, ringkas)
    assert [(x["campaign_id"], x["tindakan"]) for x in hasil] == [("1", "pause"), ("2", "resume"), ("1", "change_budget"), ("3", "change_roas_target"), ("1", "ubah_bid"), ("1", "hapus_kata_kunci"), ("1", "perhatikan")]
    assert hasil[0]["nilai"] is None and hasil[2]["nilai"] == 80000 and hasil[4]["kata"] == "kursi"


def test_cost_follows_the_model_price():
    assert ads_ai.biaya_usd("claude-opus-5-5", 5000, 4000) == Decimal("0.1")
    assert ads_ai.biaya_usd("claude-sonnet-5-5", 5000, 4000) == Decimal("0.05")


@pytest.mark.asyncio
async def test_without_an_api_key_the_advisor_says_so(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ads_ai.AiTidakTersedia, match="ANTHROPIC_API_KEY"):
        await ads_ai.minta_saran("x")


@pytest.mark.asyncio
async def test_the_model_call_asks_for_schema_json_and_reads_usage(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    dikirim = {}

    class Pesan:
        async def create(self, **kw):
            dikirim.update(kw)
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps({"ringkasan": "ok", "saran": []}))], usage=SimpleNamespace(input_tokens=5000, output_tokens=900))

    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kw: SimpleNamespace(messages=Pesan()))
    h = await ads_ai.minta_saran("data")
    assert h["mentah"]["ringkasan"] == "ok" and (h["token_masuk"], h["token_keluar"]) == (5000, 900)
    assert dikirim["model"] == "claude-opus-5-5" and dikirim["output_config"]["format"]["type"] == "json_schema" and "thinking" not in dikirim


@pytest.mark.asyncio
async def test_a_run_is_recorded_with_cost_and_the_daily_quota_blocks_the_next(session, monkeypatch):
    akun = SimpleNamespace(id=None, nama_toko="AZFA", platform="shopee")

    async def fake_daftar(sess, a, hari):
        return {"saldo": 1000, "hari": 7, "catatan": [], "kampanye": [kamp("1")]}

    async def fake_minta(pesan, model=None):
        assert "AZFA" in pesan and '"campaign_id": "1"' in pesan
        return {"mentah": {"ringkasan": "Boros.", "saran": [{"campaign_id": "1", "tindakan": "pause", "nilai": None, "kata": None, "prioritas": "tinggi", "alasan": "120 klik, 0 pesanan"}]}, "token_masuk": 5000, "token_keluar": 4000, "model": "claude-opus-5-5"}

    monkeypatch.setattr(services, "daftar_kampanye_iklan", fake_daftar)
    monkeypatch.setattr(ads_ai, "minta_saran", fake_minta)
    monkeypatch.setattr(services, "ADS_AI_BATAS_HARIAN", 2)
    h = await services.saran_ai_iklan(session, akun, SimpleNamespace(id="u1", username="budi"), 7)
    assert h["saran"][0]["tindakan"] == "pause" and h["pemakaian"]["biaya_rp"] == 1600 and h["kuota_sisa"] == 1 and h["bulan_ini_usd"] == pytest.approx(0.1)
    await services.saran_ai_iklan(session, akun, SimpleNamespace(id="u1", username="budi"), 7)
    with pytest.raises(HTTPException) as exc:
        await services.saran_ai_iklan(session, akun, SimpleNamespace(id="u1", username="budi"), 7)
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_no_running_campaign_means_no_model_call_and_no_cost(session, monkeypatch):
    async def fake_daftar(sess, a, hari):
        return {"saldo": 0, "hari": 7, "catatan": [], "kampanye": [kamp("1", status="ended")]}

    async def terlarang(*a, **k):
        raise AssertionError("must not call the model")

    monkeypatch.setattr(services, "daftar_kampanye_iklan", fake_daftar)
    monkeypatch.setattr(ads_ai, "minta_saran", terlarang)
    h = await services.saran_ai_iklan(session, SimpleNamespace(id=None, nama_toko="X", platform="shopee"), None, 7)
    assert h["saran"] == [] and h["pemakaian"]["biaya_rp"] == 0
