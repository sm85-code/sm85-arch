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
async def test_combined_mixed_label_formats_download_as_zip(session, monkeypatch):
    import io
    import zipfile

    akun = await _toko(session)
    html = await _pesanan(session, akun, "HTML", kurir="HTML Courier")
    pdf = await _pesanan(session, akun, "PDF", kurir="PDF Courier")

    async def fake_banyak(sess, akun_, pesanan, tipe=None):
        if pesanan[0][0] == "HTML":
            return b"<!doctype html><html>AWB</html>"
        return _pdf(1)

    monkeypatch.setattr(erp_shopee, "unduh_resi_banyak", fake_banyak)
    result = await services.unduh_resi_gabungan(session, [html.id, pdf.id])
    assert result["mime_type"] == "application/zip" and result["nama_file"].endswith(".zip")
    assert result["berhasil"] == 2 and result["gagal"] == []
    with zipfile.ZipFile(io.BytesIO(result["pdf"])) as archive:
        assert archive.namelist() == ["resi-1.html", "resi-2.pdf"]
        assert archive.read("resi-1.html").startswith(b"<!doctype html>")
    assert html.resi_dicetak_at is not None and pdf.resi_dicetak_at is not None


@pytest.mark.asyncio
async def test_single_html_label_uses_attachment_and_correct_filename(session, monkeypatch):
    akun = await _toko(session)
    order = await _pesanan(session, akun, "HTML")

    async def fake_label(*args, **kwargs):
        return b"<!doctype html><html>AWB</html>"

    monkeypatch.setattr(erp_shopee, "unduh_resi", fake_label)
    result = await router.cetak_resi_pesanan(order.id, tipe=None, session=session,
                                            user=SimpleNamespace(role="owner", id="u1"))
    assert result.media_type == "text/html"
    assert result.headers["content-disposition"] == 'attachment; filename="resi-HTML.html"'


@pytest.mark.asyncio
async def test_invalid_pdf_is_reported_without_marking_printed(session, monkeypatch):
    akun = await _toko(session)
    order = await _pesanan(session, akun, "BADPDF")

    async def fake_banyak(*args, **kwargs):
        return b"%PDF-1.4 corrupt"

    monkeypatch.setattr(erp_shopee, "unduh_resi_banyak", fake_banyak)
    with pytest.raises(HTTPException) as exc:
        await services.unduh_resi_gabungan(session, [order.id])
    assert "tidak valid" in exc.value.detail
    assert order.resi_dicetak_at is None


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


# --- gabungan: any mix of shops and couriers in ONE pdf --------------------------------------


def _pdf(halaman: int) -> bytes:
    import io

    from pypdf import PdfWriter

    w = PdfWriter()
    for _ in range(halaman):
        w.add_blank_page(width=100, height=100)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def _jumlah_halaman(pdf: bytes) -> int:
    import io

    from pypdf import PdfReader

    return len(PdfReader(io.BytesIO(pdf)).pages)


@pytest.mark.asyncio
async def test_a_mixed_selection_becomes_one_pdf_in_selection_order_and_is_marked_printed(session, monkeypatch):
    a, b = await _toko(session, "A"), await _toko(session, "B")
    b.id_toko_eksternal = "6"
    p1, p2 = await _pesanan(session, a, "S1", kurir="J&T"), await _pesanan(session, a, "S2", kurir="J&T")
    p3, p4 = await _pesanan(session, a, "S3", kurir="JNE"), await _pesanan(session, b, "S4", kurir="J&T")
    dipanggil = []

    async def fake_banyak(sess, akun_, pesanan, tipe=None):
        dipanggil.append((akun_.id, [sn for sn, _ in pesanan]))
        return _pdf(len(pesanan))

    monkeypatch.setattr(erp_shopee, "unduh_resi_banyak", fake_banyak)
    h = await services.unduh_resi_gabungan(session, [p1.id, p4.id, p3.id, p2.id], "NORMAL_AIR_WAYBILL", "budi")
    assert _jumlah_halaman(h["pdf"]) == 4 and h["berhasil"] == 4 and h["gagal"] == [] and h["jumlah_pdf"] == 3
    assert sorted(sorted(x[1]) for x in dipanggil) == [["S1", "S2"], ["S3"], ["S4"]]  # one call per shop and courier
    assert all(p.resi_dicetak_at and p.resi_dicetak_oleh == "budi" for p in (p1, p2, p3, p4))


@pytest.mark.asyncio
async def test_orders_not_ready_are_reported_and_never_marked_printed(session, monkeypatch):
    akun = await _toko(session)
    ok, belum = await _pesanan(session, akun, "S1"), await _pesanan(session, akun, "S2", mentah="READY_TO_SHIP")

    async def fake_banyak(sess, akun_, pesanan, tipe=None):
        return _pdf(len(pesanan))

    monkeypatch.setattr(erp_shopee, "unduh_resi_banyak", fake_banyak)
    h = await services.unduh_resi_gabungan(session, [ok.id, belum.id, "tidak-ada"])
    assert h["berhasil"] == 1 and _jumlah_halaman(h["pdf"]) == 1
    assert {g["id_eksternal"] for g in h["gagal"] if g["id_eksternal"]} == {"S2"} and len(h["gagal"]) == 2
    assert ok.resi_dicetak_at is not None and belum.resi_dicetak_at is None


@pytest.mark.asyncio
async def test_one_refused_order_does_not_block_the_rest_of_its_group(session, monkeypatch):
    akun = await _toko(session)
    p1 = await _pesanan(session, akun, "S1")
    p2 = await _pesanan(session, akun, "S2")
    p3 = await _pesanan(session, akun, "S3")

    async def fake_banyak(sess, akun_, pesanan, tipe=None):
        if any(sn == "S2" for sn, _ in pesanan):
            raise HTTPException(status_code=409, detail="Resi belum bisa dibuat: S2: error_status not ready")
        return _pdf(len(pesanan))

    monkeypatch.setattr(erp_shopee, "unduh_resi_banyak", fake_banyak)
    h = await services.unduh_resi_gabungan(session, [p1.id, p2.id, p3.id])
    assert h["berhasil"] == 2 and _jumlah_halaman(h["pdf"]) == 2
    assert [(g["id_eksternal"], "not ready" in g["pesan"]) for g in h["gagal"]] == [("S2", True)]
    assert p1.resi_dicetak_at and p3.resi_dicetak_at and p2.resi_dicetak_at is None


@pytest.mark.asyncio
async def test_when_nothing_could_be_printed_the_reasons_are_the_error(session, monkeypatch):
    akun = await _toko(session)
    p1 = await _pesanan(session, akun, "S1")

    async def fake_banyak(sess, akun_, pesanan, tipe=None):
        raise HTTPException(status_code=409, detail="Shopee menolak")

    monkeypatch.setattr(erp_shopee, "unduh_resi_banyak", fake_banyak)
    with pytest.raises(HTTPException) as exc:
        await services.unduh_resi_gabungan(session, [p1.id])
    assert exc.value.status_code == 409 and "S1" in exc.value.detail and "Shopee menolak" in exc.value.detail
    assert p1.resi_dicetak_at is None


@pytest.mark.asyncio
async def test_the_time_budget_stops_new_groups_and_says_so(session, monkeypatch):
    a, b = await _toko(session, "A"), await _toko(session, "B")
    b.id_toko_eksternal = "6"
    p1, p2 = await _pesanan(session, a, "S1"), await _pesanan(session, b, "S2")
    monkeypatch.setattr(services, "RESI_BATAS_TOTAL_DETIK", -1.0)  # already over budget: nothing may start

    async def fake_banyak(sess, akun_, pesanan, tipe=None):
        raise AssertionError("must not start")

    monkeypatch.setattr(erp_shopee, "unduh_resi_banyak", fake_banyak)
    with pytest.raises(HTTPException) as exc:
        await services.unduh_resi_gabungan(session, [p1.id, p2.id])
    assert "Waktu habis" in exc.value.detail


@pytest.mark.asyncio
async def test_gabungan_endpoint_returns_base64_pdf_with_the_failures_and_respects_staff_scope(session, monkeypatch):
    import base64

    from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import ResiGabunganIn

    akun = await _toko(session)
    p1 = await _pesanan(session, akun, "S1")

    async def fake_banyak(sess, akun_, pesanan, tipe=None):
        return _pdf(1)

    monkeypatch.setattr(erp_shopee, "unduh_resi_banyak", fake_banyak)
    hasil = await router.cetak_resi_gabungan(ResiGabunganIn(pesanan_ids=[p1.id]), session=session, user=SimpleNamespace(role="owner", id="o", username="o"))
    assert _jumlah_halaman(base64.b64decode(hasil["pdf"])) == 1 and hasil["nama_file"] == "resi-S1.pdf" and hasil["gagal"] == []
    with pytest.raises(HTTPException) as exc:
        await router.cetak_resi_gabungan(ResiGabunganIn(pesanan_ids=[p1.id]), session=session, user=SimpleNamespace(role="staff", id="s"))
    assert exc.value.status_code == 403
    with pytest.raises(ValidationError):
        ResiGabunganIn(pesanan_ids=[str(i) for i in range(201)])
