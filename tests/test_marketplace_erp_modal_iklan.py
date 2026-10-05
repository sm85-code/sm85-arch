"""Modal (cost price) of advertised products: break-even ROAS, catalogue photos on campaigns, validation."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import ads_ai, margin_iklan
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import KatalogShopee, SettlementPesanan


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


def test_break_even_roas_from_percent_or_rupiah_and_shopee_fees():
    # modal 60% + fees 10% -> margin 30% -> ROAS must reach 3.33x
    h = margin_iklan.hitung([{"harga": 100000, "modal_persen": 60}], 0.10)
    assert h["margin_persen"] == 30.0 and h["roas_impas"] == 3.33 and (h["terisi"], h["total"]) == (1, 1)
    # Rp modal over the price (500k..700k variants -> 600k): 300k = 50%
    assert margin_iklan.harga_acuan("500000", "700000") == 600000
    h = margin_iklan.hitung([{"harga": 600000, "modal_rp": 300000}], None)
    assert h["margin_persen"] == 50.0 and h["roas_impas"] == 2.0 and h["biaya_shopee_diketahui"] is False


def test_incomplete_or_impossible_margins_are_reported_not_guessed():
    assert margin_iklan.hitung([{"harga": 1000}], 0.1) == {"terisi": 0, "total": 1, "margin_persen": None, "roas_impas": None, "biaya_shopee_diketahui": True}
    assert margin_iklan.hitung([], 0.1)["total"] == 0
    assert margin_iklan.hitung([{"harga": None, "modal_rp": 5000}], 0.1)["terisi"] == 0  # Rp without a price: unknown share
    rugi = margin_iklan.hitung([{"harga": 100, "modal_persen": 95}], 0.10)  # margin -5%
    assert rugi["margin_persen"] == -5.0 and rugi["roas_impas"] is None
    sebagian = margin_iklan.hitung([{"harga": 100, "modal_persen": 50}, {"harga": 100}], 0)
    assert (sebagian["terisi"], sebagian["total"], sebagian["margin_persen"]) == (1, 2, 50.0)


@pytest.mark.asyncio
async def test_modal_is_saved_changed_cleared_and_validated(session):
    akun = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="AZFA"))
    pengguna = SimpleNamespace(id="u1")
    assert (await services.simpan_modal_produk(session, akun, "111", "250000", None, pengguna))["modal_rp"] == Decimal("250000")
    r = await services.simpan_modal_produk(session, akun, "111", None, "55.5", pengguna)
    assert r["modal_rp"] is None and r["modal_persen"] == Decimal("55.5")  # switching Rp -> % replaces the old value
    assert await services.simpan_modal_produk(session, akun, "111", None, None, pengguna) == {"item_id": "111", "modal_rp": None, "modal_persen": None}
    for rp, persen in (("1000", "10"), ("0", None), ("-5", None), (None, "0"), (None, "100"), (None, "150"), ("2000000000", None)):
        with pytest.raises(HTTPException) as exc:
            await services.simpan_modal_produk(session, akun, "111", rp, persen, pengguna)
        assert exc.value.status_code == 422
    with pytest.raises(HTTPException):
        await services.simpan_modal_produk(session, akun, "abc", "1000", None, pengguna)


@pytest.mark.asyncio
async def test_campaigns_get_product_photo_name_modal_and_break_even(session, monkeypatch):
    akun = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="AZFA"))
    session.add(KatalogShopee(akun_id=akun.id, item_id="111", nama="Partisi Rotan", foto_json=json.dumps(["https://f/1.jpg", "https://f/2.jpg"]), harga_min=Decimal("500000"), harga_max=Decimal("700000"), stok_shopee=7))
    sekarang = datetime.now(timezone.utc)
    session.add(SettlementPesanan(akun_id=akun.id, order_sn="A", dirilis_at=sekarang - timedelta(days=3), penjualan=Decimal("1000000"), komisi=Decimal("80000"), layanan=Decimal("15000"), transaksi=Decimal("5000")))
    session.add(SettlementPesanan(akun_id=akun.id, order_sn="OLD", dirilis_at=sekarang - timedelta(days=200), penjualan=Decimal("9999999"), komisi=Decimal("9999999")))
    await session.flush()
    await services.simpan_modal_produk(session, akun, "111", None, "50", SimpleNamespace(id="u"))

    async def fake(sess, a, hari):
        return {"saldo": 1, "hari": 7, "catatan": [], "kampanye": [
            {"campaign_id": "1", "nama": "A", "item_id": ["111", "222"], "kinerja": None},
            {"campaign_id": "2", "nama": "B", "item_id": [], "kinerja": None}]}

    monkeypatch.setattr(erp_shopee, "daftar_kampanye_iklan", fake)
    h = await services.daftar_kampanye_iklan(session, akun, 7)
    assert h["biaya_shopee_persen"] == pytest.approx(0.10)  # the 200-day-old settlement is ignored
    k1, k2 = h["kampanye"]
    assert k1["jumlah_produk"] == 2 and k1["produk"][0]["nama"] == "Partisi Rotan" and k1["produk"][0]["foto"] == "https://f/1.jpg"
    assert k1["produk"][0]["modal_persen"] == Decimal("50") and "harga" not in k1["produk"][0]
    assert k1["produk"][1] == {"item_id": "222", "nama": None, "foto": None, "harga_min": None, "harga_max": None, "stok": None, "modal_rp": None, "modal_persen": None}
    assert k1["margin"]["margin_persen"] == 40.0 and k1["margin"]["roas_impas"] == 2.5 and (k1["margin"]["terisi"], k1["margin"]["total"]) == (1, 2)
    assert k2["produk"] == [] and k2["margin"]["roas_impas"] is None


def test_the_advisor_gets_the_break_even_and_how_complete_the_modal_is():
    k = {"campaign_id": "1", "nama": "A", "status": "ongoing", "bidding": "auto", "anggaran": 1, "kata_kunci": [], "kinerja": {"expense": 10},
         "margin": {"terisi": 1, "total": 2, "margin_persen": 40.0, "roas_impas": 2.5}}
    ringkas = ads_ai.ringkas_kampanye([k])[0]
    assert ringkas["roas_impas"] == 2.5 and ringkas["margin_kotor_persen"] == 40.0 and ringkas["modal_terisi"] == "1/2 produk"
    tanpa = ads_ai.ringkas_kampanye([{**k, "margin": None}])[0]
    assert tanpa["roas_impas"] is None and tanpa["modal_terisi"] == "0/0 produk"
