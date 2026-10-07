"""Buyer decisions must never release stock on an unconfirmed cancellation."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1 import marketplace_erp_router as router
from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import (
    AkunMarketplaceIn, ItemPesananIn, PesananIn, PembatalanPembeliIn, ProdukIn,
)
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
    return SimpleNamespace(access_token="at", id_toko_eksternal="5")


async def order_with_stock(session):
    sku = await services.create_produk(session, ProdukIn(sku_induk="SKU", nama="Barang", stok=5, harga_dasar=Decimal("1000")))
    akun = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="Toko"))
    p = await services.create_pesanan(session, PesananIn(platform="shopee", akun_id=akun.id, id_eksternal="SN1",
        items=[ItemPesananIn(produk_id=sku.id, nama_produk="Barang", qty=2, harga_satuan=Decimal("1000"))]))
    await services.ubah_status_pesanan(session, p.id, "to_ship", dorong_marketplace=False)
    p.status_marketplace = "IN_CANCEL"
    await session.flush()
    return p, sku


@pytest.mark.asyncio
@pytest.mark.parametrize("operasi", ["ACCEPT", "REJECT"])
async def test_adapter_contract_and_remote_preflight(live, monkeypatch, operasi):
    calls = []
    async def fake(session, akun, path, **kwargs):
        calls.append((path, kwargs))
        if path == erp_shopee._PATH_ORDER_DETAIL:
            return {"response": {"order_list": [{"order_sn": "SN1", "order_status": "IN_CANCEL"}]}}
        return {"response": {"update_time": 1770000000}}
    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    await erp_shopee.tangani_pembatalan_pembeli(None, live, "SN1", operasi)
    assert calls[1] == (erp_shopee._PATH_BUYER_CANCEL, {"method": "POST", "body": {"order_sn": "SN1", "operation": operasi}})


@pytest.mark.asyncio
@pytest.mark.parametrize("orders", [[], [{"order_sn": "OTHER", "order_status": "IN_CANCEL"}], [{"order_sn": "SN1", "order_status": "READY_TO_SHIP"}]])
async def test_stale_or_missing_remote_order_never_sends_decision(live, monkeypatch, orders):
    calls = []
    async def fake(session, akun, path, **kwargs):
        calls.append(path)
        return {"response": {"order_list": orders}, "request_id": "read-id"}
    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    with pytest.raises(HTTPException):
        await erp_shopee.tangani_pembatalan_pembeli(None, live, "SN1", "ACCEPT")
    assert calls == [erp_shopee._PATH_ORDER_DETAIL]


@pytest.mark.asyncio
async def test_malformed_write_response_has_request_id(live, monkeypatch):
    async def fake(session, akun, path, **kwargs):
        if path == erp_shopee._PATH_ORDER_DETAIL:
            return {"response": {"order_list": [{"order_sn": "SN1", "order_status": "IN_CANCEL"}]}}
        return {"response": {}, "request_id": "write-id"}
    monkeypatch.setattr(erp_shopee, "signed_shop_request", fake)
    with pytest.raises(erp_shopee.ShopeeAPIError, match="write-id"):
        await erp_shopee.tangani_pembatalan_pembeli(None, live, "SN1", "ACCEPT")


@pytest.mark.asyncio
@pytest.mark.parametrize("operasi,mentah,expected_stock,expected_status", [
    ("ACCEPT", "CANCELLED", 5, "cancelled"),
    ("ACCEPT", "IN_CANCEL", 3, "to_ship"),
    ("REJECT", "READY_TO_SHIP", 3, "to_ship"),
])
async def test_stock_follows_confirmed_status_only(session, monkeypatch, operasi, mentah, expected_stock, expected_status):
    p, sku = await order_with_stock(session)
    async def write(*args):
        pass
    async def read(*args):
        return mentah
    monkeypatch.setattr(erp_shopee, "tangani_pembatalan_pembeli", write)
    monkeypatch.setattr(erp_shopee, "ambil_status_pesanan", read)
    result = await services.tangani_pembatalan_pembeli(session, p.id, operasi)
    assert result.status == expected_status and result.status_marketplace == mentah
    assert (await services.get_produk(session, sku.id)).stok == expected_stock
    if mentah == "CANCELLED":
        with pytest.raises(HTTPException) as exc:
            await services.tangani_pembatalan_pembeli(session, p.id, operasi)
        assert exc.value.status_code == 409
        assert (await services.get_produk(session, sku.id)).stok == 5


@pytest.mark.asyncio
async def test_failed_followup_read_keeps_success_and_reservation(session, monkeypatch):
    p, sku = await order_with_stock(session)
    async def write(*args):
        pass
    async def read(*args):
        raise erp_shopee.ShopeeAPIError("read", "error_permission", "Denied", "read-id")
    monkeypatch.setattr(erp_shopee, "tangani_pembatalan_pembeli", write)
    monkeypatch.setattr(erp_shopee, "ambil_status_pesanan", read)
    result = await services.tangani_pembatalan_pembeli(session, p.id, "ACCEPT")
    assert result.status_marketplace == "IN_CANCEL" and "jangan kirim keputusan ulang" in result.catatan_sinkron
    assert "read-id" in result.catatan_sinkron
    assert (await services.get_produk(session, sku.id)).stok == 3


@pytest.mark.asyncio
async def test_provider_rejection_and_shipping_guard_keep_stock(session, monkeypatch):
    p, sku = await order_with_stock(session)
    async def write(*args):
        raise erp_shopee.ShopeeAPIError("write", "error_permission", "Denied", "reject-id")
    monkeypatch.setattr(erp_shopee, "tangani_pembatalan_pembeli", write)
    with pytest.raises(HTTPException, match="reject-id"):
        await services.tangani_pembatalan_pembeli(session, p.id, "ACCEPT")
    with pytest.raises(HTTPException) as exc:
        await services.proses_pesanan_marketplace(session, p.id)
    assert exc.value.status_code == 409
    assert (await services.get_produk(session, sku.id)).stok == 3
    assert p.status_marketplace == "IN_CANCEL"


@pytest.mark.asyncio
async def test_unassigned_staff_cannot_submit_decision(session):
    p, _ = await order_with_stock(session)
    staff = SimpleNamespace(id="unassigned", role="staff")
    with pytest.raises(HTTPException) as exc:
        await router.tangani_pembatalan_pembeli(p.id, PembatalanPembeliIn(operasi="ACCEPT"), session, staff)
    assert exc.value.status_code == 403


def test_decision_schema_rejects_unknown_operation_and_extra_fields():
    for payload in [{"operasi": "accept"}, {"operasi": "ACCEPT", "status": "cancelled"}]:
        with pytest.raises(ValidationError):
            PembatalanPembeliIn(**payload)
