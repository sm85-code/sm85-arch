"""Time corrections use isolated synthetic jobs; never a configured ERP database."""
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import update

import test_marketplace_erp_processing_workflow as workflow_tests
from tenants.marketplace_erp.adapters.api.v1 import processing_workflow as workflow
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    ProcessingJob, Role, RoleHalaman, PeranTambahan,
)


client = workflow_tests.client


async def seed(factory, route="tukang", color="Putih", **overrides):
    data = dict(
        source="reseller", route=route, stage="siap_kirim", color=color, qty=2,
        production_price="100000", paint_price="20000", packing_price="5000",
        packing_type="kayu", packing_by="supplier", packing_party_name="Fixture packer",
        party_name="Fixture worker", customer="Synthetic reseller",
        production_done_at="2026-01-01T16:50:00+00:00" if route == "tukang" else None,
        received_at="2026-01-01T16:50:00+00:00" if route == "supplier" else None,
        painting_done_at="2026-01-01T18:00:00+00:00" if route == "tukang" and color != "Polos / Tanpa Cat" else None,
        packing_done_at="2026-01-02T00:00:00+00:00",
        events=[dict(action="selesai_packing", actor="fixture", at="2026-01-02T00:00:00+00:00")],
        retained={"snapshot": "unchanged"},
    )
    data.update(overrides)
    async with factory() as s:
        s.add(ProcessingJob(id="fixture", payload_json=json.dumps(data), version=1))
        await s.commit()
    return data


async def snapshot(factory):
    async with factory() as s:
        row = await s.get(ProcessingJob, "fixture")
        return row.version, json.loads(row.payload_json)


async def correct(http, stage="produksi", new_time="2026-01-02T00:10:00+07:00", **overrides):
    body = dict(version=1, stage=stage, new_time=new_time, reason="  Fixture correction  ")
    body.update(overrides)
    return await http.post("/processing/jobs/fixture/correct-time", json=body)


@pytest.mark.asyncio
@pytest.mark.parametrize("route,color,stage", [
    ("tukang", "Putih", "produksi"), ("tukang", "Putih", "pengecatan"),
    ("tukang", "Polos / Tanpa Cat", "produksi"), ("supplier", "", "supplier"),
    ("supplier", "", "packing"), ("tukang", "Putih", "packing"),
])
async def test_correction_retains_data_and_appends_exactly_one_audit(client, route, color, stage):
    http, _, factory = client
    before = await seed(factory, route, color)
    new_time = "2026-01-02T02:00:00+07:00" if stage == "packing" else (
        "2026-01-02T00:30:00+07:00" if stage == "pengecatan" else "2026-01-02T00:10:00+07:00"
    )
    response = await correct(http, stage, new_time)
    assert response.status_code == 200, response.text
    version, after = await snapshot(factory)
    field = workflow.TIMES[stage]
    assert version == 2
    event = after["events"][-1]
    assert set(event) == {"action", "actor", "at", "stage", "old_time", "new_time", "reason"}
    assert event == dict(action="koreksi_waktu", actor="admin", at=event["at"], stage=stage,
                         old_time=before[field], new_time=after[field], reason="Fixture correction")
    assert after[field] == datetime.fromisoformat(new_time).astimezone(timezone.utc).isoformat()
    assert after["events"][:-1] == before["events"]
    assert {k: v for k, v in after.items() if k not in (field, "events")} == {
        k: v for k, v in before.items() if k not in (field, "events")
    }
    assert (await correct(http, stage, new_time)).status_code == 409
    assert await snapshot(factory) == (version, after)


@pytest.mark.asyncio
@pytest.mark.parametrize("route,color,stage", [
    ("supplier", "Putih", "pengecatan"), ("supplier", "", "produksi"),
    ("tukang", "Polos / Tanpa Cat", "pengecatan"), ("tukang", "Putih", "supplier"),
])
async def test_rejects_inapplicable_stage_even_with_stale_completion_field(client, route, color, stage):
    http, _, factory = client
    await seed(factory, route, color, **{workflow.TIMES[stage]: "2026-01-01T17:00:00+00:00"})
    before = await snapshot(factory)
    assert (await correct(http, stage)).status_code == 422
    assert await snapshot(factory) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"new_time": "2026-01-02T00:10:00"}, {"new_time": "2026-01-01T17:10:00Z"},
    {"new_time": "2026-01-02T00:10:00+08:00"}, {"new_time": 1767287400},
    {"new_time": "invalid+07:00"}, {"new_time": "2099-01-01T00:00:00+07:00"},
    {"new_time": "2026-01-02T03:00:00+07:00"},  # after completed paint
    {"stage": "packing", "new_time": "2026-01-01T23:00:00+07:00"},
    {"reason": " \n "}, {"reason": "x" * 2001}, {"reason": None},
    {"stage": "draft"}, {"version": 0}, {"unexpected": "field"},
])
async def test_invalid_payload_never_mutates(client, overrides):
    http, _, factory = client
    await seed(factory)
    before = await snapshot(factory)
    assert (await correct(http, **overrides)).status_code == 422
    assert await snapshot(factory) == before


@pytest.mark.asyncio
async def test_missing_reason_unfinished_stage_and_missing_job(client):
    http, _, factory = client
    await seed(factory, production_done_at=None)
    assert (await correct(http)).status_code == 422
    assert (await http.post('/processing/jobs/fixture/correct-time', json={
        "version": 1, "stage": "packing", "new_time": "2026-01-02T02:00:00+07:00"
    })).status_code == 422
    assert (await http.post('/processing/jobs/missing/correct-time', json={
        "version": 1, "stage": "packing", "new_time": "2026-01-02T02:00:00+07:00", "reason": "Fixture"
    })).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("route,color", [("supplier", ""), ("tukang", "Polos / Tanpa Cat"), ("tukang", "Putih")])
async def test_nearest_completed_neighbors_and_equal_boundary(client, route, color):
    http, _, factory = client
    await seed(factory, route, color, painting_done_at=None)
    stage = "supplier" if route == "supplier" else "produksi"
    # Skip absent/inapplicable paint: packing still bounds the earlier stage.
    assert (await correct(http, stage, "2026-01-02T08:00:00+07:00")).status_code == 422
    assert (await correct(http, "packing", "2026-01-01T23:40:00+07:00")).status_code == 422
    assert (await correct(http, stage, "2026-01-02T07:00:00+07:00", reason="x" * 2000)).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("role,tab,extra,expected", [
    ("staff", "processing_tukang", None, 403),  # delegated write is insufficient
    ("custom", "processing_tukang", None, 403),
    ("owner", "processing_packing", None, 403),  # wrong tab
    ("owner", "processing_tukang", None, 200),
    ("staff", "processing_tukang", "owner", 200),  # actual secondary owner role
    ("owner", "processing_tukang", "read", 403),
])
async def test_actual_role_and_relevant_tab_required(client, role, tab, extra, expected):
    http, app, factory = client
    await seed(factory)
    async with factory() as s:
        for rid in {role, "owner"}:
            s.add(Role(id=rid, nama=rid, akses_toko="semua", sistem=False))
        s.add(RoleHalaman(role_id=role, halaman=tab, tingkat="baca" if extra == "read" else "tulis"))
        if extra == "owner":
            s.add(PeranTambahan(user_id="fixture-user", role_id="owner"))
        await s.commit()
    async def user():
        return SimpleNamespace(id="fixture-user", role=role)
    app.dependency_overrides[get_current_user_marketplace_erp] = user
    before = await snapshot(factory)
    response = await correct(http)
    assert response.status_code == expected, response.text
    if expected == 403:
        assert await snapshot(factory) == before


@pytest.mark.asyncio
async def test_persist_detects_race_without_losing_audit(client, monkeypatch):
    http, _, factory = client
    await seed(factory)
    original = workflow.persist
    async def racing_persist(session, row, *args, **kwargs):
        await session.execute(update(ProcessingJob).where(ProcessingJob.id == row.id).values(version=2)
                              .execution_options(synchronize_session=False))
        await session.commit()
        return await original(session, row, *args, **kwargs)
    monkeypatch.setattr(workflow, "persist", racing_persist)
    assert (await correct(http)).status_code == 409
    version, data = await snapshot(factory)
    assert version == 2 and len(data["events"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("route,stage", [("tukang", "produksi"), ("supplier", "supplier"), ("supplier", "packing")])
async def test_wage_recap_moves_across_wib_midnight_without_changing_totals(client, route, stage):
    http, _, factory = client
    await seed(factory, route, "Polos / Tanpa Cat", packing_done_at="2026-01-01T16:59:00+00:00")
    async def recap(start, end):
        async with factory() as s:
            return await workflow.biaya_produksi(s, date.fromisoformat(start), date.fromisoformat(end))
    before = await recap("2026-01-01", "2026-01-02")
    if stage != "packing":  # move packing first so predecessor correction stays chronologically valid
        assert (await correct(http, "packing", "2026-01-02T00:20:00+07:00")).status_code == 200
    response = await correct(http, stage, "2026-01-02T00:10:00+07:00", version=1 if stage == "packing" else 2)
    assert response.status_code == 200, response.text
    after = await recap("2026-01-01", "2026-01-02")
    assert sum(Decimal(r["jumlah"]) for r in before["pembayaran"]) == sum(Decimal(r["jumlah"]) for r in after["pembayaran"])
    assert sorted((r["pihak"], r["jumlah"]) for r in before["pembayaran"]) == sorted((r["pihak"], r["jumlah"]) for r in after["pembayaran"])
    moved = next(r for r in after["pembayaran"] if r["tahap"] == stage)
    assert moved["tanggal"] == "2026-01-02"
    assert not any(r["tahap"] == stage for r in (await recap("2026-01-01", "2026-01-01"))["pembayaran"])
    assert [{k: v for k, v in r.items() if k != "tanggal"} for r in before["tagihan"]] == [
        {k: v for k, v in r.items() if k != "tanggal"} for r in after["tagihan"]
    ]
