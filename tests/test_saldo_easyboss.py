from decimal import Decimal
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from tenants.marketplace_erp.modules.marketplace_erp.application import saldo_easyboss as s
from tenants.marketplace_erp.adapters.api.v1 import saldo_easyboss_router as r


def row(n="1", **kw):
    return dict(
        no_pesanan=n,
        platform="tiktok",
        shopId="shop",
        toko="Toko",
        status_platform="DELIVERED",
        sudah_cair=False,
        perkiraan_dana_cair=0.1,
        nilai_pesanan=100,
        **kw,
    )


def source(rows, complete=True):
    return {"pesanan": rows, "lengkap": complete}


def test_exclude_paid_cancel_return_unknown_and_shopee():
    rows = [row()]
    for field, value in [
        ("sudah_cair", True),
        ("status_platform", "CANCELLED"),
        ("status_platform", "RETURNED"),
        ("status_platform", "AWAITING_COLLECTION"),
        ("status_platform", ""),
        ("platform", "shopee"),
    ]:
        item = row(str(len(rows) + 1))
        item[field] = value
        rows.append(item)
    out = s.projection([source(rows)])
    assert out["jumlah"] == 1 and out["total"] == Decimal("0.1")


def test_duplicates_conflicts_and_missing():
    first = row()
    other = {**first, "perkiraan_dana_cair": 0.2}
    assert s.projection([source([first, first])])["total"] == Decimal("0.1")
    out = s.projection([source([first, other])])
    assert out["jumlah"] == 1 and out["tanpa_angka"] == 1
    assert out["pesanan"][0]["perkiraan_dana_cair"] is None
    assert out["total"] is None
    assert out["total_platform"]["tiktok"] is None
    assert out["per_toko"][0]["total"] is None


def test_filters_totals_and_pagination():
    rows = [row(str(n)) for n in range(101)]
    rows.append({**row("L"), "platform": "lazada", "shopId": "L", "toko": "Lazada"})
    out = s.projection([source(rows)], page=2, page_size=100)
    assert out["jumlah"] == 102 and len(out["pesanan"]) == 2
    assert sum(x["total"] for x in out["per_toko"]) == out["total"] == Decimal("10.2")
    assert s.projection([source(rows)], platform="lazada")["jumlah"] == 1
    assert s.projection([source(rows)], toko="L")["jumlah"] == 1
    assert s.projection([source(rows)], status="Dalam pengiriman")["jumlah"] == 0
    assert s.projection([source(rows)], search="L")["jumlah"] == 1
    assert s.projection([source(rows, False)])["lengkap"] is False
    assert s.projection([source([])])["jumlah"] == 0


@pytest.mark.asyncio
async def test_strict_admin_even_when_custom_role_has_page(monkeypatch):
    async def roles(session, user):
        return [user.role]

    monkeypatch.setattr(r.akses, "peran_pengguna", roles)
    assert await r.admin(SimpleNamespace(role="admin"), None)
    for role in ("owner", "staff", "custom"):
        with pytest.raises(HTTPException) as err:
            await r.admin(SimpleNamespace(role=role), None)
        assert err.value.status_code == 403


@pytest.mark.asyncio
async def test_background_singleflight_cache_and_backoff(monkeypatch):
    monkeypatch.setattr(s, "_cache", None)
    monkeypatch.setattr(s, "_task", None)
    monkeypatch.setattr(s, "_error", None)
    calls = []

    async def load():
        calls.append(1)
        s._cache = (s.time.monotonic(), [source([])])

    monkeypatch.setattr(s, "_load", load)
    assert await s.snapshot() is None
    assert await s.snapshot() is None
    await s._task
    assert await s.snapshot() == [source([])] and len(calls) == 1
    monkeypatch.setattr(s, "_cache", None)
    monkeypatch.setattr(s, "_error", "Sesi kedaluwarsa")
    monkeypatch.setattr(s, "_error_at", s.time.monotonic())
    with pytest.raises(HTTPException) as err:
        await s.snapshot()
    assert err.value.status_code == 503


def test_completed_without_settlement_evidence_is_uncertain():
    item = {**row(), 'status_platform': 'COMPLETED'}
    out = s.projection([source([item])])
    assert out['jumlah'] == 0 and out['total'] == 0
    assert len(out['perlu_verifikasi']) == 1
