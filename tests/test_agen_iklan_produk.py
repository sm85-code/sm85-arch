"""Product snapshots and the 7-vs-28 sales trend. The pull is mocked; orders are local rows."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio

from tenants.marketplace_erp.modules.marketplace_erp.application import agen_produk
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee_insights as insights
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import ItemPesanan, KatalogShopee, Pesanan
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models_agen import AgenProdukSnapshot

AKUN = SimpleNamespace(id="toko-1", platform="shopee", nama_toko="Toko A")


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


def test_snapshot_loop_is_off_by_default(monkeypatch):
    monkeypatch.delenv("AGEN_PRODUK_SNAPSHOT_MENIT", raising=False)
    assert agen_produk.interval_seconds() == 0


@pytest.mark.asyncio
async def test_pull_stores_stats_and_a_short_diagnosis_without_a_second_row(sesi, monkeypatch):
    sesi.add(KatalogShopee(akun_id="toko-1", item_id="9", nama="Kursi"))
    await sesi.commit()
    panggilan = []

    async def stats(session, akun, item_ids, chunk=50):
        panggilan.append(("stats", list(item_ids)))
        return {"9": {"sale": 10, "views": 100, "likes": 3, "rating_star": 4.5, "comment_count": 2}}, []

    async def diagnosis(session, akun, item_ids):
        panggilan.append(("diagnosis", item_ids))
        return {"success_item_list": [{"item_id": 9, "quality_level": 2, "unfinished_task": [{"issue_type": 1}]}]}

    monkeypatch.setattr(insights, "product_stats_many", stats)
    monkeypatch.setattr(insights, "diagnosis", diagnosis)
    hasil = await agen_produk.tarik_toko(sesi, AKUN, tanggal=agen_produk._hari())
    assert hasil["baru"] == 1 and hasil["diagnosis"] is True
    assert panggilan[0][0] == "stats" and "9" in panggilan[0][1]
    lagi = await agen_produk.tarik_toko(sesi, AKUN, tanggal=agen_produk._hari())
    assert lagi["baru"] == 1
    from sqlalchemy import func, select
    jumlah = (await sesi.execute(select(func.count()).select_from(AgenProdukSnapshot))).scalar_one()
    assert jumlah == 1
    row = (await sesi.execute(select(AgenProdukSnapshot))).scalar_one()
    assert row.views == 100 and row.sold == 10 and '"tingkat": 2' in row.diagnosis_json


@pytest.mark.asyncio
async def test_trend_uses_erp_orders_and_view_deltas(sesi):
    sekarang = datetime.now(timezone.utc)
    baru = Pesanan(id="p-baru", platform="shopee", id_eksternal="B", akun_id="toko-1", status="completed", dipesan_at=sekarang - timedelta(days=2))
    lama = Pesanan(id="p-lama", platform="shopee", id_eksternal="L", akun_id="toko-1", status="completed", dipesan_at=sekarang - timedelta(days=20))
    batal = Pesanan(id="p-batal", platform="shopee", id_eksternal="X", akun_id="toko-1", status="cancelled", dipesan_at=sekarang - timedelta(days=1))
    sesi.add_all([baru, lama, batal])
    sesi.add_all([
        ItemPesanan(pesanan_id="p-baru", nama_produk="K", harga_satuan=Decimal("1"), qty=4, subtotal=Decimal("4"), item_id_eksternal="9"),
        ItemPesanan(pesanan_id="p-lama", nama_produk="K", harga_satuan=Decimal("1"), qty=10, subtotal=Decimal("10"), item_id_eksternal="9"),
        ItemPesanan(pesanan_id="p-batal", nama_produk="K", harga_satuan=Decimal("1"), qty=50, subtotal=Decimal("50"), item_id_eksternal="9"),
    ])
    hari = agen_produk._hari()
    sesi.add_all([
        AgenProdukSnapshot(akun_id="toko-1", item_id="9", tanggal=hari, views=100, likes=3, sold=80, rating=Decimal("4.50"), diagnosis_json='{"tingkat": 2, "masalah": 1}'),
        AgenProdukSnapshot(akun_id="toko-1", item_id="9", tanggal=hari - timedelta(days=8), views=40, diagnosis_json="{}"),
        AgenProdukSnapshot(akun_id="toko-1", item_id="9", tanggal=hari - timedelta(days=30), views=10, diagnosis_json="{}"),
    ])
    await sesi.commit()
    hasil = await agen_produk.tren(sesi, "toko-1", "9")
    assert hasil["terjual_7"] == 4 and hasil["terjual_28"] == 14
    assert hasil["kecepatan_7"] == round(4 / 7, 2) and hasil["kecepatan_28"] == round(14 / 28, 2)
    assert hasil["views"] == 100 and hasil["views_delta_7"] == 60 and hasil["views_delta_28"] == 90
    assert hasil["diagnosis"]["masalah"] == 1 and hasil["sold_kumulatif"] == 80
