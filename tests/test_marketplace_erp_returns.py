from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.adapters.api.v1 import marketplace_erp_router as router
from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.schemas import AkunMarketplaceIn, PesananIn, ProdukIn, ReturOut
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee_returns as adapter
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters.erp_shopee import ShopeeAPIError
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import PengaturanStok
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase

ROW = {"return_sn": "RET1", "order_sn": "SN1", "status": "REQUESTED", "currency": "SGD", "refund_amount": 0,
       "return_solution": 1, "needs_logistics": False, "item": [{"item_id": 123, "model_id": 0, "name": "Barang", "amount": 1, "item_price": 10.50}]}


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        # This suite exercises the explicitly enabled warehouse workflow.
        s.add(PengaturanStok(id="global", gudang_aktif=True))
        await s.flush()
        yield s
    await engine.dispose()


def test_date_range_uses_full_wib_days_and_limit():
    start, end = adapter.rentang_retur(date(2026, 10, 1), date(2026, 10, 15))
    assert datetime.fromtimestamp(start, timezone.utc).isoformat() == "2026-09-30T17:00:00+00:00"
    assert end - start == 15 * 86400 - 1
    for dates in [(date(2026, 10, 1), date(2026, 10, 16)), (date(2026, 10, 3), date(2026, 10, 2))]:
        with pytest.raises(HTTPException) as exc:
            adapter.rentang_retur(*dates)
        assert exc.value.status_code == 422


def test_projection_preserves_zero_null_and_unknown_status():
    out = adapter.normalisasi_retur({**ROW, "status": "NEW_PROVIDER_STATUS"})
    assert out["status"] == "NEW_PROVIDER_STATUS" and out["nominal_refund"] == Decimal("0")
    assert out["perlu_pengembalian_barang"] is False
    assert out["items"][0]["model_id"] == "0" and out["items"][0]["nominal_refund"] is None
    assert out["items"][0]["harga"] == Decimal("10.5")
    parsed = ReturOut(**out, akun_id="shop", platform="shopee", nama_toko="Shop")
    assert parsed.model_dump(mode="json")["nominal_refund"] == "0"


@pytest.mark.asyncio
async def test_list_contract_retains_more_without_auto_fetching(monkeypatch):
    calls = []
    async def fake(session, akun, path, *, params):
        calls.append((path, params))
        return {"response": {"return": [ROW], "more": True}}
    monkeypatch.setattr(adapter, "signed_shop_request", fake)
    out = await adapter.daftar_retur(None, None, date(2026, 10, 1), date(2026, 10, 2), 2, 40)
    assert out["halaman"] == 2 and out["ada_lagi"] is True
    assert len(calls) == 1 and calls[0][0] == adapter.PATH_LIST
    assert calls[0][1]["page_no"] == 2 and calls[0][1]["page_size"] == 40
    assert "update_time_from" not in calls[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{}, {"return": [], "more": True}, {"return": [ROW], "more": "false"},
    {"return": [ROW, ROW], "more": False}, {"return": [{**ROW, "status": ""}], "more": False},
    {"return": [{**ROW, "refund_amount": "NaN"}], "more": False}, {"return": [{**ROW, "create_time": "bad"}], "more": False}])
async def test_incomplete_pages_are_errors_not_empty_success(monkeypatch, response):
    async def fake(*args, **kwargs):
        return {"response": response, "request_id": "broken-page"}
    monkeypatch.setattr(adapter, "signed_shop_request", fake)
    with pytest.raises(ShopeeAPIError, match="broken-page"):
        await adapter.daftar_retur(None, None, date(2026, 10, 1), date(2026, 10, 2))


@pytest.mark.asyncio
async def test_detail_is_scoped_to_exact_requested_return_and_keeps_provider_errors(monkeypatch):
    async def fake(*args, **kwargs):
        assert kwargs["params"] == {"return_sn": "RET1"}
        return {"response": {**ROW, "return_sn": "OTHER"}, "request_id": "wrong-detail"}
    monkeypatch.setattr(adapter, "signed_shop_request", fake)
    with pytest.raises(ShopeeAPIError, match="wrong-detail"):
        await adapter.detail_retur(None, None, "RET1")
    async def denied(*args, **kwargs):
        raise ShopeeAPIError(adapter.PATH_DETAIL, "error_permission", "Denied", "permission-id")
    monkeypatch.setattr(adapter, "signed_shop_request", denied)
    with pytest.raises(ShopeeAPIError, match="permission-id"):
        await adapter.detail_retur(None, None, "RET1")


@pytest.mark.asyncio
async def test_order_link_is_shop_scoped_and_reads_never_change_inventory(session, monkeypatch):
    shop = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="A"))
    other = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="B"))
    p = await services.create_pesanan(session, PesananIn(platform="shopee", akun_id=other.id, id_eksternal="SN1", items=[]))
    sku = await services.create_produk(session, ProdukIn(sku_induk="SKU", nama="Barang", stok=9, harga_dasar=Decimal("1000")))
    async def fake(*args, **kwargs):
        return {"response": {"return": [ROW], "more": False}}
    monkeypatch.setattr(adapter, "signed_shop_request", fake)
    result = await services.daftar_retur_marketplace(session, shop.id, date(2026, 10, 1), date(2026, 10, 2))
    assert result["items"][0]["pesanan_id"] is None
    result = await services.daftar_retur_marketplace(session, other.id, date(2026, 10, 1), date(2026, 10, 2))
    assert result["items"][0]["pesanan_id"] == p.id
    assert (await services.get_produk(session, sku.id)).stok == 9 and p.status == "unpaid"
    assert len(await services.list_stok_ledger(session, produk_id=sku.id)) == 1


@pytest.mark.asyncio
async def test_unassigned_staff_blocked_before_remote_list_and_detail(session, monkeypatch):
    shop = await services.create_akun_marketplace(session, AkunMarketplaceIn(platform="shopee", nama_toko="A"))
    async def unexpected(*args, **kwargs):
        pytest.fail("Remote API must not be reached")
    monkeypatch.setattr(adapter, "signed_shop_request", unexpected)
    staff = SimpleNamespace(id="unassigned", role="staff")
    with pytest.raises(HTTPException) as exc:
        await router.daftar_retur_marketplace(shop.id, date(2026, 10, 1), date(2026, 10, 2), 1, 40, session, staff)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        await router.detail_retur_marketplace(shop.id, "RET1", session, staff)
    assert exc.value.status_code == 403
