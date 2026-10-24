import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import ProcessingPrice, ProcessingPriceBackup
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.seeder import kapitalkan_katalog_manual


@pytest.mark.asyncio
async def test_manual_catalog_uppercased_once_with_backup_and_collisions_left():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    S = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    rows = {
        "a": ("non_kayu", {"produk": "sasag", "ukuran": "80x40", "varian": [{"nama": "Ukuran", "nilai": "80x40"}], "sku": "s-1"}),
        "b1": ("non_kayu", {"produk": "Pupuk", "ukuran": ""}),
        "b2": ("non_kayu", {"produk": "PUPUK", "ukuran": ""}),
        "c": ("non_kayu", {"produk": "lama", "deleted": True}),
        "d": ("produksi", {"produk": "kursi", "ukuran": "x"}),
    }
    async with S() as s:
        for i, (k, p) in rows.items():
            s.add(ProcessingPrice(id=i, kind=k, payload_json=json.dumps(p), version=1))
        await s.commit()
        assert await kapitalkan_katalog_manual(s) == {"diubah": 1, "bentrok": 2}
        await s.commit()
        assert await kapitalkan_katalog_manual(s) == {"diubah": 0, "bentrok": 2}
        await s.commit()
        got = {r.id: (json.loads(r.payload_json), r.version) for r in (await s.execute(select(ProcessingPrice))).scalars()}
        assert got["a"] == ({"produk": "SASAG", "ukuran": "80X40", "varian": [{"nama": "UKURAN", "nilai": "80X40"}], "sku": "S-1"}, 2)
        assert got["b1"][0]["produk"] == "Pupuk" and got["c"][0]["produk"] == "lama" and got["d"][0]["produk"] == "kursi"
        backups = (await s.execute(select(ProcessingPriceBackup))).scalars().all()
        assert [(b.price_id, json.loads(b.payload_json)["produk"]) for b in backups] == [("a", "sasag")]
    await engine.dispose()
