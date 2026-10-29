"""Ads-agent limits, STOP, and audit. These tests must not call Shopee."""
import inspect
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError

from tenants.marketplace_erp.modules.marketplace_erp.application import agen_guardrail, peta_akses


def test_guardrail_module_does_not_call_shopee_and_schema_does_not_drop_anything():
    src = inspect.getsource(agen_guardrail)
    assert "shopee_iklan" not in src and "signed_shop_request" not in src
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import models_agen

    ddl = inspect.getsource(models_agen).lower()
    assert "drop table" not in ddl and "drop column" not in ddl
    assert peta_akses.cari("/api/marketplace-erp/akun/ABC/iklan/agen/batas", "GET") == ("iklan", "baca")
    assert peta_akses.cari("/api/marketplace-erp/akun/ABC/iklan/agen/batas", "PUT") == ("iklan", "tulis")
    assert peta_akses.cari("/api/marketplace-erp/iklan/agen/stop", "PUT") == ("iklan", "tulis")
    assert peta_akses.cari("/api/marketplace-erp/iklan/agen/audit", "GET") == ("iklan", "baca")


def test_percent_and_money_limits_reject_bad_values():
    with pytest.raises(ValidationError):
        agen_guardrail.BatasIn(maks_persen_perubahan=0)
    with pytest.raises(ValidationError):
        agen_guardrail.BatasIn(min_hari_antar_perubahan=-1)
    with pytest.raises(HTTPException) as exc:
        agen_guardrail._uang(Decimal("0"), "Anggaran harian", agen_guardrail.ANGGARAN_MAKS)
    assert exc.value.status_code == 422
    assert agen_guardrail.BatasIn().otonomi is False
    assert agen_guardrail.BatasIn().maks_persen_perubahan == 20


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


ORANG = SimpleNamespace(id="user-1", username="owner", nama="Owner")


@pytest.mark.asyncio
async def test_limits_default_then_roundtrip_and_a_repeat_request_does_not_apply_twice(sesi):
    assert (await agen_guardrail.baca_batas(sesi, "toko-1"))["otonomi"] is False
    assert (await agen_guardrail.baca_batas(sesi, "toko-1"))["tersimpan"] is False
    hasil = await agen_guardrail.simpan_batas(
        sesi, "toko-1",
        agen_guardrail.BatasIn(anggaran_harian_maks=Decimal("80000"), maks_cpc=Decimal("350"), min_hari_antar_perubahan=2,
                               otonomi=True, alasan="batas awal", request_id="req-1"),
        ORANG,
    )
    assert hasil["anggaran_harian_maks"] == 80000 and hasil["maks_cpc"] == 350 and hasil["otonomi"] is True
    ulang = await agen_guardrail.simpan_batas(
        sesi, "toko-1", agen_guardrail.BatasIn(otonomi=False, request_id="req-1"), ORANG,
    )
    assert ulang["otonomi"] is True  # same request id: the first result stands
    assert (await agen_guardrail.baca_batas(sesi, "toko-1"))["maks_persen_perubahan"] == 20
    await agen_guardrail.simpan_batas(sesi, "toko-1", agen_guardrail.BatasIn(alasan="reset"), ORANG)
    assert (await agen_guardrail.baca_batas(sesi, "toko-1"))["otonomi"] is False
    log = await agen_guardrail.daftar_audit(sesi, akun_id="toko-1", diizinkan=None, limit=10)
    assert log["items"][0]["aksi"] == "batas" and log["items"][0]["pelaku_nama"] == "owner"
    assert log["items"][-1]["sebelum"]["tersimpan"] is False
    assert log["items"][-1]["request_id"] == "req-1"
    assert len(log["items"]) == 2


@pytest.mark.asyncio
async def test_global_and_shop_stop_are_independent_and_either_blocks_writes(sesi):
    assert (await agen_guardrail.baca_stop(sesi, None))["aktif"] is False
    assert await agen_guardrail.tulisan_diblokir(sesi, "toko-1") is None
    await agen_guardrail.simpan_stop(sesi, "toko-1", agen_guardrail.StopIn(aktif=True, alasan="toko"), ORANG)
    assert "toko" in (await agen_guardrail.tulisan_diblokir(sesi, "toko-1"))
    assert await agen_guardrail.tulisan_diblokir(sesi, "toko-lain") is None
    await agen_guardrail.simpan_stop(sesi, None, agen_guardrail.StopIn(aktif=True, alasan="semua"), ORANG)
    assert (await agen_guardrail.tulisan_diblokir(sesi, "toko-lain")) == "semua"
    log = await agen_guardrail.daftar_audit(sesi, akun_id=None, diizinkan=["toko-1"], limit=10)
    assert any(item["aksi"] == "stop" and item["akun_id"] is None for item in log["items"])
    with pytest.raises(HTTPException) as exc:
        await agen_guardrail.daftar_audit(sesi, akun_id="toko-lain", diizinkan=["toko-1"], limit=10)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_creating_the_new_tables_keeps_an_existing_shop_row(sesi):
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import AkunMarketplace

    async with sesi.bind.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    akun = await sesi.get(AkunMarketplace, "toko-1")
    assert akun.nama_toko == "Toko A"
    for nama in ("mpe_agen_iklan_batas", "mpe_agen_iklan_stop", "mpe_agen_iklan_audit"):
        assert nama in MarketplaceErpBase.metadata.tables
