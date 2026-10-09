"""Tahap 2: stock reservation (prevent oversell) + OMS order pipeline."""
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import (
    AkunMarketplaceIn,
    ItemPesananIn,
    PesananIn,
    ProdukIn,
    StokAdjustIn,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import PengaturanStok
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import StokReservasi
from sqlalchemy import select


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        # This suite exercises the explicitly enabled warehouse workflow.
        s.add(PengaturanStok(id="global", gudang_aktif=True))
        await s.flush()
        yield s
    await engine.dispose()


async def _sku(session, sku="SKU-T2", stok=10):
    return await services.create_produk(
        session, ProdukIn(sku_induk=sku, nama="Barang", harga_dasar=Decimal("10000"), stok=stok)
    )


async def _akun(session):
    return await services.create_akun_marketplace(
        session, AkunMarketplaceIn(platform="shopee", nama_toko="Toko Shopee A")
    )


# --- Stock adjust / ledger ---------------------------------------------------


@pytest.mark.asyncio
async def test_create_produk_writes_initial_ledger(session):
    produk = await _sku(session, stok=5)
    assert produk.stok == 5
    ledger = await services.list_stok_ledger(session, produk_id=produk.id)
    assert len(ledger) == 1
    assert ledger[0].reason == "adjust"
    assert ledger[0].qty_delta == 5


@pytest.mark.asyncio
async def test_adjust_stok_updates_cache_and_ledger(session):
    produk = await _sku(session, stok=5)
    produk = await services.adjust_stok(
        session, StokAdjustIn(produk_id=produk.id, qty_delta=3, catatan="restock")
    )
    assert produk.stok == 8
    ledger = await services.list_stok_ledger(session, produk_id=produk.id)
    assert any(row.qty_delta == 3 and row.reason == "adjust" for row in ledger)


@pytest.mark.asyncio
async def test_adjust_stok_rejects_negative_available(session):
    produk = await _sku(session, stok=2)
    with pytest.raises(HTTPException) as exc:
        await services.adjust_stok(session, StokAdjustIn(produk_id=produk.id, qty_delta=-5))
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_patch_produk_rejects_direct_stok(session):
    from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import ProdukPatch

    produk = await _sku(session, stok=2)
    with pytest.raises(HTTPException) as exc:
        await services.update_produk(session, produk.id, ProdukPatch(stok=99))
    assert exc.value.status_code == 400


# --- Reservation / order pipeline --------------------------------------------


@pytest.mark.asyncio
async def test_to_ship_reserves_stock_and_prevents_oversell(session):
    produk = await _sku(session, stok=5)
    akun = await _akun(session)
    p1 = await services.create_pesanan(
        session,
        PesananIn(
            platform="shopee",
            id_eksternal="ORD-1",
            akun_id=akun.id,
            items=[
                ItemPesananIn(
                    nama_produk=produk.nama,
                    harga_satuan=produk.harga_dasar,
                    qty=3,
                    produk_id=produk.id,
                )
            ],
        ),
    )
    assert p1.status == "unpaid"
    assert (await services.get_produk(session, produk.id)).stok == 5

    p1 = await services.ubah_status_pesanan(session, p1.id, "to_ship")
    assert p1.status == "to_ship"
    assert (await services.get_produk(session, produk.id)).stok == 2

    # Second order needing 3 would oversell remaining 2
    p2 = await services.create_pesanan(
        session,
        PesananIn(
            platform="shopee",
            id_eksternal="ORD-2",
            akun_id=akun.id,
            items=[
                ItemPesananIn(
                    nama_produk=produk.nama,
                    harga_satuan=produk.harga_dasar,
                    qty=3,
                    produk_id=produk.id,
                )
            ],
        ),
    )
    with pytest.raises(HTTPException) as exc:
        await services.ubah_status_pesanan(session, p2.id, "to_ship")
    assert exc.value.status_code == 409
    assert (await services.get_produk(session, produk.id)).stok == 2


@pytest.mark.asyncio
async def test_cancel_releases_reservation(session):
    produk = await _sku(session, stok=4)
    akun = await _akun(session)
    pesanan = await services.create_pesanan(
        session,
        PesananIn(
            platform="shopee",
            id_eksternal="ORD-CANCEL",
            akun_id=akun.id,
            status="to_ship",
            items=[
                ItemPesananIn(
                    nama_produk=produk.nama,
                    harga_satuan=produk.harga_dasar,
                    qty=2,
                    produk_id=produk.id,
                )
            ],
        ),
    )
    assert pesanan.status == "to_ship"
    assert (await services.get_produk(session, produk.id)).stok == 2

    pesanan = await services.ubah_status_pesanan(session, pesanan.id, "cancelled")
    assert pesanan.status == "cancelled"
    assert (await services.get_produk(session, produk.id)).stok == 4

    rows = list(
        (
            await session.execute(select(StokReservasi).where(StokReservasi.pesanan_id == pesanan.id))
        ).scalars().all()
    )
    assert rows and all(r.status == "released" for r in rows)


@pytest.mark.asyncio
async def test_ship_consumes_reservation_without_restoring_stok(session):
    produk = await _sku(session, stok=4)
    akun = await _akun(session)
    pesanan = await services.create_pesanan(
        session,
        PesananIn(
            platform="shopee",
            id_eksternal="ORD-SHIP",
            akun_id=akun.id,
            status="to_ship",
            items=[
                ItemPesananIn(
                    nama_produk=produk.nama,
                    harga_satuan=produk.harga_dasar,
                    qty=2,
                    produk_id=produk.id,
                )
            ],
        ),
    )
    assert (await services.get_produk(session, produk.id)).stok == 2
    pesanan = await services.ubah_status_pesanan(session, pesanan.id, "shipped")
    assert pesanan.status == "shipped"
    assert (await services.get_produk(session, produk.id)).stok == 2  # not restored
    rows = list(
        (
            await session.execute(select(StokReservasi).where(StokReservasi.pesanan_id == pesanan.id))
        ).scalars().all()
    )
    assert rows and all(r.status == "consumed" for r in rows)


@pytest.mark.asyncio
async def test_order_pipeline_confirm_process_ship_complete(session):
    produk = await _sku(session, stok=1)
    akun = await _akun(session)
    pesanan = await services.create_pesanan(
        session,
        PesananIn(
            platform="shopee",
            id_eksternal="ORD-PIPE",
            akun_id=akun.id,
            items=[
                ItemPesananIn(
                    nama_produk=produk.nama,
                    harga_satuan=produk.harga_dasar,
                    qty=1,
                    produk_id=produk.id,
                )
            ],
        ),
    )
    pesanan = await services.ubah_status_pesanan(session, pesanan.id, "to_ship")
    # Soft-fail sync: no live Shopee → catatan_sinkron set, local status ok
    assert pesanan.status == "to_ship"
    assert pesanan.tersinkron_marketplace is False
    assert pesanan.catatan_sinkron

    pesanan = await services.ubah_status_pesanan(session, pesanan.id, "shipped")
    pesanan = await services.ubah_status_pesanan(session, pesanan.id, "completed")
    assert pesanan.status == "completed"


@pytest.mark.asyncio
async def test_list_pesanan_filters_by_platform_and_status(session):
    akun = await _akun(session)
    await services.create_pesanan(
        session,
        PesananIn(platform="shopee", id_eksternal="A", akun_id=akun.id, nama_pembeli="A"),
    )
    await services.create_pesanan(
        session,
        PesananIn(platform="lazada", id_eksternal="B", nama_pembeli="B"),
    )
    only_shopee = await services.list_pesanan(session, platform="shopee")
    assert len(only_shopee) == 1 and only_shopee[0].id_eksternal == "A"
    unpaid = await services.list_pesanan(session, status_filter="unpaid")
    assert len(unpaid) == 2


@pytest.mark.asyncio
async def test_duplicate_external_order_rejected(session):
    await services.create_pesanan(
        session, PesananIn(platform="shopee", id_eksternal="DUP-1", nama_pembeli="x")
    )
    with pytest.raises(HTTPException) as exc:
        await services.create_pesanan(
            session, PesananIn(platform="shopee", id_eksternal="DUP-1", nama_pembeli="y")
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_invalid_status_transition_rejected(session):
    pesanan = await services.create_pesanan(
        session, PesananIn(platform="blibli", id_eksternal="T", nama_pembeli="z")
    )
    with pytest.raises(HTTPException) as exc:
        await services.ubah_status_pesanan(session, pesanan.id, "completed")
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_default_gudang_created(session):
    rows = await services.list_gudang(session)
    assert any(g.kode == "DEFAULT" for g in rows)


@pytest.mark.asyncio
async def test_absolute_stock_adjustment_rejects_stale_snapshot(session):
    produk = await _sku(session, stok=5)
    await services.adjust_stok(session, StokAdjustIn(produk_id=produk.id, qty_delta=3))
    with pytest.raises(HTTPException) as exc:
        await services.adjust_stok(session, StokAdjustIn(produk_id=produk.id, qty_delta=5, expected_stock=5))
    assert exc.value.status_code == 409
    assert produk.stok == 8
    assert len(await services.list_stok_ledger(session, produk_id=produk.id)) == 2
