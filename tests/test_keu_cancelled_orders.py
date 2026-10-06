"""Cancellation history and financial exclusion without deleting ledger records."""
import httpx2 as httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select

import test_keu
from test_keu import order_payload
from tenants.bumi_lestari.adapters.api.v1.keu_router import router
from tenants.bumi_lestari.modules.bumi_lestari.application import keu_services as svc, schemas_keu as sc
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_keu as m
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import get_current_user_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlAuditLog

env = test_keu.env


def settlement(sal, ref="settled", number="cancel"):
    return sc.SettlementIn(saluran_id=sal["id"], sumber_ref=ref, tanggal_cair="2026-09-03", bruto="20", potongan="2", neto="18", rincian={"order_sn": number})


@pytest.mark.asyncio
async def test_cancel_hides_items_financial_totals_and_remains_history(env):
    s, user, sal, product, _, vendor = env
    payload = order_payload(sal, product, "cancel")
    payload.items[0].produk_id = None
    order = await svc.create_order(s, user, payload)
    incoming = await svc.create_settlement(s, user, settlement(sal), from_source=True)
    assert (await svc.dashboard(s))["settlement_draf"] == 1
    with pytest.raises(HTTPException):
        await svc.order_status(s, user, order["id"], sc.PesananStatusIn(status="batal", alasan=""))
    cancelled = await svc.order_status(s, user, order["id"], sc.PesananStatusIn(status="batal", alasan="Pesanan diabaikan"))
    assert cancelled["status"] == "batal"
    assert (await svc.order_status(s, user, order["id"], sc.PesananStatusIn(status="batal", alasan="Pesanan diabaikan")))["status"] == "batal"
    assert (await svc.page(s, m.KeuPesanan))["total"] == (await svc.page(s, m.KeuItem))["total"] == 0
    history = await svc.page(s, m.KeuPesanan, search="cancel", status="batal")
    assert history["total"] == 1 and len(history["rows"][0]["items"]) == 1
    assert history["rows"][0]["items"][0]["harga_satuan"] == "10.00"
    assert (await svc.page(s, m.KeuItem, status="batal"))["total"] == 1
    assert (await svc.page(s, m.KeuSettlement))["total"] == 0
    dashboard = await svc.dashboard(s)
    assert dashboard["pesanan"] == dashboard["belum_dipetakan"] == dashboard["settlement_draf"] == 0
    assert float(dashboard["nilai_pesanan"]) == float(dashboard["biaya_vendor"]) == float(dashboard["saldo_kas"]) == 0
    assert (await s.execute(select(BlAuditLog).where(BlAuditLog.entitas == "keu_pesanan", BlAuditLog.aksi == "status"))).scalar_one().alasan == "Pesanan diabaikan"
    for action in [svc.allocate_vendor(s, user, sc.AlokasiVendorIn(item_id=order["items"][0]["id"], vendor_id=vendor["vendor_id"], qty=1, biaya_satuan="5")),
                   svc.allocate_settlement(s, user, sc.AlokasiSettlementIn(settlement_id=incoming["id"], item_id=order["items"][0]["id"], jumlah="18")),
                   svc.order_status(s, user, order["id"], sc.PesananStatusIn(status="aktif"))]:
        with pytest.raises(HTTPException):
            await action
    # A new source revision cannot reactivate the ignored local order.
    revised = payload.model_copy(update={"status_sumber": "completed"})
    changed = revised.model_dump()
    changed["items"][0].update(qty=3, subtotal_sumber="30")
    changed["total_sumber"] = "30"
    result = await svc.create_order(s, user, sc.PesananIn.model_validate(changed), from_source=True)
    assert result["status"] == "batal" and result["total_sumber"] == "20.00" and result["items"][0]["qty"] == 2
    assert (await svc.create_settlement(s, user, settlement(sal, "new-source"), from_source=True))["diabaikan"]
    assert len((await s.execute(select(m.KeuSettlement))).scalars().all()) == 1
    with pytest.raises(HTTPException):
        await svc.create_settlement(s, user, settlement(sal, "manual"))


@pytest.mark.asyncio
async def test_cancel_rejects_vendor_and_settlement_allocations(env):
    s, user, sal, product, _, vendor = env
    order = await svc.create_order(s, user, order_payload(sal, product, "allocated"))
    allocation = await svc.allocate_vendor(s, user, sc.AlokasiVendorIn(item_id=order["items"][0]["id"], vendor_id=vendor["vendor_id"], qty=1, biaya_satuan="5"))
    cancellation = sc.PesananStatusIn(status="batal", alasan="Diabaikan")
    with pytest.raises(HTTPException):
        await svc.order_status(s, user, order["id"], cancellation)
    await svc.cancel_allocation(s, user, allocation["id"], sc.BatalIn(alasan="Koreksi alokasi"))
    incoming = await svc.create_settlement(s, user, settlement(sal, number="allocated"))
    await svc.allocate_settlement(s, user, sc.AlokasiSettlementIn(settlement_id=incoming["id"], item_id=order["items"][0]["id"], jumlah="18"))
    with pytest.raises(HTTPException):
        await svc.order_status(s, user, order["id"], cancellation)
    assert (await s.get(m.KeuPesanan, order["id"])).status == "draf"


@pytest.mark.asyncio
async def test_posted_source_reference_prevents_cancellation(env):
    s, user, sal, product, account, _ = env
    original = await svc.create_order(s, user, order_payload(sal, product, "original"))
    other = await svc.create_order(s, user, order_payload(sal, product, "other"))
    incoming = await svc.create_settlement(s, user, settlement(sal, number="original"))
    await svc.allocate_settlement(s, user, sc.AlokasiSettlementIn(settlement_id=incoming["id"], item_id=other["items"][0]["id"], jumlah="18"))
    await svc.post_settlement(s, user, incoming["id"], sc.PostingSettlementIn(akun_id=account["id"], kategori_id="income"))
    with pytest.raises(HTTPException) as error:
        await svc.order_status(s, user, original["id"], sc.PesananStatusIn(status="batal", alasan="Koreksi"))
    assert error.value.status_code == 409
    assert (await svc.dashboard(s))["kas_masuk"] == "18.00"


@pytest.mark.asyncio
async def test_inconsistent_legacy_cancelled_links_excluded_and_posting_blocked(env):
    s, user, sal, product, account, vendor = env
    order = await svc.create_order(s, user, order_payload(sal, product, "legacy"))
    await svc.allocate_vendor(s, user, sc.AlokasiVendorIn(item_id=order["items"][0]["id"], vendor_id=vendor["vendor_id"], qty=2, biaya_satuan="5"))
    incoming = await svc.create_settlement(s, user, settlement(sal, number="legacy"))
    await svc.allocate_settlement(s, user, sc.AlokasiSettlementIn(settlement_id=incoming["id"], item_id=order["items"][0]["id"], jumlah="18"))
    # Simulate inconsistent historic DB data; the API normally rejects this cancellation.
    row = await s.get(m.KeuPesanan, order["id"])
    row.status = "batal"
    await s.flush()
    with pytest.raises(HTTPException) as error:
        await svc.post_settlement(s, user, incoming["id"], sc.PostingSettlementIn(akun_id=account["id"], kategori_id="income"))
    assert error.value.status_code == 409
    s.add(m.KeuTransaksi(saluran_id=sal["id"], sumber_ref="legacy-cash", settlement_id=incoming["id"], akun_id=account["id"], kategori_id="income", tanggal=row.tanggal,
        jenis="masuk", jumlah=18, status="terkirim", dibuat_oleh=user.id))
    s.add(m.KeuTransaksi(saluran_id=sal["id"], sumber_ref="draft-cash", settlement_id=incoming["id"], akun_id=account["id"], kategori_id="income", tanggal=row.tanggal,
        jenis="masuk", jumlah=18, status="draf", dibuat_oleh=user.id))
    await s.flush()
    draft = (await s.execute(select(m.KeuTransaksi).where(m.KeuTransaksi.sumber_ref == "draft-cash"))).scalar_one()
    with pytest.raises(HTTPException):
        await svc.post_transaction(s, user, draft.id)
    expense = await svc.transaction(s, user, sc.TransaksiIn(saluran_id=sal["id"], sumber_ref="unrelated", akun_id=account["id"], kategori_id="expense", tanggal=row.tanggal, jenis="keluar", jumlah="3"))
    await svc.post_transaction(s, user, expense["id"])
    dashboard = await svc.dashboard(s)
    assert dashboard["pesanan"] == dashboard["belum_dipetakan"] == dashboard["settlement_draf"] == 0
    assert float(dashboard["nilai_pesanan"]) == float(dashboard["biaya_vendor"]) == float(dashboard["kas_masuk"]) == 0
    assert float(dashboard["saldo_kas"]) == -3
    for model in [m.KeuAlokasiVendor, m.KeuAlokasiSettlement, m.KeuSettlement]:
        assert (await svc.page(s, model))["total"] == 0
    assert (await svc.page(s, m.KeuTransaksi))["total"] == 1


@pytest.mark.asyncio
async def test_cancelled_reference_is_scoped_to_channel(env):
    s, user, sal, product, _, _ = env
    order = await svc.create_order(s, user, order_payload(sal, product, "same"))
    await svc.order_status(s, user, order["id"], sc.PesananStatusIn(status="batal", alasan="Abaikan"))
    other_channel = await svc.create_master(s, user, "saluran", sc.SaluranIn(nama="Other", sistem="manual", akun_ref="other", aktif=True))
    await svc.create_settlement(s, user, settlement(other_channel, number="same"), from_source=True)
    assert (await svc.page(s, m.KeuSettlement))["total"] == 1


@pytest.mark.asyncio
async def test_api_cancel_filter_pagination_and_role_guard(env):
    s, user, sal, product, _, _ = env
    first = await svc.create_order(s, user, order_payload(sal, product, "cancel"))
    await svc.create_order(s, user, order_payload(sal, product, "keep"))
    app = FastAPI()
    app.include_router(router)
    async def db():
        yield s
    async def principal():
        return user
    app.dependency_overrides[get_db_bumi_lestari] = db
    app.dependency_overrides[get_current_user_bumi_lestari] = principal
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = f'/keu/pesanan/{first["id"]}/status'
        user.role = "staff"
        assert (await client.post(path, json={"status": "batal", "alasan": "Abaikan"})).status_code == 403
        user.role = "owner"
        response = await client.post(path, json={"status": "batal", "alasan": "Abaikan"})
        assert response.status_code == 200 and response.json()["status"] == "batal"
        current = (await client.get("/keu/pesanan")).json()
        assert current["total"] == 1 and current["rows"][0]["nomor"] == "keep"
        history = (await client.get("/keu/pesanan", params={"status": "batal", "limit": 1, "offset": 0, "search": "cancel"})).json()
        assert history["total"] == 1 and history["rows"][0]["status"] == "batal"
        assert not (await client.get("/keu/pesanan", params={"status": "batal", "offset": 1})).json()["rows"]
        assert (await client.get("/keu/pesanan", params={"status": "semua"})).json()["total"] == 2
        assert (await client.get("/keu/pesanan", params={"status": "invalid"})).status_code == 422
        assert (await client.get(f'/keu/pesanan/{first["id"]}')).json()["status"] == "batal"
