from datetime import date
from decimal import Decimal

import httpx2 as httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select

import test_keu

from tenants.bumi_lestari.adapters.api.v1.keu_router import router
from tenants.bumi_lestari.modules.bumi_lestari.application import keu_services as svc, schemas_keu as sc
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_keu as m
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import get_current_user_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlAuditLog, BlTutupBuku


env = test_keu.env
order_payload = test_keu.order_payload


async def cash(env, kind="masuk"):
    s, u, sal, _, account, _ = env
    payload = sc.TransaksiIn(saluran_id=sal["id"], sumber_ref="manual1", akun_id=account["id"],
        kategori_id="income" if kind == "masuk" else "expense", tanggal=date(2026, 10, 6), jenis=kind, jumlah="123.45")
    tx = await svc.transaction(s, u, payload)
    return await svc.post_transaction(s, u, tx["id"]), payload


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["masuk", "keluar"])
async def test_unpost_restores_balance_retains_snapshot_and_is_idempotent(env, kind):
    s, u, _, _, account, _ = env
    await svc.edit_master(s, u, "akun", account["id"], sc.AkunIn(kode="KAS", nama="Kas", jenis="kas", saldo_awal="500"))
    tx, original_payload = await cash(env, kind)
    assert Decimal((await svc.dashboard(s))["saldo_kas"]) == Decimal("500") + (Decimal("123.45") if kind == "masuk" else -Decimal("123.45"))
    result = await svc.unpost_transaction(s, u, tx["id"], sc.BatalIn(alasan="Koreksi bukti transaksi"))
    assert result["status"] == "dibatalkan"
    assert result["dibatalkan_oleh"] == u.id and result["dibatalkan_at"]
    for field in ["jumlah", "tanggal", "akun_id", "sumber_ref", "jenis"]:
        assert result[field] == tx[field]
    assert Decimal((await svc.dashboard(s))["saldo_kas"]) == 500
    assert (await svc.page(s, m.KeuTransaksi))["total"] == 0
    assert (await svc.page(s, m.KeuTransaksi, status="batal"))["total"] == 1
    again = await svc.unpost_transaction(s, u, tx["id"], sc.BatalIn(alasan="Alasan pengulangan berbeda"))
    assert {k: v for k, v in again.items() if k != "dibatalkan_at"} == {k: v for k, v in result.items() if k != "dibatalkan_at"}
    assert again["dibatalkan_at"].removesuffix("+00:00") == result["dibatalkan_at"].removesuffix("+00:00")
    assert len((await s.execute(select(BlAuditLog).where(BlAuditLog.aksi == "unpost"))).scalars().all()) == 1
    assert (await svc.transaction(s, u, original_payload))["status"] == "dibatalkan"
    with pytest.raises(HTTPException) as error:
        await svc.post_transaction(s, u, tx["id"])
    assert error.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("neto", ["18", "0", "-2"])
async def test_settlement_and_cash_cancel_atomically_including_zero_and_negative(env, neto):
    s, u, sal, product, account, _ = env
    order = await svc.create_order(s, u, order_payload(sal, product))
    payload = sc.SettlementIn(saluran_id=sal["id"], sumber_ref="settlement1", tanggal_cair=date(2026, 10, 6), bruto="20", potongan=str(Decimal("20")-Decimal(neto)), neto=neto)
    settlement = await svc.create_settlement(s, u, payload)
    await svc.allocate_settlement(s, u, sc.AlokasiSettlementIn(settlement_id=settlement["id"], item_id=order["items"][0]["id"], jumlah=neto))
    posting = sc.PostingSettlementIn(akun_id=account["id"], kategori_id="expense" if neto.startswith("-") else "income")
    await svc.post_settlement(s, u, settlement["id"], posting)
    assert Decimal((await svc.dashboard(s))["saldo_kas"]) == Decimal(neto)
    transactions = (await s.execute(select(m.KeuTransaksi))).scalars().all()
    if transactions:
        await svc.unpost_transaction(s, u, transactions[0].id, sc.BatalIn(alasan="Rekonsiliasi ulang settlement"))
    else:
        await svc.unpost_settlement(s, u, settlement["id"], sc.BatalIn(alasan="Rekonsiliasi ulang settlement"))
    assert all(t.status == "dibatalkan" for t in transactions)
    assert (await svc.get(s, m.KeuSettlement, settlement["id"])).status == "dibatalkan"
    assert Decimal((await svc.dashboard(s))["saldo_kas"]) == 0
    assert (await svc.page(s, m.KeuSettlement))["total"] == 0
    assert (await svc.page(s, m.KeuSettlement, status="batal"))["total"] == 1
    assert (await svc.page(s, m.KeuTransaksi, status="batal"))["total"] == len(transactions)
    assert (await svc.create_settlement(s, u, payload))["status"] == "dibatalkan"
    with pytest.raises(HTTPException):
        await svc.post_settlement(s, u, settlement["id"], posting)


@pytest.mark.asyncio
async def test_closed_period_and_draft_cannot_unpost(env):
    s, u, _, _, _, _ = env
    tx, payload = await cash(env)
    draft = await svc.transaction(s, u, payload.model_copy(update={"sumber_ref": "draft"}))
    with pytest.raises(HTTPException):
        await svc.unpost_transaction(s, u, draft["id"], sc.BatalIn(alasan="Koreksi draf"))
    s.add(BlTutupBuku(periode="2026-10", ditutup_oleh=u.id, snapshot={}))
    await s.flush()
    with pytest.raises(HTTPException) as error:
        await svc.unpost_transaction(s, u, tx["id"], sc.BatalIn(alasan="Koreksi terkunci"))
    assert error.value.status_code == 409
    assert (await svc.get(s, m.KeuTransaksi, tx["id"])).status == "terkirim"
    assert not (await s.execute(select(BlAuditLog).where(BlAuditLog.aksi == "unpost"))).first()


@pytest.mark.asyncio
async def test_unpost_api_reason_validation_roles_and_history(env):
    s, u, _, _, _, _ = env
    tx, _ = await cash(env)
    app = FastAPI()
    app.include_router(router)
    async def db():
        yield s
    async def actor():
        return u
    app.dependency_overrides[get_db_bumi_lestari] = db
    app.dependency_overrides[get_current_user_bumi_lestari] = actor
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = f"/keu/transaksi/{tx['id']}/unpost"
        for body in [{}, {"alasan": "  "}, {"alasan": "x"*2001}, {"alasan": "Valid", "jumlah": 1}]:
            assert (await client.post(path, json=body)).status_code == 422
        u.role = "tukang_kayu"
        assert (await client.post(path, json={"alasan": "Koreksi"})).status_code == 403
        u.role = "admin"
        u.must_change_password = True
        assert (await client.post(path, json={"alasan": "Koreksi"})).status_code == 403
        u.must_change_password = False
        assert (await client.post(path, json={"alasan": "Koreksi"})).status_code == 200
        assert (await client.get("/keu/transaksi")).json()["total"] == 0
        assert (await client.get("/keu/transaksi?status=batal")).json()["total"] == 1
