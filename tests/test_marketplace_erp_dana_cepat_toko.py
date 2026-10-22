from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tenants.marketplace_erp.modules.marketplace_erp.application import financial_fast_escrow as fe
from tenants.marketplace_erp.modules.marketplace_erp.application.financial_reports import WIB


def test_default_keeps_bumi_tani_and_env_overrides(monkeypatch):
    monkeypatch.delenv("SHOPEE_DANA_CEPAT_TOKO", raising=False)
    assert fe.pakai_dana_cepat(SimpleNamespace(nama_toko="bumi  tani ind"))
    assert not fe.pakai_dana_cepat(SimpleNamespace(nama_toko="MAJAPAHIT STORE IND"))
    monkeypatch.setenv("SHOPEE_DANA_CEPAT_TOKO", "MAJAPAHIT STORE IND, Toko Baru")
    assert fe.pakai_dana_cepat(SimpleNamespace(nama_toko="Toko Baru"))
    assert not fe.pakai_dana_cepat(SimpleNamespace(nama_toko="BUMI TANI IND"))


@pytest.mark.asyncio
async def test_advanced_orders_lists_only_advances(monkeypatch):
    monkeypatch.delenv("SHOPEE_DANA_CEPAT_TOKO", raising=False)
    at = datetime(2026, 10, 20, tzinfo=WIB)

    async def fake(session, shop, start, now, ignored=None):
        return [dict(at=at, amount=Decimal("1000"), kind="advance", orders={"A"}),
                dict(at=at, amount=Decimal("-200"), kind="repayment", orders={"A"}),
                dict(at=at, amount=Decimal("500"), kind="escrow", orders={"B"})]

    monkeypatch.setattr(fe, "wallet_income", fake)
    out = await fe.advanced_orders(None, SimpleNamespace(nama_toko="BUMI TANI IND"), now=at)
    assert out == {"aktif": True, "pesanan": {"A": "1000"}, "tanpa_pesanan": 0}
    assert (await fe.advanced_orders(None, SimpleNamespace(nama_toko="X"), now=at)) == {"aktif": False, "pesanan": {}}
