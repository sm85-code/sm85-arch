"""Arrange shipment from the ERP, follow Shopee's status afterwards, and print Shopee's own label."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
import requests
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1 import marketplace_erp_router as router
from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(erp_shopee, "SHOPEE_LIVE_SYNC", True)
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_ID", "1")
    monkeypatch.setattr(erp_shopee, "SHOPEE_PARTNER_KEY", "k")
    monkeypatch.setattr(erp_shopee, "_DOC_POLL_DELAY", 0)
    return SimpleNamespace(access_token="at", id_toko_eksternal="5", nama_toko="TES Sandbox")


PICKUP_PARAM = {
    "info_needed": {"dropoff": [], "pickup": ["address_id", "pickup_time_id"]},
    "pickup": {
        "address_list": [
            {"address_id": 1, "address_flag": [], "time_slot_list": [{"pickup_time_id": "x1"}]},
            {
                "address_id": 2,
                "address_flag": ["default_address", "pickup_address"],
                "time_slot_list": [
                    {"pickup_time_id": "t1", "flags": []},
                    {"pickup_time_id": "t2", "flags": ["recommended"]},
                ],
            },
        ]
    },
}


# --- choosing how to arrange shipment ---------------------------------------------


def test_prefers_pickup_address_and_recommended_slot():
    assert erp_shopee.pilih_parameter_kirim(PICKUP_PARAM, "Toko") == {"pickup": {"address_id": 2, "pickup_time_id": "t2"}}


def test_pickup_without_slots_is_a_clear_409():
    param = {"info_needed": {"pickup": ["address_id", "pickup_time_id"]}, "pickup": {"address_list": [{"address_id": 1}]}}
    with pytest.raises(HTTPException) as exc:
        erp_shopee.pilih_parameter_kirim(param, "Toko")
    assert exc.value.status_code == 409


def test_pickup_that_needs_a_carrier_tracking_number_is_refused():
    param = {"info_needed": {"pickup": ["address_id", "tracking_number"]}, "pickup": {"address_list": [{"address_id": 1}]}}
    with pytest.raises(HTTPException) as exc:
        erp_shopee.pilih_parameter_kirim(param, "Toko")
    assert exc.value.status_code == 409 and "Seller Centre" in exc.value.detail


def test_falls_back_to_dropoff_with_branch_and_sender():
    param = {
        "info_needed": {"dropoff": ["branch_id", "sender_real_name"]},
        "dropoff": {"branch_list": [{"branch_id": 77}, {"branch_id": 78}]},
    }
    assert erp_shopee.pilih_parameter_kirim(param, "Toko A") == {"dropoff": {"branch_id": 77, "sender_real_name": "Toko A"}}


def test_nothing_available_is_409():
    with pytest.raises(HTTPException) as exc:
        erp_shopee.pilih_parameter_kirim({"info_needed": {}}, "Toko")
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_proses_pengiriman_calls_param_then_ship_then_tracking(live, monkeypatch):
    calls = []

    async def fake(session, akun, path, *, method="GET", body=None, params=None, **_):
        calls.append((path, method, body, params))
        if path == erp_shopee._PATH_SHIP_PARAM:
            return {"response": PICKUP_PARAM}
        if path == erp_shopee._PATH_TRACKING:
            return {"response": {"tracking_number": "ID123"}}
        return {}

    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    out = await erp_shopee.proses_pengiriman(None, live, "SN1")

    assert out == {"status_marketplace": "PROCESSED", "nomor_resi": "ID123"}
    assert [c[0] for c in calls] == [erp_shopee._PATH_SHIP_PARAM, erp_shopee._PATH_SHIP_ORDER, erp_shopee._PATH_TRACKING]
    assert calls[1][1:3] == ("POST", {"order_sn": "SN1", "pickup": {"address_id": 2, "pickup_time_id": "t2"}})


# --- service: process + follow Shopee afterwards ------------------------------------------


async def _pesanan_sync(session, status="to_ship", mentah="READY_TO_SHIP"):
    akun = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="TES"))
    row = {
        "id_eksternal": "SN1", "status": status, "status_mentah": mentah, "nama_pembeli": "budi",
        "total": Decimal("1000"), "kurir": None, "items": [],
    }
    await services.impor_pesanan_marketplace(session, akun, [row])
    (pesanan,) = await services.list_pesanan(session, platform="shopee")
    return akun, pesanan, row


@pytest.mark.asyncio
async def test_proses_pesanan_marketplace_updates_order(session, live, monkeypatch):
    akun, pesanan, _ = await _pesanan_sync(session)
    akun.access_token, akun.id_toko_eksternal = "at", "5"

    async def fake(session, akun, order_sn):
        assert order_sn == "SN1"
        return {"status_marketplace": "PROCESSED", "nomor_resi": "ID123"}

    monkeypatch.setattr(erp_shopee, "proses_pengiriman", fake)
    out = await services.proses_pesanan_marketplace(session, pesanan.id)
    assert out.status == "to_ship"  # still to_ship locally until the courier picks it up
    assert (out.status_marketplace, out.nomor_resi) == ("PROCESSED", "ID123")
    assert out.tersinkron_marketplace is True and "menunggu kurir" in out.catatan_sinkron


@pytest.mark.asyncio
async def test_proses_rejects_manual_processed_and_wrong_status(session, monkeypatch):
    akun, pesanan, _ = await _pesanan_sync(session)
    manual = await services.create_pesanan(
        session, services.PesananIn(platform="shopee", id_eksternal="MANUAL", akun_id=akun.id, items=[])
    )
    with pytest.raises(HTTPException) as exc:
        await services.proses_pesanan_marketplace(session, manual.id)
    assert exc.value.status_code == 409  # typed-in orders are not Shopee orders

    pesanan.status_marketplace = "PROCESSED"
    with pytest.raises(HTTPException) as exc:
        await services.proses_pesanan_marketplace(session, pesanan.id)
    assert "sudah diproses" in exc.value.detail

    pesanan.status_marketplace, pesanan.status = "READY_TO_SHIP", "shipped"
    with pytest.raises(HTTPException) as exc:
        await services.proses_pesanan_marketplace(session, pesanan.id)
    assert "to_ship" in exc.value.detail


@pytest.mark.asyncio
async def test_sync_after_courier_pickup_moves_order_to_shipped(session):
    akun, pesanan, row = await _pesanan_sync(session)
    row2 = {**row, "status": "shipped", "status_mentah": "SHIPPED", "kurir": "J&T Express", "nomor_resi": "ID123"}
    hasil = await services.impor_pesanan_marketplace(session, akun, [row2])

    assert hasil["diperbarui"] == 1
    out = await services.get_pesanan(session, pesanan.id)
    assert out.status == "shipped" and out.status_marketplace == "SHIPPED"
    assert out.catatan_sinkron == "Status mengikuti shopee (SHIPPED)"  # no stale "menunggu kurir" note
    assert (out.kurir, out.nomor_resi) == ("J&T Express", "ID123") and out.tanggal_kirim is not None


@pytest.mark.asyncio
async def test_id_pesanan_punya_resi(session):
    akun, pesanan, row = await _pesanan_sync(session)
    assert await services.id_pesanan_punya_resi(session, akun) == set()
    await services.impor_pesanan_marketplace(session, akun, [{**row, "nomor_resi": "ID123"}])
    assert await services.id_pesanan_punya_resi(session, akun) == {"SN1"}


# --- shipping label ----------------------------------------------------------------------


def _label_fake(statuses, calls):
    seq = iter(statuses)

    async def fake(session, akun, path, *, method="GET", body=None, params=None, raw=False):
        calls.append((path, body))
        if path == erp_shopee._PATH_TRACKING:
            return {"response": {"tracking_number": "ID123"}}
        if path == erp_shopee._PATH_DOC_PARAM:
            return {"response": {"result_list": [{"suggest_shipping_document_type": "THERMAL_AIR_WAYBILL"}]}}
        if path == erp_shopee._PATH_DOC_CREATE:
            return {"response": {"result_list": [{"order_sn": "SN1"}]}}
        if path == erp_shopee._PATH_DOC_RESULT:
            return {"response": {"result_list": [{"status": next(seq)}]}}
        assert path == erp_shopee._PATH_DOC_DOWNLOAD and raw is True
        return {"_bytes": b"%PDF-1.4 label"}

    return fake


@pytest.mark.asyncio
async def test_unduh_resi_uses_suggested_template_and_waits_until_ready(live, monkeypatch):
    calls = []
    monkeypatch.setattr(erp_shopee, "signed_shop_request", _label_fake(["PROCESSING", "PROCESSING", "READY"], calls))
    pdf = await erp_shopee.unduh_resi(None, live, "SN1")

    assert pdf.startswith(b"%PDF")
    create = next(b for p, b in calls if p == erp_shopee._PATH_DOC_CREATE)
    assert create == {"order_list": [{"order_sn": "SN1", "shipping_document_type": "THERMAL_AIR_WAYBILL", "tracking_number": "ID123"}]}
    assert [p for p, _ in calls].count(erp_shopee._PATH_DOC_RESULT) == 3
    download = next(b for p, b in calls if p == erp_shopee._PATH_DOC_DOWNLOAD)
    assert download["order_list"] == [{"order_sn": "SN1", "shipping_document_type": "THERMAL_AIR_WAYBILL"}]


@pytest.mark.asyncio
async def test_unduh_resi_failed_and_timeout_are_502(live, monkeypatch):
    monkeypatch.setattr(erp_shopee, "signed_shop_request", _label_fake(["FAILED"], []))
    with pytest.raises(HTTPException) as exc:
        await erp_shopee.unduh_resi(None, live, "SN1")
    assert exc.value.status_code == 502

    monkeypatch.setattr(erp_shopee, "_DOC_POLL_TRIES", 2)
    monkeypatch.setattr(erp_shopee, "signed_shop_request", _label_fake(["PROCESSING"] * 5, []))
    with pytest.raises(HTTPException) as exc:
        await erp_shopee.unduh_resi(None, live, "SN1")
    assert exc.value.status_code == 502 and "belum siap" in exc.value.detail


@pytest.mark.asyncio
async def test_resi_endpoint_returns_pdf_only_for_processed_orders(session, monkeypatch):
    akun, pesanan, row = await _pesanan_sync(session)
    user = SimpleNamespace(role="owner", id="u1")

    with pytest.raises(HTTPException) as exc:  # READY_TO_SHIP: not processed yet
        await router.cetak_resi_pesanan(pesanan.id, session=session, user=user)
    assert exc.value.status_code == 409

    await services.impor_pesanan_marketplace(session, akun, [{**row, "status_mentah": "PROCESSED"}])

    async def fake_unduh(session, akun, order_sn, nomor_resi=None):
        assert order_sn == "SN1"
        return b"%PDF-1.4 x"

    monkeypatch.setattr(erp_shopee, "unduh_resi", fake_unduh)
    resp = await router.cetak_resi_pesanan(pesanan.id, session=session, user=user)
    assert resp.media_type == "application/pdf" and resp.body.startswith(b"%PDF")
    assert 'filename="resi-SN1.pdf"' in resp.headers["content-disposition"]


# --- binary download support -------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_shop_api_raw_returns_file_bytes(live, monkeypatch):
    class FakeResp:
        ok, status_code, content = True, 200, b"%PDF-1.4 file"

        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp())
    out = await erp_shopee._call_shop_api(access_token="at", shop_id="5", api_path="/x", method="POST", body={}, raw=True)
    assert out == {"_bytes": b"%PDF-1.4 file"}

    with pytest.raises(HTTPException):  # same body without raw=True is still an error
        await erp_shopee._call_shop_api(access_token="at", shop_id="5", api_path="/x", method="POST", body={})
