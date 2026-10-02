"""'Already printed' mark on shipping labels, so the same parcel is not printed twice."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1 import marketplace_erp_router as router
from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import (
    AkunMarketplaceIn,
    PesananOut,
    ResiMassalIn,
    TandaiResiIn,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase

OWNER = SimpleNamespace(role="owner", id="o1", nama="Budi", email="budi@x.id")


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _pesanan(session, akun, sn, mentah="PROCESSED"):
    row = {"id_eksternal": sn, "status": "to_ship", "status_mentah": mentah, "nama_pembeli": "b", "total": Decimal("1"),
           "kurir": "J&T", "nomor_resi": f"R-{sn}", "items": []}
    await services.impor_pesanan_marketplace(session, akun, [row])
    return next(p for p in await services.list_pesanan(session, platform="shopee") if p.id_eksternal == sn)


@pytest_asyncio.fixture
async def akun(session):
    a = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="TES"))
    a.access_token, a.id_toko_eksternal = "at", "5"
    await session.flush()
    return a


@pytest.mark.asyncio
async def test_single_print_marks_time_and_user_only_when_it_worked(session, akun, monkeypatch):
    p = await _pesanan(session, akun, "S1")
    assert p.resi_dicetak_at is None and p.resi_dicetak_oleh is None

    async def boom(*a, **k):
        raise HTTPException(status_code=409, detail="Resi belum siap")

    monkeypatch.setattr(erp_shopee, "unduh_resi", boom)
    with pytest.raises(HTTPException):
        await router.cetak_resi_pesanan(p.id, tipe=None, session=session, user=OWNER)
    assert (await services.get_pesanan(session, p.id)).resi_dicetak_at is None  # failed -> not marked

    async def ok(*a, **k):
        return b"%PDF-1.4 x"

    monkeypatch.setattr(erp_shopee, "unduh_resi", ok)
    await router.cetak_resi_pesanan(p.id, tipe=None, session=session, user=OWNER)
    out = await services.get_pesanan(session, p.id)
    assert out.resi_dicetak_at is not None and out.resi_dicetak_oleh == "Budi"
    assert PesananOut.model_validate(out).resi_dicetak_oleh == "Budi"  # exposed to the UI


@pytest.mark.asyncio
async def test_reprint_updates_the_mark_to_the_latest_printer(session, akun, monkeypatch):
    p = await _pesanan(session, akun, "S1")

    async def ok(*a, **k):
        return b"%PDF-1.4 x"

    monkeypatch.setattr(erp_shopee, "unduh_resi", ok)
    await router.cetak_resi_pesanan(p.id, tipe=None, session=session, user=OWNER)
    await router.cetak_resi_pesanan(p.id, tipe=None, session=session, user=SimpleNamespace(role="owner", id="o2", email="sari@x.id"))
    assert (await services.get_pesanan(session, p.id)).resi_dicetak_oleh == "sari@x.id"  # no name -> email


@pytest.mark.asyncio
async def test_bulk_print_marks_every_order_in_the_batch(session, akun, monkeypatch):
    p1, p2, p3 = await _pesanan(session, akun, "S1"), await _pesanan(session, akun, "S2"), await _pesanan(session, akun, "S3")

    async def ok(sess, akun_, pesanan, tipe=None):
        return b"%PDF-1.4 many"

    monkeypatch.setattr(erp_shopee, "unduh_resi_banyak", ok)
    ids = [p1.id, p2.id, p3.id]
    await router.cetak_resi_massal(ResiMassalIn(pesanan_ids=ids[:2]), session=session, user=OWNER)
    marks = {pid: (await services.get_pesanan(session, pid)).resi_dicetak_at for pid in ids}
    assert marks[ids[0]] and marks[ids[1]] and marks[ids[2]] is None  # the third was not in the batch

    async def boom(*a, **k):
        raise HTTPException(status_code=409, detail="x")

    monkeypatch.setattr(erp_shopee, "unduh_resi_banyak", boom)
    with pytest.raises(HTTPException):
        await router.cetak_resi_massal(ResiMassalIn(pesanan_ids=[ids[2]]), session=session, user=OWNER)
    assert (await services.get_pesanan(session, ids[2])).resi_dicetak_at is None


@pytest.mark.asyncio
async def test_manual_mark_and_unmark(session, akun):
    p = await _pesanan(session, akun, "S1")
    out = await router.tandai_resi_dicetak(p.id, TandaiResiIn(), session=session, user=OWNER)
    assert out.resi_dicetak_at is not None and out.resi_dicetak_oleh == "Budi"
    out = await router.tandai_resi_dicetak(p.id, TandaiResiIn(dicetak=False), session=session, user=OWNER)
    assert out.resi_dicetak_at is None and out.resi_dicetak_oleh is None


@pytest.mark.asyncio
async def test_mark_rejects_typed_in_orders_and_staff_without_the_shop(session, akun):
    manual = await services.create_pesanan(session, services.PesananIn(platform="shopee", id_eksternal="M1", akun_id=akun.id, items=[]))
    with pytest.raises(HTTPException) as exc:
        await router.tandai_resi_dicetak(manual.id, TandaiResiIn(), session=session, user=OWNER)
    assert exc.value.status_code == 409

    p = await _pesanan(session, akun, "S1")
    with pytest.raises(HTTPException) as exc:
        await router.tandai_resi_dicetak(p.id, TandaiResiIn(), session=session, user=SimpleNamespace(role="staff", id="s9"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_sync_does_not_clear_the_printed_mark(session, akun, monkeypatch):
    p = await _pesanan(session, akun, "S1")
    await services.tandai_resi_pesanan(session, p.id, True, "Budi")
    row = {"id_eksternal": "S1", "status": "shipped", "status_mentah": "SHIPPED", "nama_pembeli": "b", "total": Decimal("1"),
           "kurir": "J&T", "nomor_resi": "R-S1", "items": []}
    await services.impor_pesanan_marketplace(session, akun, [row])
    out = await services.get_pesanan(session, p.id)
    assert out.status == "shipped" and out.resi_dicetak_oleh == "Budi"
