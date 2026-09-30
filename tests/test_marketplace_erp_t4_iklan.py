"""Tahap 4: iklan (ads) campaigns -- manual spend entry, ROAS computed from
actual Pesanan/ItemPesanan data for the campaign's linked produk. No ads API
dependency (Shopee Ads/TikTok Ads/etc. need separate partner approval)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import (
    AkunMarketplaceIn,
    IklanCampaignIn,
    IklanCampaignPatch,
    IklanMetrikHarianIn,
    ItemPesananIn,
    PesananIn,
    ProdukIn,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s
    await engine.dispose()


async def _sku(session, sku="SKU-IKLAN", stok=50):
    return await services.create_produk(
        session, ProdukIn(sku_induk=sku, nama="Barang Iklan", harga_dasar=Decimal("10000"), stok=stok)
    )


async def _akun(session, platform="shopee"):
    return await services.create_akun_marketplace(session, AkunMarketplaceIn(platform=platform, nama_toko="Toko A"))


@pytest.mark.asyncio
async def test_create_campaign_links_produk_and_akun(session):
    produk = await _sku(session)
    akun = await _akun(session)
    now = datetime.now(timezone.utc)
    campaign = await services.create_campaign(
        session,
        IklanCampaignIn(akun_id=akun.id, produk_id=produk.id, nama="Promo Sabun", tanggal_mulai=now),
    )
    assert campaign.platform == akun.platform
    assert campaign.status == "draft"


@pytest.mark.asyncio
async def test_create_campaign_rejects_bad_period(session):
    akun = await _akun(session)
    now = datetime.now(timezone.utc)
    with pytest.raises(HTTPException) as exc_info:
        await services.create_campaign(
            session,
            IklanCampaignIn(akun_id=akun.id, nama="Promo", tanggal_mulai=now, tanggal_selesai=now - timedelta(days=1)),
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_update_campaign_status_follows_linear_lifecycle(session):
    akun = await _akun(session)
    now = datetime.now(timezone.utc)
    campaign = await services.create_campaign(
        session, IklanCampaignIn(akun_id=akun.id, nama="Promo", tanggal_mulai=now)
    )
    aktif = await services.update_campaign(session, campaign.id, IklanCampaignPatch(status="aktif"))
    assert aktif.status == "aktif"
    with pytest.raises(HTTPException) as exc_info:
        await services.update_campaign(session, campaign.id, IklanCampaignPatch(status="draft"))
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_delete_campaign_only_allowed_while_draft(session):
    akun = await _akun(session)
    now = datetime.now(timezone.utc)
    campaign = await services.create_campaign(
        session, IklanCampaignIn(akun_id=akun.id, nama="Promo", tanggal_mulai=now)
    )
    await services.update_campaign(session, campaign.id, IklanCampaignPatch(status="aktif"))
    with pytest.raises(HTTPException) as exc_info:
        await services.delete_campaign(session, campaign.id)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_record_metrik_harian_upserts_same_day(session):
    akun = await _akun(session)
    now = datetime.now(timezone.utc)
    campaign = await services.create_campaign(
        session, IklanCampaignIn(akun_id=akun.id, nama="Promo", tanggal_mulai=now)
    )
    tanggal = now.replace(hour=0, minute=0, second=0, microsecond=0)
    await services.record_metrik_harian(
        session, campaign.id, IklanMetrikHarianIn(tanggal=tanggal, impression=100, klik=10, biaya=Decimal("5000"))
    )
    # Re-entering the same day corrects instead of duplicating.
    await services.record_metrik_harian(
        session, campaign.id, IklanMetrikHarianIn(tanggal=tanggal, impression=150, klik=15, biaya=Decimal("7000"))
    )
    rows = await services.list_metrik_harian(session, campaign.id)
    assert len(rows) == 1
    assert rows[0].impression == 150
    assert rows[0].biaya == Decimal("7000")


@pytest.mark.asyncio
async def test_laporan_iklan_computes_roas_from_actual_sales(session):
    produk = await _sku(session)
    akun = await _akun(session)
    now = datetime.now(timezone.utc)
    campaign = await services.create_campaign(
        session, IklanCampaignIn(akun_id=akun.id, produk_id=produk.id, nama="Promo Sabun", tanggal_mulai=now)
    )
    tanggal = now.replace(hour=0, minute=0, second=0, microsecond=0)
    await services.record_metrik_harian(
        session, campaign.id, IklanMetrikHarianIn(tanggal=tanggal, impression=1000, klik=50, biaya=Decimal("10000"))
    )

    pesanan = await services.create_pesanan(
        session,
        PesananIn(
            platform=akun.platform,
            id_eksternal="ORD-IKLAN-1",
            akun_id=akun.id,
            items=[
                ItemPesananIn(nama_produk=produk.nama, harga_satuan=produk.harga_dasar, qty=3, produk_id=produk.id)
            ],
        ),
    )
    await services.ubah_status_pesanan(session, pesanan.id, "to_ship")

    laporan = await services.laporan_iklan(
        session, campaign.id, dari=now - timedelta(days=1), sampai=now + timedelta(days=1)
    )
    assert laporan["total_biaya"] == Decimal("10000")
    assert laporan["omzet_atribusi"] == Decimal("30000")  # 3 * 10000
    assert laporan["roas"] == Decimal("3")
    assert laporan["total_klik"] == 50


@pytest.mark.asyncio
async def test_laporan_iklan_roas_none_when_no_spend(session):
    akun = await _akun(session)
    now = datetime.now(timezone.utc)
    campaign = await services.create_campaign(
        session, IklanCampaignIn(akun_id=akun.id, nama="Promo tanpa spend", tanggal_mulai=now)
    )
    laporan = await services.laporan_iklan(
        session, campaign.id, dari=now - timedelta(days=1), sampai=now + timedelta(days=1)
    )
    assert laporan["roas"] is None
    assert laporan["total_biaya"] == Decimal("0")
