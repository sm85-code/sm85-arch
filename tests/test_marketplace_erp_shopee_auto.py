"""Automatic order sync (throttled), bulk processing, and cancelling an order from the ERP."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1 import marketplace_erp_router as router
from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import (
    AkunMarketplaceIn,
    ProdukIn,
    ProdukListingIn,
    ProsesMassalIn,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import PengaturanStok
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import StaffAkunMarketplace


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        # This suite exercises the explicitly enabled warehouse workflow.
        s.add(PengaturanStok(id="global", gudang_aktif=True))
        await s.flush()
        yield s
    await engine.dispose()


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(erp_shopee, "SHOPEE_LIVE_SYNC", True)
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_ID", "1")
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_KEY", "k")
    return SimpleNamespace(access_token="at", id_toko_eksternal="5", nama_toko="TES")


async def _toko(session, nama="Toko A", sid="100", status="terhubung"):
    akun = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko=nama))
    akun.access_token, akun.refresh_token, akun.id_toko_eksternal, akun.status = "at", "rt", sid, status
    await session.flush()
    return akun


# --- throttled automatic sync ------------------------------------------------------------


@pytest.mark.asyncio
async def test_klaim_sinkron_is_atomic_and_throttled(session):
    akun = await _toko(session)
    assert await services.klaim_sinkron_pesanan(session, akun, 60) is True
    assert await services.klaim_sinkron_pesanan(session, akun, 60) is False  # claimed a moment ago
    assert await services.klaim_sinkron_pesanan(session, akun, 0) is True  # window elapsed


@pytest.mark.asyncio
async def test_sinkron_semua_skips_recent_isolates_failures_and_respects_budget(session, monkeypatch):
    a, b, c = await _toko(session, "A", "1"), await _toko(session, "B", "2"), await _toko(session, "C", "3", "token_kadaluarsa")
    calls = []

    async def fake_sinkron(sess, akun):
        calls.append(akun.nama_toko)
        if akun.nama_toko == "A":
            raise HTTPException(status_code=502, detail="Shopee gagal")
        return {"pulled": 3, "baru": 2, "diperbarui": 1, "tidak_berubah": 0, "dilewati": 0}

    monkeypatch.setattr(services, "sinkron_pesanan_akun", fake_sinkron)
    out = await services.sinkron_semua_pesanan(session, [a, b, c])

    by = {o["nama_toko"]: o for o in out}
    assert by["A"]["hasil"] == "gagal" and "Shopee gagal" in by["A"]["pesan"]  # B still ran
    assert (by["B"]["hasil"], by["B"]["baru"], by["B"]["diperbarui"]) == ("ok", 2, 1)
    assert by["C"]["hasil"] == "gagal" and "hubungkan ulang" in by["C"]["pesan"]
    assert calls == ["A", "B"]

    again = await services.sinkron_semua_pesanan(session, [a, b])  # inside the throttle window
    assert [o["hasil"] for o in again] == ["dilewati", "dilewati"] and calls == ["A", "B"]

    late = await services.sinkron_semua_pesanan(session, [b], jeda_detik=0, batas_detik=0)
    assert late[0]["hasil"] == "ditunda"  # time budget used up -> next call picks it up


@pytest.mark.asyncio
async def test_sinkron_endpoint_inactive_when_live_sync_off(session, monkeypatch):
    monkeypatch.setattr(erp_shopee, "SHOPEE_LIVE_SYNC", False)
    out = await router.sinkron_pesanan_otomatis(session=session, user=SimpleNamespace(role="owner", id="u"))
    assert out == {"aktif": False, "jumlah_baru": 0, "jumlah_diperbarui": 0, "toko": []}


@pytest.mark.asyncio
async def test_sinkron_endpoint_only_connected_shops_and_staff_scope(session, live, monkeypatch):
    a, b = await _toko(session, "A", "1"), await _toko(session, "B", "2")
    await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="Belum terhubung"))
    seen = []

    async def fake_semua(sess, akun_list, *, jeda_detik):
        seen.append(([x.nama_toko for x in akun_list], jeda_detik))
        return [{"akun_id": x.id, "nama_toko": x.nama_toko, "hasil": "ok", "baru": 1, "diperbarui": 0, "pesan": None} for x in akun_list]

    monkeypatch.setattr(services, "sinkron_semua_pesanan", fake_semua)

    owner = await router.sinkron_pesanan_otomatis(paksa=True, session=session, user=SimpleNamespace(role="owner", id="o"))
    assert owner["aktif"] is True and owner["jumlah_baru"] == 2
    assert sorted(seen[0][0]) == ["A", "B"] and seen[0][1] == 5  # unconnected shop skipped; refresh = 5s window

    session.add(StaffAkunMarketplace(user_id="s1", akun_id=b.id))
    await session.flush()
    staff = await router.sinkron_pesanan_otomatis(paksa=False, session=session, user=SimpleNamespace(role="staff", id="s1"))
    assert seen[1] == (["B"], 60) and staff["jumlah_baru"] == 1  # staff only sees assigned shops


# --- bulk processing ---------------------------------------------------------------------


def test_proses_massal_is_capped():
    assert len(ProsesMassalIn(pesanan_ids=["a"]).pesanan_ids) == 1
    with pytest.raises(ValidationError):
        ProsesMassalIn(pesanan_ids=[])
    with pytest.raises(ValidationError):
        ProsesMassalIn(pesanan_ids=[str(i) for i in range(26)])


async def _pesanan(session, akun, sn, mentah="READY_TO_SHIP", status="to_ship", items=None):
    row = {"id_eksternal": sn, "status": status, "status_mentah": mentah, "nama_pembeli": "b", "total": Decimal("1"), "kurir": None, "items": items or []}
    await services.impor_pesanan_marketplace(session, akun, [row])
    return next(p for p in await services.list_pesanan(session, platform="shopee") if p.id_eksternal == sn)


@pytest.mark.asyncio
async def test_proses_massal_reports_each_order_and_keeps_going(session, monkeypatch):
    akun = await _toko(session)
    ok1, boom, ok2 = await _pesanan(session, akun, "S1"), await _pesanan(session, akun, "S2"), await _pesanan(session, akun, "S3")
    done = await _pesanan(session, akun, "S4", mentah="PROCESSED")  # already arranged on Shopee

    async def fake_proses(sess, akun_, sn):
        if sn == "S2":
            raise HTTPException(status_code=409, detail="Tidak ada jadwal pickup")
        return {"status_marketplace": "PROCESSED", "nomor_resi": f"R-{sn}"}

    monkeypatch.setattr(erp_shopee, "proses_pengiriman", fake_proses)
    ids = [ok1.id, boom.id, ok2.id, done.id]  # rollbacks inside the call expire these ORM objects
    out = await router.proses_massal_pesanan(
        ProsesMassalIn(pesanan_ids=[*ids, "tidak-ada", ids[0]]),
        session=session,
        user=SimpleNamespace(role="owner", id="o"),
    )

    by = {h["id_eksternal"]: h for h in out["hasil"] if h["id_eksternal"]}
    assert by["S1"]["ok"] and by["S3"]["ok"]  # S3 ran after S2 failed
    assert "jadwal pickup" in by["S2"]["pesan"] and "sudah diproses" in by["S4"]["pesan"]
    assert len(out["hasil"]) == 5 and (out["berhasil"], out["gagal"]) == (2, 3)  # duplicate id handled once
    assert (await services.get_pesanan(session, ids[0])).nomor_resi == "R-S1"


# --- cancel ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batalkan_pesanan_adapter_bodies(live, monkeypatch):
    calls = []

    async def fake(session, akun, path, *, method="GET", body=None, params=None, **_):
        calls.append((path, body, params))
        if path == erp_shopee._PATH_ORDER_DETAIL:
            return {"response": {"order_list": [{"item_list": [{"item_id": 9, "model_id": 4}, {"item_id": 10}]}]}}
        return {}

    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    await erp_shopee.batalkan_pesanan(None, live, "SN1", "CUSTOMER_REQUEST")
    assert calls == [(erp_shopee._PATH_CANCEL, {"order_sn": "SN1", "cancel_reason": "CUSTOMER_REQUEST"}, None)]

    calls.clear()
    await erp_shopee.batalkan_pesanan(None, live, "SN1", "OUT_OF_STOCK")
    assert calls[0][0] == erp_shopee._PATH_ORDER_DETAIL and "item_list" in calls[0][2]["response_optional_fields"]
    assert calls[1][1] == {
        "order_sn": "SN1",
        "cancel_reason": "OUT_OF_STOCK",
        "item_list": [{"item_id": 9, "model_id": 4}, {"item_id": 10, "model_id": 0}],
    }

    with pytest.raises(HTTPException) as exc:
        await erp_shopee.batalkan_pesanan(None, live, "SN1", "BORED")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_batalkan_marketplace_releases_stock_and_marks_cancelled(session, monkeypatch):
    akun = await _toko(session)
    produk = await services.create_produk(session, ProdukIn(sku_induk="K", nama="Kaos", harga_dasar=Decimal("1"), stok=10))
    await services.create_listing(session, ProdukListingIn(produk_id=produk.id, akun_id=akun.id, platform="shopee", id_eksternal="9001"))
    item = {"nama_produk": "Kaos", "harga_satuan": Decimal("1"), "qty": 3, "id_eksternal_kandidat": ["9001"]}
    pesanan = await _pesanan(session, akun, "S1", items=[item])
    assert (await services.get_produk(session, produk.id)).stok == 7  # reserved

    sent = {}

    async def fake_batal(sess, akun_, sn, alasan):
        sent.update(sn=sn, alasan=alasan)

    monkeypatch.setattr(erp_shopee, "batalkan_pesanan", fake_batal)
    out = await services.batalkan_pesanan_marketplace(session, pesanan.id, "OUT_OF_STOCK")

    assert sent == {"sn": "S1", "alasan": "OUT_OF_STOCK"}
    assert (out.status, out.status_marketplace) == ("cancelled", "CANCELLED")
    assert "Dibatalkan di Shopee" in out.catatan_sinkron
    assert (await services.get_produk(session, produk.id)).stok == 10  # stock released


@pytest.mark.asyncio
async def test_batalkan_marketplace_rejects_shipped_manual_and_shopee_refusal(session, monkeypatch):
    akun = await _toko(session)
    shipped = await _pesanan(session, akun, "S1", mentah="SHIPPED", status="shipped")
    with pytest.raises(HTTPException) as exc:
        await services.batalkan_pesanan_marketplace(session, shipped.id, "CUSTOMER_REQUEST")
    assert exc.value.status_code == 409

    manual = await services.create_pesanan(session, services.PesananIn(platform="shopee", id_eksternal="M1", akun_id=akun.id, items=[]))
    with pytest.raises(HTTPException) as exc:
        await services.batalkan_pesanan_marketplace(session, manual.id, "CUSTOMER_REQUEST")
    assert exc.value.status_code == 409

    ready = await _pesanan(session, akun, "S2")

    async def refuse(*a, **k):
        raise HTTPException(status_code=502, detail="order.error_limit: Can not cancel this order")

    monkeypatch.setattr(erp_shopee, "batalkan_pesanan", refuse)
    with pytest.raises(HTTPException):
        await services.batalkan_pesanan_marketplace(session, ready.id, "CUSTOMER_REQUEST")
    assert (await services.get_pesanan(session, ready.id)).status == "to_ship"  # untouched when Shopee says no
