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
    item = {**row(), "status_platform": "COMPLETED"}
    out = s.projection([source([item])])
    assert out["jumlah"] == 0 and out["total"] == 0
    assert len(out["perlu_verifikasi"]) == 1


@pytest.mark.asyncio
async def test_expired_snapshot_stays_visible_during_refresh(monkeypatch):
    import asyncio

    sources = [source([])]
    monkeypatch.setattr(s, "_cache", (s.time.monotonic() - 601, sources))
    monkeypatch.setattr(s, "_task", None)
    monkeypatch.setattr(s, "_error", None)
    gate = asyncio.Event()
    monkeypatch.setattr(s, "_load", gate.wait)
    assert await s.snapshot() is sources
    task = s._task
    assert s.refresh_state()["refreshing"] is True
    assert s.refresh_state()["stale"] is True
    assert await s.snapshot() is sources and s._task is task
    gate.set()
    await task


def test_snapshot_survives_process_restart(monkeypatch, tmp_path):
    import json

    path = tmp_path / "saldo.json"
    monkeypatch.setenv("ERP_EASYBOSS_CACHE_FILE", str(path))
    sources = [source([]), source([])]
    s._persist(sources)
    assert path.stat().st_mode & 0o077 == 0
    assert json.loads(path.read_text())["sources"] == sources
    monkeypatch.setattr(s, "_cache", None)
    s._restore()
    assert s._cache[1] == sources


@pytest.mark.asyncio
async def test_unexpected_source_failure_does_not_loop_loading(monkeypatch):
    async def fail():
        raise TypeError("unexpected source schema")

    monkeypatch.setattr(s, "_fetch_sources", fail)
    monkeypatch.setattr(s, "_error", None)
    monkeypatch.setattr(s, "_error_at", 0)
    await s._load()
    assert s._error and s._error_at > 0


@pytest.mark.asyncio
async def test_refresh_deadline_is_bounded(monkeypatch):
    import asyncio

    original_timeout = asyncio.timeout
    monkeypatch.setattr(s.asyncio, "timeout", lambda seconds: original_timeout(0.01))
    monkeypatch.setattr(s, "_fetch_sources", lambda: asyncio.sleep(10))
    monkeypatch.setattr(s, "_error", None)
    monkeypatch.setattr(s, "_error_at", 0)
    await s._load()
    assert s._error and s._error_at > 0


@pytest.mark.asyncio
async def test_stale_snapshot_still_available_when_source_fails(monkeypatch):
    sources = [source([])]
    monkeypatch.setattr(s, "_cache", (s.time.monotonic() - 601, sources))
    monkeypatch.setattr(s, "_error", "Source unavailable")
    monkeypatch.setattr(s, "_error_at", s.time.monotonic())
    assert await s.snapshot() is sources
    assert s.refresh_state()["refresh_error"] == "Source unavailable"
