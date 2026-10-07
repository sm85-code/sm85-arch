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
        if path == erp_shopee._PATH_ORDER_DETAIL:
            return {"response": {"order_list": [{"order_sn": "SN1", "order_status": "READY_TO_SHIP"}]}}
        if path == erp_shopee._PATH_SHIP_PARAM:
            return {"response": PICKUP_PARAM}
        if path == erp_shopee._PATH_TRACKING:
            return {"response": {"tracking_number": "ID123"}}
        return {}

    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    out = await erp_shopee.proses_pengiriman(None, live, "SN1")

    assert out == {"status_marketplace": "PROCESSED", "nomor_resi": "ID123", "metode_pengiriman": "pickup"}
    assert [c[0] for c in calls] == [erp_shopee._PATH_ORDER_DETAIL, erp_shopee._PATH_SHIP_PARAM, erp_shopee._PATH_SHIP_ORDER, erp_shopee._PATH_TRACKING]
    assert calls[2][1:3] == ("POST", {"order_sn": "SN1", "pickup": {"address_id": 2, "pickup_time_id": "t2"}})


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


def _label_fake(statuses, calls, selectable=("NORMAL_AIR_WAYBILL", "THERMAL_AIR_WAYBILL")):
    seq = iter(statuses)

    async def fake(session, akun, path, *, method="GET", body=None, params=None, raw=False, timeout=None):
        calls.append((path, body))
        if path == erp_shopee._PATH_TRACKING:
            return {"response": {"tracking_number": "ID123"}}
        if path == erp_shopee._PATH_DOC_PARAM:
            return {
                "response": {
                    "result_list": [
                        {"suggest_shipping_document_type": "NORMAL_AIR_WAYBILL", "selectable_shipping_document_type": list(selectable)}
                    ]
                }
            }
        if path == erp_shopee._PATH_DOC_CREATE:
            return {"response": {"result_list": [{"order_sn": "SN1"}]}}
        if path == erp_shopee._PATH_DOC_RESULT:
            return {"response": {"result_list": [{"status": next(seq)}]}}
        assert path == erp_shopee._PATH_DOC_DOWNLOAD and raw is True
        return {"_bytes": b"%PDF-1.4 label"}

    return fake


@pytest.mark.asyncio
async def test_unduh_resi_prefers_thermal_a6_over_the_suggested_a4_and_waits_until_ready(live, monkeypatch):
    calls = []
    monkeypatch.setattr(erp_shopee, "signed_shop_request", _label_fake(["PROCESSING", "PROCESSING", "READY"], calls))
    pdf = await erp_shopee.unduh_resi(None, live, "SN1")

    assert pdf.startswith(b"%PDF")
    create = next(b for p, b in calls if p == erp_shopee._PATH_DOC_CREATE)
    assert create == {"order_list": [{"order_sn": "SN1", "shipping_document_type": "THERMAL_AIR_WAYBILL", "tracking_number": "ID123"}]}
    assert [p for p, _ in calls].count(erp_shopee._PATH_DOC_RESULT) == 3
    download = next(b for p, b in calls if p == erp_shopee._PATH_DOC_DOWNLOAD)
    assert download["shipping_document_type"] == "THERMAL_AIR_WAYBILL"  # documented top-level template
    assert download["order_list"] == [{"order_sn": "SN1", "shipping_document_type": "THERMAL_AIR_WAYBILL"}]


@pytest.mark.asyncio
async def test_unduh_resi_failed_and_not_ready_never_look_like_a_gateway_error(live, monkeypatch):
    monkeypatch.setattr(erp_shopee, "signed_shop_request", _label_fake(["FAILED"], []))
    with pytest.raises(HTTPException) as exc:
        await erp_shopee.unduh_resi(None, live, "SN1")
    assert exc.value.status_code == 424  # not 5xx: the proxy would turn that into an opaque 504 page

    monkeypatch.setattr(erp_shopee, "_DOC_POLL_TRIES", 2)
    monkeypatch.setattr(erp_shopee, "signed_shop_request", _label_fake(["PROCESSING"] * 5, []))
    with pytest.raises(HTTPException) as exc:
        await erp_shopee.unduh_resi(None, live, "SN1")
    assert exc.value.status_code == 409 and "belum siap" in exc.value.detail


@pytest.mark.asyncio
async def test_unduh_resi_gives_up_at_the_overall_deadline(live, monkeypatch):
    """Slow Shopee answers must end in a readable 409 well before the proxy's ~100 s limit."""
    monkeypatch.setattr(erp_shopee, "_DOC_BATAS_DETIK", 0)
    calls = []
    monkeypatch.setattr(erp_shopee, "signed_shop_request", _label_fake(["PROCESSING"] * 10, calls))
    with pytest.raises(HTTPException) as exc:
        await erp_shopee.unduh_resi(None, live, "SN1")
    assert exc.value.status_code == 409 and "THERMAL_AIR_WAYBILL" in exc.value.detail
    assert [p for p, _ in calls].count(erp_shopee._PATH_DOC_RESULT) == 1  # stopped after the first poll


@pytest.mark.asyncio
async def test_resi_endpoint_returns_pdf_only_for_processed_orders(session, monkeypatch):
    akun, pesanan, row = await _pesanan_sync(session)
    user = SimpleNamespace(role="owner", id="u1")

    with pytest.raises(HTTPException) as exc:  # READY_TO_SHIP: not processed yet
        await router.cetak_resi_pesanan(pesanan.id, tipe=None, session=session, user=user)
    assert exc.value.status_code == 409

    await services.impor_pesanan_marketplace(session, akun, [{**row, "status_mentah": "PROCESSED"}])

    asked = {}

    async def fake_unduh(session, akun, order_sn, nomor_resi=None, tipe=None):
        assert order_sn == "SN1"
        asked["tipe"] = tipe
        return b"%PDF-1.4 x"

    monkeypatch.setattr(erp_shopee, "unduh_resi", fake_unduh)
    resp = await router.cetak_resi_pesanan(pesanan.id, tipe="NORMAL_AIR_WAYBILL", session=session, user=user)
    assert asked["tipe"] == "NORMAL_AIR_WAYBILL"
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


@pytest.mark.asyncio
async def test_unduh_resi_template_choice(live, monkeypatch):
    def create_type(calls):
        return next(b for p, b in calls if p == erp_shopee._PATH_DOC_CREATE)["order_list"][0]["shipping_document_type"]

    calls = []
    monkeypatch.setattr(erp_shopee, "signed_shop_request", _label_fake(["READY"], calls))
    await erp_shopee.unduh_resi(None, live, "SN1", tipe="NORMAL_AIR_WAYBILL")  # explicit A4
    assert create_type(calls) == "NORMAL_AIR_WAYBILL"

    calls = []
    monkeypatch.setattr(erp_shopee, "signed_shop_request", _label_fake(["READY"], calls, selectable=("NORMAL_AIR_WAYBILL",)))
    await erp_shopee.unduh_resi(None, live, "SN1")  # no thermal offered -> Shopee's suggestion
    assert create_type(calls) == "NORMAL_AIR_WAYBILL"

    monkeypatch.setattr(erp_shopee, "signed_shop_request", _label_fake(["READY"], [], selectable=("NORMAL_AIR_WAYBILL",)))
    with pytest.raises(HTTPException) as exc:  # asking for a template the courier does not offer
        await erp_shopee.unduh_resi(None, live, "SN1", tipe="THERMAL_AIR_WAYBILL")
    assert exc.value.status_code == 409 and "NORMAL_AIR_WAYBILL" in exc.value.detail


# Explicit shipping methods must never fall back to a different method.
@pytest.mark.parametrize("param", [{"info_needed": {"dropoff": []}}, {"info_needed": {"dropoff": []}, "dropoff": {}}])
def test_empty_dropoff_is_available_and_can_be_submitted(param):
    assert erp_shopee.parameter_kirim_eksplisit(param, "Toko", {"metode": "dropoff"}) == {"dropoff": {}}
    assert erp_shopee.pilih_parameter_kirim(param, "Toko") == {"dropoff": {}}


def test_explicit_dropoff_wins_even_when_pickup_exists():
    param = {**PICKUP_PARAM, "dropoff": {}}
    assert erp_shopee.parameter_kirim_eksplisit(param, "Toko", {"metode": "dropoff"}) == {"dropoff": {}}


@pytest.mark.parametrize("setting", [
    {"metode": "pickup", "address_id": 999, "pickup_time_id": "t2"},
    {"metode": "pickup", "address_id": 1, "pickup_time_id": "t2"},
    {"metode": "pickup", "address_id": 2},
    {"metode": "pickup", "address_id": 2, "pickup_time_id": "expired"},
    {"metode": "pickup", "address_id": 2, "pickup_time_id": "t2", "branch_id": 77},
])
def test_invalid_pickup_fields_are_refused(setting):
    with pytest.raises(HTTPException) as exc:
        erp_shopee.parameter_kirim_eksplisit(PICKUP_PARAM, "Toko", setting)
    assert exc.value.status_code == 409


def test_branch_and_sender_are_validated():
    param = {"info_needed": {"dropoff": ["branch_id", "sender_real_name"]},
             "dropoff": {"branch_list": [{"branch_id": 77}, {"branch_id": 78}]}}
    assert erp_shopee.parameter_kirim_eksplisit(param, "Toko", {
        "metode": "dropoff", "branch_id": 78, "sender_real_name": " Budi "
    }) == {"dropoff": {"branch_id": 78, "sender_real_name": "Budi"}}
    for setting in ({"metode": "dropoff", "branch_id": 99, "sender_real_name": "Budi"},
                    {"metode": "dropoff", "branch_id": 77, "sender_real_name": " "}):
        with pytest.raises(HTTPException):
            erp_shopee.parameter_kirim_eksplisit(param, "Toko", setting)


def test_unsupported_required_fields_disable_only_that_mode():
    param = {"info_needed": {"dropoff": ["tracking_no"], "pickup": ["address_id"]},
             "pickup": PICKUP_PARAM["pickup"]}
    options = {o["metode"]: o for o in erp_shopee.opsi_parameter_kirim(param, "Toko")["opsi"]}
    assert options["pickup"]["tersedia"] is True
    assert options["dropoff"]["tersedia"] is False
    with pytest.raises(HTTPException):
        erp_shopee.parameter_kirim_eksplisit(param, "Toko", {"metode": "dropoff"})


def test_shipping_schema_rejects_cross_method_fields_and_incomplete_batch_settings():
    from pydantic import ValidationError
    from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import PengaturanPengirimanIn, ProsesMassalIn
    for payload in ({"metode": "unknown"}, {"metode": "dropoff", "address_id": 1},
                    {"metode": "pickup", "branch_id": 77}, {"metode": "pickup", "address_id": -1}):
        with pytest.raises(ValidationError):
            PengaturanPengirimanIn(**payload)
    assert ProsesMassalIn(pesanan_ids=["a"]).pengaturan == {}  # old client
    with pytest.raises(ValidationError):
        ProsesMassalIn(pesanan_ids=["a", "b"], pengaturan={"a": {"metode": "dropoff"}})


@pytest.mark.asyncio
@pytest.mark.parametrize("metode,setting,body", [
    ("dropoff", {"metode": "dropoff"}, {"dropoff": {}}),
    ("pickup", {"metode": "pickup", "address_id": 1, "pickup_time_id": "x1"},
     {"pickup": {"address_id": 1, "pickup_time_id": "x1"}}),
])
async def test_explicit_shipping_checks_remote_status_and_sends_exact_body(live, monkeypatch, metode, setting, body):
    calls = []
    async def fake(session, akun, path, **kwargs):
        calls.append((path, kwargs))
        if path == erp_shopee._PATH_ORDER_DETAIL:
            return {"response": {"order_list": [{"order_sn": "SN1", "order_status": "READY_TO_SHIP"}]}}
        if path == erp_shopee._PATH_SHIP_PARAM:
            return {"response": PICKUP_PARAM}
        if path == erp_shopee._PATH_TRACKING:
            raise requests.Timeout("tracking not ready")
        return {}
    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    out = await erp_shopee.proses_pengiriman(None, live, "SN1", setting)
    assert out["metode_pengiriman"] == metode and out["nomor_resi"] is None
    assert next(kwargs["body"] for path, kwargs in calls if path == erp_shopee._PATH_SHIP_ORDER) == {"order_sn": "SN1", **body}


@pytest.mark.asyncio
async def test_remote_processed_status_prevents_resubmission(live, monkeypatch):
    calls = []
    async def fake(session, akun, path, **kwargs):
        calls.append(path)
        return {"response": {"order_list": [{"order_sn": "SN1", "order_status": "PROCESSED"}]}}
    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    with pytest.raises(HTTPException) as exc:
        await erp_shopee.proses_pengiriman(None, live, "SN1", {"metode": "dropoff"})
    assert exc.value.status_code == 409 and calls == [erp_shopee._PATH_ORDER_DETAIL]


@pytest.mark.asyncio
async def test_ship_timeout_is_not_retried_and_reports_uncertainty(live, monkeypatch):
    calls = []
    async def fake(session, akun, path, **kwargs):
        calls.append(path)
        if path == erp_shopee._PATH_ORDER_DETAIL:
            return {"response": {"order_list": [{"order_sn": "SN1", "order_status": "READY_TO_SHIP"}]}}
        if path == erp_shopee._PATH_SHIP_PARAM:
            return {"response": {"info_needed": {"dropoff": []}}}
        raise requests.Timeout("provider might have accepted shipment")
    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    with pytest.raises(HTTPException) as exc:
        await erp_shopee.proses_pengiriman(None, live, "SN1", {"metode": "dropoff"})
    assert "Sinkronkan" in exc.value.detail
    assert calls.count(erp_shopee._PATH_SHIP_ORDER) == 1


@pytest.mark.asyncio
async def test_explicit_dropoff_persists_method_across_sync_and_allows_label(session, monkeypatch):
    akun, pesanan, row = await _pesanan_sync(session)
    async def fake(session, akun, order_sn, pengaturan=None):
        assert pengaturan == {"metode": "dropoff"}
        return {"status_marketplace": "PROCESSED", "nomor_resi": None, "metode_pengiriman": "dropoff"}
    monkeypatch.setattr(erp_shopee, "proses_pengiriman", fake)
    out = await services.proses_pesanan_marketplace(session, pesanan.id, {"metode": "dropoff"})
    assert out.metode_pengiriman == "dropoff" and out.status == "to_ship"
    assert "gerai" in out.catatan_sinkron
    await services.impor_pesanan_marketplace(session, akun, [{**row, "status_mentah": "PROCESSED"}])
    assert out.metode_pengiriman == "dropoff"
    async def fake_label(*args, **kwargs):
        return b"%PDF-1.4 dropoff"
    monkeypatch.setattr(erp_shopee, "unduh_resi", fake_label)
    resp = await router.cetak_resi_pesanan(pesanan.id, tipe=None, session=session, user=SimpleNamespace(role="owner", id="u1"))
    assert resp.body.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_options_and_explicit_process_enforce_staff_shop_access(session, monkeypatch):
    _, pesanan, _ = await _pesanan_sync(session)
    staff = SimpleNamespace(role="staff", id="unassigned")
    async def forbidden(*args, **kwargs):
        pytest.fail("Shopee must not be contacted for unauthorized staff")
    monkeypatch.setattr(erp_shopee, "opsi_pengiriman", forbidden)
    monkeypatch.setattr(erp_shopee, "proses_pengiriman", forbidden)
    for endpoint in (router.opsi_pengiriman_pesanan, router.proses_pesanan_marketplace):
        with pytest.raises(HTTPException) as exc:
            await endpoint(pesanan.id, session=session, user=staff)
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_explicit_bulk_preserves_success_when_later_order_fails(session, monkeypatch):
    from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import ProsesMassalIn
    akun, pesanan, row = await _pesanan_sync(session)
    await services.impor_pesanan_marketplace(session, akun, [{**row, "id_eksternal": "SN2"}])
    await session.commit()
    orders = sorted(await services.list_pesanan(session, platform="shopee"), key=lambda p: p.id_eksternal)
    settings = {p.id: {"metode": "dropoff"} for p in orders}
    async def fake(session, akun, order_sn, pengaturan=None):
        assert pengaturan == {"metode": "dropoff"}
        if order_sn == "SN2":
            raise HTTPException(status_code=409, detail="Metode tidak tersedia")
        return {"status_marketplace": "PROCESSED", "nomor_resi": None, "metode_pengiriman": "dropoff"}
    monkeypatch.setattr(erp_shopee, "proses_pengiriman", fake)
    first_id = pesanan.id
    out = await router.proses_massal_pesanan(ProsesMassalIn(pesanan_ids=[p.id for p in orders], pengaturan=settings),
                                            session=session, user=SimpleNamespace(role="owner", id="u1"))
    assert (out["berhasil"], out["gagal"]) == (1, 1)
    session.expire_all()
    stored = await services.get_pesanan(session, first_id)
    assert stored.metode_pengiriman == "dropoff" and stored.status_marketplace == "PROCESSED"


def test_shipping_routes_expose_optional_single_body_and_bulk_settings():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router.marketplace_erp_router, prefix="/api/marketplace-erp")
    schema = app.openapi()
    paths = schema["paths"]
    body = paths["/api/marketplace-erp/pesanan/{pesanan_id}/proses"]["post"]["requestBody"]
    assert body.get("required", False) is False
    assert "/api/marketplace-erp/pesanan/{pesanan_id}/opsi-pengiriman" in paths
    assert "pengaturan" in schema["components"]["schemas"]["ProsesMassalIn"]["properties"]


def test_response_objects_do_not_advertise_unsupported_modes():
    param = {"info_needed": {"dropoff": []}, "pickup": PICKUP_PARAM["pickup"], "dropoff": {}}
    assert [o["metode"] for o in erp_shopee.opsi_parameter_kirim(param, "Toko")["opsi"]] == ["dropoff"]
    assert erp_shopee.pilih_parameter_kirim(param, "Toko") == {"dropoff": {}}
    with pytest.raises(HTTPException):
        erp_shopee.parameter_kirim_eksplisit(param, "Toko", {"metode": "pickup", "address_id": 1})


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [True, False])
async def test_multipackage_is_blocked_before_shipping_even_for_legacy_clients(live, monkeypatch, explicit):
    calls = []

    async def fake(session, akun, path, **kwargs):
        calls.append(path)
        assert kwargs["params"]["response_optional_fields"] == "package_list"
        return {"response": {"order_list": [{"order_sn": "SN1", "order_status": "READY_TO_SHIP",
                                              "package_list": [{"package_number": "P1"}, {"package_number": "P2"}]}]}}

    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    with pytest.raises(HTTPException) as exc:
        await erp_shopee.proses_pengiriman(None, live, "SN1", {"metode": "dropoff"} if explicit else None)
    assert "beberapa paket" in exc.value.detail
    assert calls == [erp_shopee._PATH_ORDER_DETAIL]


@pytest.mark.asyncio
async def test_retry_shipping_offers_only_pickup_and_uses_update_endpoint(live, monkeypatch):
    calls = []

    async def fake(session, akun, path, **kwargs):
        calls.append((path, kwargs))
        if path == erp_shopee._PATH_ORDER_DETAIL:
            return {"response": {"order_list": [{"order_sn": "SN1", "order_status": "RETRY_SHIP"}]}}
        if path == erp_shopee._PATH_SHIP_PARAM:
            return {"response": PICKUP_PARAM}
        return {}

    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    opsi = await erp_shopee.opsi_pengiriman(None, live, "SN1")
    assert opsi["aksi"] == "pickup_ulang" and [o["metode"] for o in opsi["opsi"]] == ["pickup"]
    setting = {"metode": "pickup", "address_id": 1, "pickup_time_id": "x1"}
    result = await erp_shopee.proses_pengiriman(None, live, "SN1", setting)
    assert result["metode_pengiriman"] == "pickup"
    update = next(kwargs for path, kwargs in calls if path == "/api/v2/logistics/update_shipping_order")
    assert update["body"] == {"order_sn": "SN1", "pickup": {"address_id": 1, "pickup_time_id": "x1"}}
    assert not any(path == erp_shopee._PATH_SHIP_ORDER for path, _ in calls)
    with pytest.raises(HTTPException):
        await erp_shopee.proses_pengiriman(None, live, "SN1", {"metode": "dropoff"})


@pytest.mark.asyncio
@pytest.mark.parametrize("response_error,expected", [(True, 424), (False, 409)])
async def test_ship_rejection_is_distinct_from_ambiguous_transport_failure(live, monkeypatch, response_error, expected):
    calls = []

    async def fake(session, akun, path, **kwargs):
        calls.append(path)
        if path == erp_shopee._PATH_ORDER_DETAIL:
            return {"response": {"order_list": [{"order_sn": "SN1", "order_status": "READY_TO_SHIP"}]}}
        if path == erp_shopee._PATH_SHIP_PARAM:
            return {"response": {"info_needed": {"dropoff": []}}}
        if response_error:
            raise erp_shopee.ShopeeAPIError(path, "invalid_status", "Not ready", "request-123")
        raise HTTPException(status_code=502, detail="Transport gagal: not a structured Shopee rejection")

    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    with pytest.raises(HTTPException) as exc:
        await erp_shopee.proses_pengiriman(None, live, "SN1", {"metode": "dropoff"})
    assert exc.value.status_code == expected
    assert calls.count(erp_shopee._PATH_SHIP_ORDER) == 1
    assert ("request-123" in exc.value.detail) == response_error


@pytest.mark.asyncio
async def test_http_error_with_json_success_shape_is_not_accepted(live, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: SimpleNamespace(
        ok=False, status_code=503, json=lambda: {"error": "", "response": {}}))
    with pytest.raises(HTTPException) as exc:
        await erp_shopee._call_shop_api(access_token="at", shop_id="5", api_path="/x")
    assert exc.value.status_code == 502


@pytest.mark.parametrize("body,mime,extension", [(b"%PDF-1.4 label", "application/pdf", "pdf"),
                                                (b"<!DOCTYPE html><html>label</html>", "text/html", "html")])
def test_label_format_and_response_headers(body, mime, extension):
    assert erp_shopee.format_dokumen_resi(body) == (mime, extension)
    response = router._respons_resi(body, f"resi.{extension}")
    assert response.media_type == mime
    assert response.headers["content-disposition"].startswith("inline" if extension == "pdf" else "attachment")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_unknown_label_body_is_not_served_as_pdf():
    with pytest.raises(HTTPException) as exc:
        erp_shopee.format_dokumen_resi(b"upstream error")
    assert exc.value.status_code == 424


@pytest.mark.asyncio
async def test_successful_pickup_reschedule_clears_old_print_mark(session, monkeypatch):
    from datetime import datetime, timezone

    _, pesanan, _ = await _pesanan_sync(session, mentah="RETRY_SHIP")
    pesanan.resi_dicetak_at = datetime.now(timezone.utc)
    pesanan.resi_dicetak_oleh = "old printer"

    async def fake(*args, **kwargs):
        return {"status_marketplace": "PROCESSED", "nomor_resi": None, "metode_pengiriman": "pickup"}

    monkeypatch.setattr(erp_shopee, "proses_pengiriman", fake)
    result = await services.proses_pesanan_marketplace(session, pesanan.id, {"metode": "pickup"})
    assert result.status_marketplace == "PROCESSED" and result.metode_pengiriman == "pickup"
    assert result.resi_dicetak_at is None and result.resi_dicetak_oleh is None


@pytest.mark.asyncio
@pytest.mark.parametrize("http_status,structured_rejection", [(200, True), (503, False)])
async def test_structured_error_retains_request_id_but_server_failure_remains_uncertain(live, monkeypatch, http_status, structured_rejection):
    monkeypatch.setattr(requests, "get", lambda *a, **k: SimpleNamespace(
        ok=http_status == 200, status_code=http_status,
        json=lambda: {"error": "invalid_status", "message": "Not ready", "request_id": "req-123"}))
    with pytest.raises(HTTPException) as exc:
        await erp_shopee._call_shop_api(access_token="at", shop_id="5", api_path="/x")
    assert isinstance(exc.value, erp_shopee.ShopeeAPIError) == structured_rejection
    if structured_rejection:
        assert exc.value.request_id == "req-123" and "req-123" in exc.value.detail


def test_zip_label_detected_and_downloaded_as_attachment():
    import io
    import zipfile

    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("label.html", "<html>AWB</html>")
    assert erp_shopee.format_dokumen_resi(content.getvalue()) == ("application/zip", "zip")
    response = router._respons_resi(content.getvalue(), "resi.zip")
    assert response.media_type == "application/zip"
    assert response.headers["content-disposition"] == 'attachment; filename="resi.zip"'
