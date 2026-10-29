"""Promo and listing wrappers. Reads may call Shopee; writes only after the guard. Both are mocked."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException

from tenants.marketplace_erp.modules.marketplace_erp.application import agen_guardrail, agen_pemasaran
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

ORANG = SimpleNamespace(id="user-1", username="owner", nama="Owner")
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


def test_catalog_covers_the_requested_families_and_keeps_reads_separate():
    katalog = agen_pemasaran.daftar_aksi()
    path = {baris["aksi"]: baris["path"] for baris in katalog["baca"] + katalog["tulis"]}
    assert path["voucher_daftar"].endswith("/voucher/get_voucher_list")
    assert path["flash_buat"].endswith("/shop_flash_sale/create_shop_flash_sale")
    assert path["hadiah_daftar"].endswith("/follow_prize/get_follow_prize_list")
    assert path["video_mulai"].endswith("/media_space/init_video_upload")
    assert path["video_selesai"].endswith("/media_space/complete_video_upload")
    assert path["listing_bermasalah"].endswith("/account_health/get_listings_with_issues")
    assert all(baris["method"] == "GET" for baris in katalog["baca"])
    assert all(baris["method"] == "POST" for baris in katalog["tulis"])
    panjang = list(range(40))
    assert agen_pemasaran.ringkas({"response": {"item_list": panjang}, "request_id": "r"})["dipotong"] is True


@pytest.mark.asyncio
async def test_a_read_calls_get_and_a_write_stays_dry_until_approved(sesi, monkeypatch):
    calls = []

    async def fake(session, akun, path, *, method="GET", body=None, params=None, read_only=None, **kw):
        calls.append((method, path, body))
        return {"request_id": "req", "response": {"item_list": [1, 2]}}

    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    baca = await agen_pemasaran.baca(sesi, AKUN, "listing_bermasalah", {"page_no": 1})
    assert baca["response"]["item_list"] == [1, 2] and calls[-1][0] == "GET"
    calls.clear()
    kering = await agen_pemasaran.tulis(
        sesi, AKUN, agen_pemasaran.TulisIn(aksi="voucher_buat", payload={"voucher_name": "PROMO"}), ORANG,
    )
    assert kering["kering"] is True and kering["diterapkan"] is False and calls == []
    with pytest.raises(HTTPException) as tolak:
        await agen_pemasaran.tulis(
            sesi, AKUN, agen_pemasaran.TulisIn(aksi="voucher_buat", payload={"voucher_name": "PROMO"}, kering=False), ORANG,
        )
    assert tolak.value.status_code == 409 and calls == []
    hasil = await agen_pemasaran.tulis(
        sesi, AKUN,
        agen_pemasaran.TulisIn(aksi="video_mulai", payload={"file_size": 1000}, kering=False, persetujuan="ya", request_id="vid"),
        ORANG,
    )
    assert hasil["diterapkan"] is True and calls[-1][0] == "POST" and calls[-1][1].endswith("/init_video_upload")
    ulang = await agen_pemasaran.tulis(
        sesi, AKUN, agen_pemasaran.TulisIn(aksi="video_selesai", kering=False, persetujuan="ya", request_id="vid"), ORANG,
    )
    assert ulang["diulang"] is True and len(calls) == 1


@pytest.mark.asyncio
async def test_stop_blocks_a_promo_write_before_shopee(sesi, monkeypatch):
    calls = []

    async def fake(*args, **kwargs):
        calls.append(args)
        return {"response": {}}

    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    await agen_guardrail.simpan_stop(sesi, "toko-1", agen_guardrail.StopIn(aktif=True, alasan="berhenti"), ORANG)
    await sesi.commit()
    with pytest.raises(HTTPException) as exc:
        await agen_pemasaran.tulis(
            sesi, AKUN, agen_pemasaran.TulisIn(aksi="flash_buat", payload={"name": "X"}, kering=False, persetujuan="ya"), ORANG,
        )
    assert exc.value.status_code == 409 and calls == []
