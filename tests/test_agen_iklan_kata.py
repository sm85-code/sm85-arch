"""Bids and keywords, including negative keywords, on the guarded ads path. Shopee writes are mocked."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException

from tenants.marketplace_erp.modules.marketplace_erp.application import agen_aksi, agen_guardrail
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import shopee_iklan

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


def _pasang(monkeypatch, calls):
    kata = {
        "kursi": {"keyword": "kursi", "status": "normal", "match_type": "broad", "bid_price_per_click": 300},
        "gratis": {"keyword": "gratis", "status": "blacklist", "match_type": "exact", "bid_price_per_click": 1},
    }

    async def fake(session, akun, aksi, *, params=None, body=None):
        calls.append((aksi, body))
        if aksi == "pengaturan_kampanye":
            return {"response": {"campaign_list": [{
                "campaign_id": 7,
                "common_info": {"campaign_budget": 50000, "campaign_status": "ongoing"},
                "auto_bidding_info": {"roas_target": 4},
                "manual_bidding_info": {"selected_keywords": list(kata.values())},
            }]}}
        if aksi == "ubah_kata_kunci":
            for item in body["selected_keywords"]:
                baris = kata.setdefault(item["keyword"], {"keyword": item["keyword"], "status": "normal", "match_type": "broad"})
                if item["edit_action"] == "change_bid_price":
                    baris["bid_price_per_click"] = item["bid_price_per_click"]
                elif item["edit_action"] == "delete":
                    baris["status"] = "blacklist"
                elif item["edit_action"] == "restore":
                    baris["status"] = "normal"
                elif item["edit_action"] == "add":
                    baris["status"] = "normal"
                    baris["bid_price_per_click"] = item.get("bid_price_per_click")
            return {"response": [{"campaign_id": body["campaign_id"]}]}
        raise AssertionError(aksi)

    monkeypatch.setattr(shopee_iklan, "panggil", fake)


@pytest.mark.asyncio
async def test_bid_stays_inside_percent_and_cpc_and_rolls_back(sesi, monkeypatch):
    calls = []
    _pasang(monkeypatch, calls)
    kering = await agen_aksi.terapkan(
        sesi, AKUN, agen_aksi.AksiIn(jenis="bid", campaign_id=7, kata="kursi", nilai=Decimal("330")), ORANG,
    )
    assert kering["kering"] is True and [a for a, _ in calls] == ["pengaturan_kampanye"]
    calls.clear()
    hasil = await agen_aksi.terapkan(
        sesi, AKUN,
        agen_aksi.AksiIn(jenis="bid", campaign_id=7, kata="kursi", nilai=Decimal("360"), kering=False, persetujuan="ya", request_id="bid-1"),
        ORANG,
    )
    assert hasil["diterapkan"] is True
    kata = calls[-1][1]["selected_keywords"][0]
    assert kata["edit_action"] == "change_bid_price" and kata["bid_price_per_click"] == 360 and kata["keyword"] == "kursi"
    calls.clear()
    with pytest.raises(HTTPException) as besar:
        await agen_aksi.terapkan(
            sesi, AKUN, agen_aksi.AksiIn(jenis="bid", campaign_id=7, kata="kursi", nilai=Decimal("450"), kering=False, persetujuan="ya"),
            ORANG,
        )
    assert besar.value.status_code == 422 and all(a != "ubah_kata_kunci" for a, _ in calls)
    await agen_guardrail.simpan_batas(sesi, "toko-1", agen_guardrail.BatasIn(maks_cpc=Decimal("350")), ORANG)
    await sesi.commit()
    with pytest.raises(HTTPException) as cap:
        await agen_aksi.terapkan(
            sesi, AKUN, agen_aksi.AksiIn(jenis="bid", campaign_id=7, kata="kursi", nilai=Decimal("355"), kering=False, persetujuan="ya", request_id="bid-cap"),
            ORANG,
        )
    assert cap.value.status_code == 422
    calls.clear()
    balik = await agen_aksi.kembalikan(
        sesi, AKUN, hasil["audit_id"], agen_aksi.KembaliIn(kering=False, persetujuan="ya", request_id="balik-bid"), ORANG,
    )
    assert balik["diterapkan"] is True
    assert calls[-1][1]["selected_keywords"][0]["bid_price_per_click"] == 300


@pytest.mark.asyncio
async def test_negative_keywords_use_delete_and_restore_without_a_bid(sesi, monkeypatch):
    calls = []
    _pasang(monkeypatch, calls)
    blokir = await agen_aksi.terapkan(
        sesi, AKUN,
        agen_aksi.AksiIn(
            jenis="kata_negatif", campaign_id=7, kering=False, persetujuan="ya",
            kata_kunci=[agen_aksi.KataPerubahan(aksi="tambah", kata="kursi")],
        ),
        ORANG,
    )
    assert blokir["diterapkan"] is True
    assert calls[-1][1]["selected_keywords"] == [{"edit_action": "delete", "keyword": "kursi"}]
    calls.clear()
    buka = await agen_aksi.terapkan(
        sesi, AKUN,
        agen_aksi.AksiIn(
            jenis="kata_negatif", campaign_id=7, kering=False, persetujuan="ya", request_id="buka",
            kata_kunci=[agen_aksi.KataPerubahan(aksi="hapus", kata="gratis")],
        ),
        ORANG,
    )
    assert calls[-1][1]["selected_keywords"] == [{"edit_action": "restore", "keyword": "gratis"}]
    calls.clear()
    balik = await agen_aksi.kembalikan(
        sesi, AKUN, buka["audit_id"], agen_aksi.KembaliIn(kering=False, persetujuan="ya", request_id="balik-neg"), ORANG,
    )
    assert balik["diterapkan"] is True
    assert calls[-1][1]["selected_keywords"][0]["edit_action"] == "delete"
    assert calls[-1][1]["selected_keywords"][0]["keyword"] == "gratis"


@pytest.mark.asyncio
async def test_autonomy_cannot_add_a_keyword_bid_without_a_cpc_cap(sesi, monkeypatch):
    calls = []
    _pasang(monkeypatch, calls)
    await agen_guardrail.simpan_batas(sesi, "toko-1", agen_guardrail.BatasIn(otonomi=True), ORANG)
    await sesi.commit()
    with pytest.raises(HTTPException) as exc:
        await agen_aksi.terapkan(
            sesi, AKUN,
            agen_aksi.AksiIn(
                jenis="kata_kunci", campaign_id=7, kering=False,
                kata_kunci=[agen_aksi.KataPerubahan(aksi="add", kata="meja", bid=Decimal("200"), tipe="broad")],
            ),
            ORANG,
        )
    assert exc.value.status_code == 422 and all(a != "ubah_kata_kunci" for a, _ in calls)
    await agen_guardrail.simpan_batas(sesi, "toko-1", agen_guardrail.BatasIn(otonomi=True, maks_cpc=Decimal("250")), ORANG)
    await sesi.commit()
    hasil = await agen_aksi.terapkan(
        sesi, AKUN,
        agen_aksi.AksiIn(
            jenis="kata_kunci", campaign_id=7, kering=False, request_id="tambah",
            kata_kunci=[agen_aksi.KataPerubahan(aksi="add", kata="meja", bid=Decimal("200"), tipe="broad")],
        ),
        ORANG,
    )
    assert hasil["diterapkan"] is True
    assert calls[-1][1]["selected_keywords"][0]["keyword"] == "meja"
