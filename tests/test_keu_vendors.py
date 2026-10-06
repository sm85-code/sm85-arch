"""Unlimited vendor CRUD, legacy compatibility, history and authorization guards."""
from decimal import Decimal

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


@pytest.mark.asyncio
async def test_vendors_exceed_old_limits_without_slots_and_search_codes(env):
    s, u, sal, p, _, _ = env
    vendors = []
    for kind in ["kayu", "non_kayu"]:
        for index in range(12):
            vendors.append(await svc.create_master(s, u, "vendor", sc.VendorIn(nama=f"Vendor {kind} {index}", tipe=kind)))
    assert len({v["kode"] for v in vendors}) == 24
    assert all(v["status"] == "aktif" and v["kode"].startswith("VND-") for v in vendors)
    assert len(await svc.slots(s)) == 1  # Only the explicit legacy fixture, no fixed seed slots.
    chosen = next(v for v in vendors if v["tipe"] == "kayu")
    order = await svc.create_order(s, u, order_payload(sal, p))
    allocation = await svc.allocate_vendor(s, u, sc.AlokasiVendorIn(item_id=order["items"][0]["id"], vendor_id=chosen["id"], qty=1, biaya_satuan="5"))
    assert allocation["vendor_id"] == chosen["id"]
    result = await svc.page(s, m.KeuVendor, limit=10, offset=10)
    assert result["total"] == 25 and len(result["rows"]) == 10
    found = await svc.page(s, m.KeuVendor, search=chosen["kode"])
    assert [r["id"] for r in found["rows"]] == [chosen["id"]]


@pytest.mark.asyncio
async def test_inactive_vendor_blocks_new_work_but_preserves_history_and_cost(env):
    s, u, sal, p, _, _ = env
    vendor = await svc.create_master(s, u, "vendor", sc.VendorIn(kode="TK-MANUAL", nama="Dinamis", tipe="kayu", kontak="08123", alamat="Jl. Kayu", keterangan="Produksi"))
    order = await svc.create_order(s, u, order_payload(sal, p))
    allocation = await svc.allocate_vendor(s, u, sc.AlokasiVendorIn(item_id=order["items"][0]["id"], vendor_id=vendor["id"], qty=1, biaya_satuan="5"))
    updated = await svc.save_vendor(s, u, vendor["id"], sc.VendorEditIn(nama="Nama baru", kontak="08999", alamat="Alamat baru"))
    assert updated["kode"] == "TK-MANUAL" and updated["id"] == vendor["id"]
    await svc.deactivate_vendor(s, u, vendor["id"])
    inactive = await svc.deactivate_vendor(s, u, vendor["id"])
    assert inactive["status"] == "non_aktif" and not inactive["aktif"]
    other = await svc.create_order(s, u, order_payload(sal, p, ref="o2"))
    with pytest.raises(HTTPException) as error:
        await svc.allocate_vendor(s, u, sc.AlokasiVendorIn(item_id=other["items"][0]["id"], vendor_id=vendor["id"], qty=1, biaya_satuan="5"))
    assert error.value.status_code == 422
    assert Decimal((await svc.dashboard(s))["biaya_vendor"]) == 5
    stored = await s.get(m.KeuAlokasiVendor, allocation["id"])
    assert stored.vendor_id == vendor["id"] and not stored.dibatalkan
    with pytest.raises(HTTPException) as error:
        await svc.save_vendor(s, u, vendor["id"], sc.VendorEditIn(tipe="non_kayu"))
    assert error.value.status_code == 409
    # Cancelling history also cannot reinterpret the recorded vendor kind.
    await svc.cancel_allocation(s, u, allocation["id"], sc.BatalIn(alasan="Koreksi alokasi"))
    with pytest.raises(HTTPException):
        await svc.save_vendor(s, u, vendor["id"], sc.VendorEditIn(tipe="non_kayu"))
    await svc.save_vendor(s, u, vendor["id"], sc.VendorEditIn(aktif=True))
    again = await svc.allocate_vendor(s, u, sc.AlokasiVendorIn(item_id=other["items"][0]["id"], vendor_id=vendor["id"], qty=1, biaya_satuan="5"))
    assert again["vendor_id"] == vendor["id"]
    logs = (await s.execute(select(BlAuditLog).where(BlAuditLog.entitas == "keu_vendor", BlAuditLog.entitas_id == vendor["id"]))).scalars().all()
    assert any(row.sesudah.get("aktif") is False for row in logs)


@pytest.mark.asyncio
async def test_unused_legacy_vendor_can_change_type_without_losing_identity(env):
    s, u, _, _, _, legacy = env
    edited = await svc.save_vendor(s, u, legacy["vendor_id"], sc.VendorEditIn(tipe="non_kayu"))
    assert edited["id"] == legacy["vendor_id"] and edited["jenis"] == "supplier"
    assert all(row["vendor_id"] is None for row in await svc.slots(s))


@pytest.mark.parametrize("payload", [
    {"nama": " ", "tipe": "kayu"}, {"nama": "N", "tipe": "invalid"},
    {"nama": "N", "tipe": "kayu", "kode": " "}, {"nama": "N", "tipe": "kayu", "kode": "x" * 129},
    {"nama": "N", "tipe": "kayu", "alamat": "x" * 2001}, {"nama": "N", "tipe": "kayu", "aktif": "false"},
    {"nama": "N", "tipe": "kayu", "jenis": "supplier"},
])
def test_invalid_vendor_inputs(payload):
    with pytest.raises(ValueError):
        sc.VendorIn.model_validate(payload)


@pytest.mark.parametrize("payload", [{}, {"nama": None}, {"aktif": None}, {"kode": ""}, {"kontak": None}])
def test_invalid_vendor_updates(payload):
    with pytest.raises(ValueError):
        sc.VendorEditIn.model_validate(payload)


@pytest.mark.asyncio
async def test_vendor_crud_api_unique_code_rollback_and_guards(env):
    session, user, _, _, _, _ = env
    await session.commit()
    app = FastAPI()
    app.include_router(router, prefix="/api/bumi-lestari")
    async def db():
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    async def principal():
        return user
    app.dependency_overrides[get_db_bumi_lestari] = db
    app.dependency_overrides[get_current_user_bumi_lestari] = principal
    base = "/api/bumi-lestari/keu/vendor"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(base, json={"kode": "SUP-CODE", "nama": "Supplier", "tipe": "non_kayu", "alamat": "Alamat"})
        assert created.status_code == 201, created.text
        key = created.json()["id"]
        duplicate = await client.post(base, json={"kode": "SUP-CODE", "nama": "Duplikat", "tipe": "kayu"})
        assert duplicate.status_code == 409 and "INSERT" not in duplicate.text
        # Reload after rollback, matching a fresh authenticated request.
        user = await session.get(type(user), "owner")
        detail = await client.get(f"{base}/{key}")
        assert detail.status_code == 200 and detail.json()["tipe"] == "non_kayu"
        edited = await client.patch(f"{base}/{key}", json={"nama": "Diedit", "keterangan": "Catatan"})
        assert edited.status_code == 200 and edited.json()["kode"] == "SUP-CODE"
        removed = await client.delete(f"{base}/{key}")
        assert removed.status_code == 200 and removed.json()["status"] == "non_aktif"
        assert (await client.get(f"{base}/{key}")).status_code == 200
        assert (await client.patch(f"{base}/{key}", json={"aktif": True})).json()["aktif"]
        assert (await client.patch(f"{base}/missing", json={"aktif": False})).status_code == 404
        user = await session.get(type(user), "owner")
        for role, must_change in [("staff", False), ("owner", True)]:
            user.role, user.must_change_password = role, must_change
            for method, path, body in [("GET", base, None), ("POST", base, {"nama": "Blocked", "tipe": "kayu"}), ("PATCH", f"{base}/{key}", {"aktif": False}), ("DELETE", f"{base}/{key}", None)]:
                assert (await client.request(method, path, json=body)).status_code == 403
