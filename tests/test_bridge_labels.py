import base64
import io
import json
import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from pypdf import PdfWriter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import selectinload
from tenants.marketplace_erp.modules.marketplace_erp.application import order_bridge as b, services, bridge_labels
from tenants.marketplace_erp.modules.marketplace_erp.application.bridge_presentation import enrich_orders
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import PesananOut
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import easyboss as eb
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import Pesanan, TokoPenjualan, BridgeOrder


@pytest_asyncio.fixture
async def setup(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        s.add(
            TokoPenjualan(
                jenis="internal",
                platform="tiktokshop",
                nama="Shop",
                nama_key="shop",
                sumber="easyboss",
                easyboss_shop_id="s",
                sinkron_status="synced",
                aktif=True,
                keterangan="",
            )
        )
        await s.commit()
        o = eb.Order(
            platform="tiktokshop",
            shop_id="s",
            order_id="original",
            package_ids=["pkg"],
            complete=True,
            paid=True,
            status="ready",
            source_status="AWAITING_COLLECTION",
            buyer="masked",
            ordered_at="2026-11-01T09:00:00+08:00",
            deadline="2026-11-09T23:59:00+08:00",
            items=[{"item_id": "i", "product": "Chair", "quantity": 1}],
        )
        assert await b.ingest(s, o) == "imported"
        await s.commit()
        p = await s.scalar(select(Pesanan).options(selectinload(Pesanan.items)))
        out = io.BytesIO()
        w = PdfWriter()
        w.add_blank_page(283, 425)
        w.write(out)
        data = {
            "platform": "tiktok",
            "shop_id": "s",
            "order_id": "original",
            "package_id": "pkg",
            "pdf_base64": base64.b64encode(out.getvalue()).decode(),
        }

        class Fake:
            async def call(self, name, args):
                assert name == "eb_resi_pdf" and args == {
                    "platform": "tiktok",
                    "shop_id": "s",
                    "no_pesanan": "original",
                    "package_id": "pkg",
                }
                return data

        monkeypatch.setattr(eb, "client", lambda: httpx.AsyncClient())
        monkeypatch.setattr(eb, "Gateway", lambda c: Fake())
        yield s, p, data
    await engine.dispose()


@pytest.mark.asyncio
async def test_single_official_label_marks_only_after_valid_pdf(setup):
    s, p, data = setup
    before = (p.id, p.status, p.detail_json)
    pdf, name = await services.unduh_resi_pesanan(s, p.id, oleh="staff")
    assert pdf.startswith(b"%PDF-") and name == "resi-original.pdf"
    assert p.resi_dicetak_at and p.resi_dicetak_oleh == "staff"
    assert before == (p.id, p.status, p.detail_json)
    await enrich_orders(s, [p])
    assert PesananOut.model_validate(p).resi_bridge_tersedia


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"order_id": "wrong"},
        {"package_id": "wrong"},
        {"pdf_base64": "not-pdf"},
        {"pdf_base64": base64.b64encode(b"%PDF-invalid").decode()},
    ],
)
async def test_bad_label_never_marked(setup, change):
    s, p, data = setup
    data.update(change)
    with pytest.raises(HTTPException) as e:
        await services.unduh_resi_pesanan(s, p.id)
    assert e.value.status_code == 424 and p.resi_dicetak_at is None


@pytest.mark.asyncio
async def test_unprocessed_order_cannot_print(setup):
    s, p, data = setup
    receipt = await s.scalar(select(BridgeOrder))
    receipt.source_status = "AWAITING_SHIPMENT"
    with pytest.raises(HTTPException) as e:
        await bridge_labels.download(s, p)
    assert e.value.status_code == 409 and p.resi_dicetak_at is None


@pytest.mark.asyncio
async def test_combined_bridge_label_and_duplicate_id(setup):
    s, p, data = setup
    r = await services.unduh_resi_gabungan(s, [p.id, p.id], oleh="staff")
    assert r["berhasil"] == 1 and not r["gagal"] and r["pdf"].startswith(b"%PDF-")
    assert p.resi_dicetak_at
    assert json.loads(p.detail_json)["easyboss"]["order_id"] == "original"
