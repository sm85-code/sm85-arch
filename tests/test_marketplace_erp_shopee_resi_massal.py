"""One PDF with the labels of several Shopee orders (same shop and courier)."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1 import marketplace_erp_router as router
from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn, ResiMassalIn
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
    return SimpleNamespace(access_token="at", id_toko_eksternal="5", nama_toko="TES")


def _fake(calls, selectable=None, fail_param=None, statuses=("READY",)):
    selectable = selectable or {}
    seq = iter(statuses)

    async def fake(session, akun, path, *, method="GET", body=None, params=None, raw=False, timeout=None):
        calls.append((path, body))
        if path == erp_shopee._PATH_TRACKING:
            return {"response": {"tracking_number": f"R-{params['order_sn']}"}}
        if path == erp_shopee._PATH_DOC_PARAM:
            rows = []
            for o in body["order_list"]:
                row = {"order_sn": o["order_sn"], "suggest_shipping_document_type": "NORMAL_AIR_WAYBILL",
                       "selectable_shipping_document_type": selectable.get(o["order_sn"], ["NORMAL_AIR_WAYBILL", "THERMAL_AIR_WAYBILL"])}
                if o["order_sn"] == fail_param:
                    row.update(fail_error="error_status", fail_message="not ready")
                rows.append(row)
            return {"response": {"result_list": rows}}
        if path == erp_shopee._PATH_DOC_CREATE:
            return {"response": {"result_list": [{"order_sn": o["order_sn"]} for o in body["order_list"]]}}
        if path == erp_shopee._PATH_DOC_RESULT:
            st = next(seq)
            return {"response": {"result_list": [{"order_sn": o["order_sn"], "status": st} for o in body["order_list"]]}}
        assert path == erp_shopee._PATH_DOC_DOWNLOAD and raw is True
        return {"_bytes": b"%PDF-1.4 many"}

    return fake


# --- adapter -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unduh_resi_banyak_one_call_for_all_orders(live, monkeypatch):
    calls = []
    monkeypatch.setattr(erp_shopee, "signed_shop_request", _fake(calls, statuses=("PROCESSING", "READY")))
    pdf = await erp_shopee.unduh_resi_banyak(None, live, [("S1", "R1"), ("S2", None)])

    assert pdf.startswith(b"%PDF")
    by_path = {p: b for p, b in calls}
    assert [o["order_sn"] for o in by_path[erp_shopee._PATH_DOC_PARAM]["order_list"]] == ["S1", "S2"]
    create = by_path[erp_shopee._PATH_DOC_CREATE]["order_list"]
    assert [(o["order_sn"], o["shipping_document_type"], o["tracking_number"]) for o in create] == [
        ("S1", "THERMAL_AIR_WAYBILL", "R1"),
        ("S2", "THERMAL_AIR_WAYBILL", "R-S2"),  # missing tracking number fetched
    ]
    assert by_path[erp_shopee._PATH_DOC_DOWNLOAD]["shipping_document_type"] == "THERMAL_AIR_WAYBILL"
    assert len(by_path[erp_shopee._PATH_DOC_DOWNLOAD]["order_list"]) == 2
    assert [p for p, _ in calls].count(erp_shopee._PATH_DOC_RESULT) == 2  # waited for READY on both


@pytest.mark.asyncio
async def test_template_must_be_offered_for_every_order(live, monkeypatch):
    calls = []
    only_a4 = {"S2": ["NORMAL_AIR_WAYBILL"]}
    monkeypatch.setattr(erp_shopee, "signed_shop_request", _fake(calls, selectable=only_a4))
    await erp_shopee.unduh_resi_banyak(None, live, [("S1", "R1"), ("S2", "R2")])
    assert {o["shipping_document_type"] for o in next(b for p, b in calls if p == erp_shopee._PATH_DOC_CREATE)["order_list"]} == {
        "NORMAL_AIR_WAYBILL"
    }  # thermal is not available for S2, so the whole batch falls back

    with pytest.raises(HTTPException) as exc:
        await erp_shopee.unduh_resi_banyak(None, live, [("S1", "R1"), ("S2", "R2")], tipe="THERMAL_AIR_WAYBILL")
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_one_unprintable_order_fails_the_whole_batch(live, monkeypatch):
    monkeypatch.setattr(erp_shopee, "signed_shop_request", _fake([], fail_param="S2"))
    with pytest.raises(HTTPException) as exc:
        await erp_shopee.unduh_resi_banyak(None, live, [("S1", "R1"), ("S2", "R2")])
    assert exc.value.status_code == 409 and "S2" in exc.value.detail  # never a parcel silently without a label


@pytest.mark.asyncio
async def test_batch_size_is_checked(live):
    with pytest.raises(HTTPException) as exc:
        await erp_shopee.unduh_resi_banyak(None, live, [])
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        await erp_shopee.unduh_resi_banyak(None, live, [(f"S{i}", "R") for i in range(51)])


# --- service + endpoint --------------------------------------------------------------------


async def _toko(session, nama="A"):
    akun = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko=nama))
    akun.access_token, akun.id_toko_eksternal = "at", "5"
    await session.flush()
    return akun


async def _pesanan(session, akun, sn, mentah="PROCESSED", kurir="J&T", resi="R"):
    row = {"id_eksternal": sn, "status": "to_ship", "status_mentah": mentah, "nama_pembeli": "b", "total": Decimal("1"),
           "kurir": kurir, "nomor_resi": resi, "items": []}
    await services.impor_pesanan_marketplace(session, akun, [row])
    return next(p for p in await services.list_pesanan(session, platform="shopee") if p.id_eksternal == sn)


@pytest.mark.asyncio
async def test_service_prints_processed_orders_of_one_shop_and_courier(session, monkeypatch):
    akun = await _toko(session)
    p1, p2 = await _pesanan(session, akun, "S1", resi="R1"), await _pesanan(session, akun, "S2", resi="R2")
    asked = {}

    async def fake_banyak(sess, akun_, pesanan, tipe=None):
        asked.update(pesanan=pesanan, tipe=tipe)
        return b"%PDF-1.4 x"

    monkeypatch.setattr(erp_shopee, "unduh_resi_banyak", fake_banyak)
    pdf, nama = await services.unduh_resi_massal(session, [p1.id, p2.id, p1.id], "NORMAL_AIR_WAYBILL")
    assert pdf.startswith(b"%PDF") and nama == "resi-2-pesanan.pdf"
    assert asked == {"pesanan": [("S1", "R1"), ("S2", "R2")], "tipe": "NORMAL_AIR_WAYBILL"}  # duplicate id ignored


@pytest.mark.asyncio
async def test_service_refuses_unprocessed_and_mixed_selections(session, monkeypatch):
    akun, lain = await _toko(session, "A"), await _toko(session, "B")
    lain.id_toko_eksternal = "6"
    ok = await _pesanan(session, akun, "S1")
    belum = await _pesanan(session, akun, "S2", mentah="READY_TO_SHIP")
    jne = await _pesanan(session, akun, "S3", kurir="JNE")
    toko_b = await _pesanan(session, lain, "S4")

    async def never(*a, **k):
        raise AssertionError("must not call Shopee")

    monkeypatch.setattr(erp_shopee, "unduh_resi_banyak", never)
    with pytest.raises(HTTPException) as exc:
        await services.unduh_resi_massal(session, [ok.id, belum.id])
    assert exc.value.status_code == 409 and "S2" in exc.value.detail
    for pid in (jne.id, toko_b.id):
        with pytest.raises(HTTPException) as exc:
            await services.unduh_resi_massal(session, [ok.id, pid])
        assert exc.value.status_code == 409 and "per toko dan per kurir" in exc.value.detail


@pytest.mark.asyncio
async def test_endpoint_returns_pdf_and_respects_staff_scope(session, monkeypatch):
    akun = await _toko(session)
    p1 = await _pesanan(session, akun, "S1")

    async def fake_banyak(sess, akun_, pesanan, tipe=None):
        return b"%PDF-1.4 y"

    monkeypatch.setattr(erp_shopee, "unduh_resi_banyak", fake_banyak)
    resp = await router.cetak_resi_massal(ResiMassalIn(pesanan_ids=[p1.id]), session=session, user=SimpleNamespace(role="owner", id="o"))
    assert resp.media_type == "application/pdf" and 'filename="resi-S1.pdf"' in resp.headers["content-disposition"]

    with pytest.raises(HTTPException) as exc:  # staff without this shop assigned
        await router.cetak_resi_massal(ResiMassalIn(pesanan_ids=[p1.id]), session=session, user=SimpleNamespace(role="staff", id="s"))
    assert exc.value.status_code == 403


def test_schema_limits():
    assert ResiMassalIn(pesanan_ids=["a"], tipe="THERMAL_AIR_WAYBILL").tipe == "THERMAL_AIR_WAYBILL"
    with pytest.raises(ValidationError):
        ResiMassalIn(pesanan_ids=[])
    with pytest.raises(ValidationError):
        ResiMassalIn(pesanan_ids=["a"], tipe="OTHER")
    with pytest.raises(ValidationError):
        ResiMassalIn(pesanan_ids=[str(i) for i in range(51)])
