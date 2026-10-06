"""Master protection and confirmed keu-only reset; live databases are never used."""
from datetime import datetime, timedelta, timezone

import httpx2 as httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select

import test_keu
from test_keu import order_payload
from shared.security import hash_password
from tenants.bumi_lestari.modules.bumi_lestari.application import keu_reset, keu_services as svc, schemas_keu as sc
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_keu as m
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlAuditLog, BlKategori
from tenants.bumi_lestari.adapters.api.v1.keu_router import router
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import get_current_user_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari

env = test_keu.env


@pytest.mark.asyncio
async def test_reset_preserves_masters_audit_and_legacy_and_is_one_use(env):
    s, u, sal, p, account, v = env
    u.password_hash = hash_password("Owner-reset-password")
    order = await svc.create_order(s, u, order_payload(sal, p))
    await svc.allocate_vendor(s, u, sc.AlokasiVendorIn(item_id=order["items"][0]["id"], vendor_id=v["vendor_id"], qty=1, biaya_satuan="5"))
    tx = await svc.transaction(s, u, sc.TransaksiIn(saluran_id=sal["id"], sumber_ref="tx", akun_id=account["id"], kategori_id="income", tanggal="2026-10-06", jenis="masuk", jumlah="10"))
    await svc.post_transaction(s, u, tx["id"])
    master_before = {name: (await svc.page(s, model))["rows"] for name, model in svc.MASTERS.items()}
    preview = await keu_reset.preview(s, u)
    assert preview["counts"]["keu_transaksi"] == 1 and preview["counts"]["keu_alokasi_vendor"] == 1
    payload = sc.ResetKeuIn(**{k: preview[k] for k in ["challenge_id", "token"]}, konfirmasi="RESET-KEUANGAN", password="Owner-reset-password")
    result = await keu_reset.execute(s, u, payload)
    assert result["direset"]
    for model in keu_reset.RESET_MODELS:
        assert (await s.execute(select(func.count()).select_from(model))).scalar_one() == 0
    assert master_before == {name: (await svc.page(s, model))["rows"] for name, model in svc.MASTERS.items()}
    assert (await s.execute(select(func.count()).select_from(BlKategori))).scalar_one() == 2
    assert (await s.execute(select(BlAuditLog).where(BlAuditLog.aksi == "reset-keu"))).scalar_one()
    with pytest.raises(HTTPException) as error:
        await keu_reset.execute(s, u, payload)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_reset_password_expiry_tamper_and_changed_inventory(env):
    s, u, sal, p, _, _ = env
    u.password_hash = hash_password("Correct-password")
    preview = await keu_reset.preview(s, u)
    payload = sc.ResetKeuIn(challenge_id=preview["challenge_id"], token=preview["token"], konfirmasi="RESET-KEUANGAN", password="Wrong")
    with pytest.raises(HTTPException) as error:
        await keu_reset.execute(s, u, payload)
    assert error.value.status_code == 403
    payload = payload.model_copy(update={"password": sc.ResetKeuIn.model_validate({**payload.model_dump(), "password": "Correct-password"}).password})
    with pytest.raises(HTTPException):
        await keu_reset.execute(s, u, payload.model_copy(update={"token": "x" * 64}))
    await svc.create_order(s, u, order_payload(sal, p))
    with pytest.raises(HTTPException) as error:
        await keu_reset.execute(s, u, payload)
    assert error.value.status_code == 409 and "Data berubah" in error.value.detail
    fresh = await keu_reset.preview(s, u)
    stored = await s.get(BlAuditLog, fresh["challenge_id"])
    stored.sesudah = {**stored.sesudah, "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
    await s.flush()
    with pytest.raises(HTTPException):
        await keu_reset.execute(s, u, sc.ResetKeuIn(challenge_id=fresh["challenge_id"], token=fresh["token"], konfirmasi="RESET-KEUANGAN", password="Correct-password"))
    assert (await svc.page(s, m.KeuPesanan))["total"] == 1
    u.role = "admin"
    with pytest.raises(HTTPException) as error:
        await keu_reset.preview(s, u)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_master_edit_delete_reference_protection_and_soft_delete(env):
    s, u, sal, p, account, v = env
    customer = await svc.create_master(s, u, "pelanggan", sc.PelangganIn(nama="Customer"))
    await svc.edit_master(s, u, "pelanggan", customer["id"], sc.PelangganIn(nama="Diedit", segmen="reseller", kontak="08123"))
    assert (await s.get(m.KeuPelanggan, customer["id"])).nama == "Diedit"
    await svc.delete_master(s, u, "pelanggan", customer["id"])
    assert await s.get(m.KeuPelanggan, customer["id"]) is None
    order = await svc.create_order(s, u, order_payload(sal, p))
    await svc.allocate_vendor(s, u, sc.AlokasiVendorIn(item_id=order["items"][0]["id"], vendor_id=v["vendor_id"], qty=1, biaya_satuan="5"))
    await svc.transaction(s, u, sc.TransaksiIn(saluran_id=sal["id"], sumber_ref="tx", akun_id=account["id"], kategori_id="income", tanggal="2026-10-06", jenis="masuk", jumlah="10"))
    for name, key in [("produk", p["id"]), ("vendor", v["vendor_id"]), ("akun", account["id"]), ("saluran", sal["id"])]:
        with pytest.raises(HTTPException) as error:
            await svc.delete_master(s, u, name, key)
        assert error.value.status_code == 409
        inactive = await svc.master_status(s, u, name, key, sc.MasterStatusIn(aktif=False))
        assert not inactive["aktif"]
    with pytest.raises(HTTPException):
        await svc.edit_master(s, u, "akun", account["id"], sc.AkunIn(kode="NEW", nama="Kas", jenis="kas", saldo_awal="1"))
    with pytest.raises(HTTPException):
        await svc.edit_master(s, u, "saluran", sal["id"], sc.SaluranIn(nama="Source", sistem="store", akun_ref="store"))


def test_reset_keyword_is_exact_and_password_is_redacted():
    values = {"challenge_id": "id", "token": "x" * 64, "konfirmasi": "RESET-KEUANGAN", "password": " secret "}
    payload = sc.ResetKeuIn.model_validate(values)
    assert payload.password.get_secret_value() == " secret " and " secret " not in repr(payload)
    with pytest.raises(ValueError):
        sc.ResetKeuIn.model_validate({**values, "konfirmasi": "reset-keuangan"})


@pytest.mark.asyncio
async def test_management_routes_validate_reset_and_edit_delete_masters(env):
    s, u, _, _, account, _ = env
    u.password_hash = hash_password("Owner-password")
    await s.commit()
    app = FastAPI()
    app.include_router(router, prefix="/api/bumi-lestari")
    async def db():
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise
    async def principal():
        return u
    app.dependency_overrides[get_db_bumi_lestari] = db
    app.dependency_overrides[get_current_user_bumi_lestari] = principal
    base = "/api/bumi-lestari/keu"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        edit = await client.patch(f'{base}/akun/{account["id"]}', json={"kode": "KAS", "nama": "Diedit", "jenis": "kas", "saldo_awal": "0"})
        assert edit.status_code == 200 and edit.json()["nama"] == "Diedit"
        created = await client.post(f"{base}/pelanggan", json={"nama": "Unused"})
        removed = await client.delete(f'{base}/master/pelanggan/{created.json()["id"]}')
        assert removed.status_code == 200 and removed.json()["dihapus"]
        assert (await client.patch(f'{base}/master/akun/{account["id"]}/status', json={"aktif": False})).status_code == 200
        preview = (await client.post(f"{base}/reset/pratinjau")).json()
        reset = await client.post(f"{base}/reset", json={"challenge_id": preview["challenge_id"], "token": preview["token"], "konfirmasi": "RESET-KEUANGAN", "password": "Owner-password"})
        assert reset.status_code == 200 and reset.json()["direset"]
        u.role = "admin"
        assert (await client.post(f"{base}/reset/pratinjau")).status_code == 403
