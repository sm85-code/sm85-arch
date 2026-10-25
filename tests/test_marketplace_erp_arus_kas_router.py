import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1.arus_kas_router import router
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import (
    MarketplaceErpBase,
    get_db_marketplace_erp,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import ProcessingParty


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


def client_for(role, session):
    app = FastAPI()
    app.include_router(router)

    async def db():
        yield session

    async def current():
        return SimpleNamespace(id="user", role=role)

    app.dependency_overrides[get_db_marketplace_erp] = db
    app.dependency_overrides[get_current_user_marketplace_erp] = current
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_owner_is_denied_everywhere(session):
    # Owner has no Arus Kas access by default; an admin may grant it in the role settings.
    async with client_for("owner", session) as client:
        for method, path in [
            ("GET", "/arus-kas/laporan?periode=2026-10"),
            ("GET", "/arus-kas/lembar?periode=2026-10"),
            ("PUT", "/arus-kas/lembar?periode=2026-10"),
            ("GET", "/arus-kas/rekening-tukang"),
        ]:
            assert (await client.request(method, path)).status_code == 403, path


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "staff"])
async def test_admin_and_staff_read_and_tick_but_cannot_change_figures(session, role):
    async with client_for(role, session) as client:
        assert (await client.get("/arus-kas/laporan?periode=2026-10")).status_code == 200
        first = (await client.get("/arus-kas/lembar?periode=2026-10")).json()
        assert first == {"version": 0, "lembar": {"sel": {}, "baris": [], "hapus": []}}

        tick = {"sel": {"ok:k_toko|2026-10-04": "1"}, "baris": [], "hapus": []}
        saved = await client.put("/arus-kas/lembar?periode=2026-10", json={"version": 0, "lembar": tick})
        assert saved.status_code == 200 and saved.json()["version"] == 1

        for lembar in [
            {"sel": {**tick["sel"], "rb:k_toko|2026-10-04": "5"}, "baris": [], "hapus": []},
            {"sel": tick["sel"], "baris": [{"id": "c:x", "tabel": "arus_rinci", "grup": "Kas masuk", "label": "Baris"}], "hapus": []},
            {"sel": tick["sel"], "baris": [], "hapus": ["k_toko"]},
        ]:
            denied = await client.put("/arus-kas/lembar?periode=2026-10", json={"version": 1, "lembar": lembar})
            assert denied.status_code == 403

        untick = await client.put("/arus-kas/lembar?periode=2026-10", json={"version": 1, "lembar": {"sel": {}, "baris": [], "hapus": []}})
        assert untick.status_code == 200


@pytest.mark.asyncio
async def test_staff_cannot_overwrite_figures_saved_by_admin(session):
    async with client_for("admin", session) as admin:
        await admin.put("/arus-kas/lembar?periode=2026-10", json={"version": 0, "lembar": {"sel": {}, "baris": [], "hapus": []}})
    from tenants.marketplace_erp.modules.marketplace_erp.application import financial_manual as manual

    typed = manual.Simpan(version=1, lembar=manual.Lembar(sel={"rb:k_toko|2026-10-04": "7"}))
    await manual.simpan(session, "2026-10", typed, SimpleNamespace(id="admin"))
    async with client_for("staff", session) as staff:
        stale = {"sel": {"ok:k_toko|2026-10-04": "1"}, "baris": [], "hapus": []}
        assert (await staff.put("/arus-kas/lembar?periode=2026-10", json={"version": 2, "lembar": stale})).status_code == 403
        keep = {"sel": {"rb:k_toko|2026-10-04": "7", "ok:k_toko|2026-10-04": "1"}, "baris": [], "hapus": []}
        assert (await staff.put("/arus-kas/lembar?periode=2026-10", json={"version": 2, "lembar": keep})).status_code == 200


@pytest.mark.asyncio
async def test_payment_accounts_list_only_craftsmen_with_an_account_number(session):
    for sid, payload in [
        ("a", {"nama": "Pak A", "nama_bank": " BRI ", "no_rekening": " 123 ", "catatan": "", "sumber": ""}),
        ("b", {"nama": "Pak B", "nama_bank": "BCA", "no_rekening": "  ", "catatan": "", "sumber": ""}),
        ("c", {"nama": "Pak C", "catatan": "", "sumber": ""}),
    ]:
        session.add(ProcessingParty(kind="tukang", source_id=sid, payload_json=json.dumps(payload), version=1))
    session.add(ProcessingParty(kind="supplier", source_id="s", payload_json=json.dumps({"nama": "Sup", "no_rekening": "9"}), version=1))
    await session.commit()
    async with client_for("staff", session) as client:
        rows = (await client.get("/arus-kas/rekening-tukang")).json()
    assert rows == [{"id": "a", "nama": "Pak A", "nama_bank": "BRI", "no_rekening": "123"}]


@pytest.mark.asyncio
async def test_rekening_daftar_admin_saves_staff_reads_only(session):
    body = {"lain": [{"id": "lain-iklan", "nama": "UMAR", "jenis": "Iklan", "bank": "BRI", "rekening": "123"}], "urutan": ["t1", "lain-iklan"]}
    async with client_for("staff", session) as client:
        assert (await client.get("/arus-kas/rekening-daftar")).json() is None
        assert (await client.put("/arus-kas/rekening-daftar", json=body)).status_code == 403
    async with client_for("admin", session) as client:
        assert (await client.put("/arus-kas/rekening-daftar", json=body)).status_code == 200
        dobel = {"lain": body["lain"] * 2, "urutan": []}
        assert (await client.put("/arus-kas/rekening-daftar", json=dobel)).status_code == 422
    async with client_for("staff", session) as client:
        assert (await client.get("/arus-kas/rekening-daftar")).json() == body
