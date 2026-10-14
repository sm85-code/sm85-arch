from datetime import date, datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.marketplace_erp.modules.marketplace_erp.application import financial_reports as f
from tenants.marketplace_erp.adapters.api.v1.financial_reports_router import router, save_opening
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import (
    MarketplaceErpBase,
    get_db_marketplace_erp,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    AkunMarketplace,
    Settlement,
    Pesanan,
    IklanHarianToko,
    LaporanKeuanganTutup,
    LaporanKeuanganAudit,
)


def balances(**extra):
    data = {k: D(0) for k in f.Balances.model_fields}
    data.update(extra)
    return f.Balances(**data)


def payload(**extra):
    data = dict(
        tanggal=date(2020, 10, 4),
        piutang_pengiriman=0,
        saldo_toko=0,
        stok=0,
        utang_usaha=0,
        catatan="Sumber dan saldo telah diperiksa",
        sumber_diperiksa=True,
    )
    data.update(extra)
    if "transaksi" not in data:
        data["transaksi"] = [
            {"tanggal": data["tanggal"], "jenis": k, "nominal": v}
            for k, v in data.items()
            if k in (*f.CASH_FIELDS, "biaya_iklan") and v is not None
        ]
    if "tanggal_settlement" not in data:
        data["tanggal_settlement"] = {sid: data["tanggal"] for sid in data.get("settlement_ids", [])}
    return f.CloseInput(**data)


def test_bumi_lestari_two_weeks_and_monthly_reconciliation():
    b = balances(
        kas_utama=5725000,
        stok=3585000,
        kas_operasional=3000000,
        iklan_prabayar=2000000,
        saldo_toko=5520000,
        modal=20000000,
        saldo_laba=-170000,
    )
    assert f.totals(b.model_dump())["selisih"] == 0
    p = payload(
        penarikan_lain=1004000,
        pendapatan_produk=1250000,
        jasa_proses=30000,
        jasa_cat_packing=380000,
        jasa_cat=2211000,
        bayar_tukang=2625000,
        pembelian=939500,
        operasional=2249000,
        gaji=937500,
        topup_iklan=1578000,
        transfer_kas_gaji=937500,
        piutang_pengiriman=2334462,
        saldo_toko=7207000,
        stok=3585000,
    )
    first = f.calculate(b, p, D(0), D(1578000))
    assert first["perubahan_piutang"] == D(4021462)
    assert first["laba"] == D(567462)
    assert first["saldo_akhir"]["kas_utama"] == D(2271000)
    assert first["neraca"]["selisih"] == 0
    p = payload(
        penarikan_lain=5730000,
        pendapatan_produk=3550000,
        jasa_proses=60000,
        jasa_cat_packing=1210000,
        jasa_cat=1540000,
        bayar_tukang=6675000,
        pembelian=1390500,
        operasional=1960000,
        gaji=937500,
        topup_iklan=1365000,
        transfer_kas_gaji=937500,
        transfer_kas_ops=2500000,
        piutang_pengiriman=9140280,
        saldo_toko=1992000,
        stok=3585000,
    )
    second = f.calculate(f.Balances(**first["saldo_akhir"]), p, D(0), D(1365000))
    assert second["laba"] == D(1352818)
    assert first["laba"] + second["laba"] == D(1920280)
    assert second["saldo_akhir"]["kas_utama"] == D(4533000)
    assert second["neraca"]["aset"] == D(23625280)
    assert second["neraca"]["selisih"] == 0


def test_ads_exhausted_without_topup_and_internal_salary_transfer():
    b = balances(kas_utama=3000000, iklan_prabayar=2000000, modal=5000000)
    result = f.calculate(b, payload(gaji=1000000, transfer_kas_gaji=1000000), D(0), D(2000000))
    assert result["kas_keluar"] == 0
    assert result["saldo_akhir"]["iklan_prabayar"] == 0
    assert result["saldo_akhir"]["kas_utama"] == 2000000
    assert result["saldo_akhir"]["kas_gaji"] == 1000000
    assert result["saldo_akhir"]["utang_gaji"] == 1000000
    assert result["laba"] == -3000000
    assert result["neraca"]["selisih"] == 0


def test_inventory_and_credit_purchase_do_not_distort_profit():
    b = balances(kas_utama=1000, stok=500, modal=1500)
    result = f.calculate(b, payload(pembelian=200, stok=600, utang_usaha=100), D(0), D(0))
    assert result["hpp"] == 200
    assert result["neraca"]["selisih"] == 0


def test_sunday_cutoff_and_calendar_month_boundaries():
    assert f.week_bounds(date(2026, 10, 31)) == (date(2026, 10, 26), date(2026, 11, 1))
    assert f.next_cutoff(date(2026, 10, 25)) == date(2026, 10, 31)
    assert f.next_cutoff(date(2026, 10, 31)) == date(2026, 11, 1)
    assert f.next_cutoff(date(2026, 11, 1)) == date(2026, 11, 8)
    assert f.month_bounds("2024-02")[1] == date(2024, 2, 29)
    assert f.next_cutoff(date(2023, 12, 31)) == date(2024, 1, 7)
    for month in ("2026-00", "2026-13", "20999-10"):
        with pytest.raises(HTTPException):
            f.month_bounds(month)
    for amount in ("NaN", "Infinity", "-1", "0.001"):
        with pytest.raises(ValidationError):
            payload(stok=amount)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MarketplaceErpBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def seed_report(session):
    actor = SimpleNamespace(id="admin", role="admin")
    await save_opening(
        f.Opening(tanggal=date(2020, 9, 30), saldo=balances(kas_utama=10000, modal=10000)), session, actor
    )
    shop = AkunMarketplace(id="shop", platform="shopee", nama_toko="Bumi Lestari")
    settlement = Settlement(
        id="paid",
        akun_id="shop",
        platform="shopee",
        status="paid",
        net=1000,
        periode_mulai=datetime(2020, 10, 1, tzinfo=timezone.utc),
        periode_selesai=datetime(2020, 10, 4, tzinfo=timezone.utc),
    )
    order = Pesanan(id="order", platform="shopee", id_eksternal="sn", akun_id="shop", status="shipped", total=500)
    ad = IklanHarianToko(id="ad", akun_id="shop", tanggal=date(2020, 10, 4), expense=100)
    session.add_all([shop, settlement, order, ad])
    await session.commit()
    return actor, order, settlement, ad


@pytest.mark.asyncio
async def test_erp_sources_snapshot_freeze_reopen_and_no_operational_mutations(session):
    actor, order, settlement, ad = await seed_report(session)
    p = payload(settlement_ids=["paid"], saldo_toko=500, topup_iklan=100)
    p.preview_token = (await f.preview(session, p))["preview_token"]
    result = await f.close(session, p, actor)
    assert result["hasil"]["pemasukan_toko"] == "1000.00"
    assert result["hasil"]["biaya_iklan"] == "100.00"
    assert order.status == "shipped" and settlement.status == "paid" and ad.expense == D(100)
    order.status = "completed"
    ad.expense = 200
    await session.commit()
    frozen = await session.get(LaporanKeuanganTutup, p.tanggal)
    import json

    assert json.loads(frozen.snapshot_json)["hasil"]["biaya_iklan"] == "100.00"
    with pytest.raises(HTTPException):
        await f.close(session, p, actor)
    with pytest.raises(HTTPException):
        await f.reopen(session, date(2020, 9, 29), "Koreksi tanggal", actor)
    await f.reopen(session, p.tanggal, "Koreksi nilai iklan", actor)
    assert await session.get(LaporanKeuanganTutup, p.tanggal) is None
    assert await session.scalar(select(func.count()).select_from(LaporanKeuanganAudit)) == 3
    with pytest.raises(HTTPException):
        await f.preview(session, payload(tanggal=date(2020, 10, 11)))


@pytest.mark.asyncio
async def test_close_validation_and_read_only_preview(session):
    actor, *_ = await seed_report(session)
    for p in (
        payload(sumber_diperiksa=False),
        payload(topup_iklan=0),
        payload(settlement_ids=["paid", "paid"]),
        payload(settlement_ids=["unknown"]),
    ):
        with pytest.raises(HTTPException):
            await f.close(session, p, actor)
    await f.preview(session, payload(topup_iklan=100))
    assert await session.scalar(select(func.count()).select_from(LaporanKeuanganTutup)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "staff"])
async def test_every_report_endpoint_denies_non_admin_before_db_access(role):
    app = FastAPI()
    app.include_router(router)

    async def current():
        return SimpleNamespace(id="user", role=role)

    async def forbidden_db():
        raise AssertionError("Non-admin reached report database")

    app.dependency_overrides[get_current_user_marketplace_erp] = current
    app.dependency_overrides[get_db_marketplace_erp] = forbidden_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for method, path in [
            ("GET", "?periode=2026-10"),
            ("GET", "/sumber?tanggal=2026-10-06"),
            ("GET", "/pemasukan-shopee?periode=2026-10"),
            ("GET", "/riwayat"),
            ("POST", "/saldo-awal"),
            ("POST", "/pratinjau"),
            ("POST", "/tutup"),
            ("POST", "/impor-drive"),
            ("GET", "/2026-10-06/pembanding"),
            ("POST", "/2026-10-06/buka-kembali"),
        ]:
            response = await client.request(method, "/laporan-keuangan" + path, json={} if method == "POST" else None)
            assert response.status_code == 403, (path, response.text)


@pytest.mark.asyncio
async def test_admin_can_read_api_and_anonymous_is_denied(session):
    actor, *_ = await seed_report(session)
    app = FastAPI()
    app.include_router(router)

    async def db():
        yield session

    app.dependency_overrides[get_db_marketplace_erp] = db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/laporan-keuangan?periode=2026-10")).status_code == 401

        async def current():
            return actor

        app.dependency_overrides[get_current_user_marketplace_erp] = current
        response = await client.get("/laporan-keuangan?periode=2026-10")
        assert response.status_code == 200
        assert response.json()["akhir_bulan"] == "2026-10-31"
        assert (await client.get("/laporan-keuangan?periode=2026-13")).status_code == 422


@pytest.mark.asyncio
async def test_changed_erp_source_requires_new_preview_and_negative_balance_is_rejected(session):
    actor, _, _, ad = await seed_report(session)
    p = payload(topup_iklan=100)
    p.preview_token = (await f.preview(session, p))["preview_token"]
    ad.expense = 150
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await f.close(session, p, actor)
    assert exc.value.status_code == 409
    p.preview_token = (await f.preview(session, p))["preview_token"]
    with pytest.raises(HTTPException) as exc:
        await f.close(session, p, actor)
    assert exc.value.status_code == 422
    assert "negatif" in exc.value.detail


@pytest.mark.asyncio
async def test_month_final_uses_calendar_end_and_split_week_preserves_months(session):
    from tenants.marketplace_erp.adapters.api.v1.financial_reports_router import overview

    actor, *_ = await seed_report(session)
    for day in (4, 11, 18, 25, 31):
        p = payload(
            tanggal=date(2020, 10, day), topup_iklan=100 if day == 4 else 0, penarikan_lain=600 if day == 31 else 0
        )
        p.preview_token = (await f.preview(session, p))["preview_token"]
        await f.close(session, p, actor)
    monthly = await overview("2020-10", session)
    assert monthly["bulanan"]["final"] is True
    assert monthly["akhir_bulan"] == "2020-10-31"
    assert monthly["berikutnya"] == "2020-11-01"
    assert f.money(monthly["bulanan"]["pemasukan"]) == 600
    october_cash = monthly["bulanan"]["saldo_akhir"]["kas_utama"]
    p = payload(tanggal=date(2020, 11, 1), penarikan_lain=100)
    next_preview = await f.preview(session, p)
    assert next_preview["mulai"] == "2020-11-01"
    p.preview_token = next_preview["preview_token"]
    await f.close(session, p, actor)
    october = await overview("2020-10", session)
    november = await overview("2020-11", session)
    assert october["bulanan"]["saldo_akhir"]["kas_utama"] == october_cash
    assert f.money(october["bulanan"]["pemasukan"]) == 600
    assert f.money(november["bulanan"]["pemasukan"]) == 100
    split_week = october["mingguan"][-1]
    assert split_week["mulai"] == "2020-10-26"
    assert split_week["tanggal"] == "2020-11-01"
    assert split_week["jadwal_laporan"] == "2020-11-03"
    assert split_week["laporan"]["lengkap"] is True
    assert f.money(split_week["laporan"]["hasil"]["pemasukan"]) == 700
    assert split_week["laporan"]["hasil"]["saldo_awal"] == october["laporan"][-1]["hasil"]["saldo_awal"]
    with pytest.raises(HTTPException):
        await save_opening(f.Opening(tanggal=date(2020, 9, 30), saldo=balances()), session, actor)
    with pytest.raises(HTTPException):
        await f.reopen(session, date(2020, 10, 25), "Koreksi lama", actor)


@pytest.mark.asyncio
async def test_dates_required_and_out_of_range_transactions_are_rejected(session):
    await seed_report(session)
    for p in [
        payload(penarikan_lain=100, transaksi=[]),
        payload(transaksi=[dict(tanggal=date(2020, 10, 5), jenis="operasional", nominal=100)]),
        payload(settlement_ids=["paid"], tanggal_settlement={}),
        payload(settlement_ids=["paid"], tanggal_settlement={"paid": date(2020, 10, 5)}),
    ]:
        with pytest.raises(HTTPException) as exc:
            await f.preview(session, p)
        assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_daily_ads_filtered_at_month_end_wib_and_actual_payout_date(session):
    actor, _, settlement, _ = await seed_report(session)
    # Statement ends in October; cash is received in November. Selecting it by actual date is mandatory.
    settlement.periode_selesai = datetime(2020, 10, 31, 23, 59, tzinfo=timezone.utc)
    session.add(IklanHarianToko(id="ads-oct31", akun_id="shop", tanggal=date(2020, 10, 31), expense=60))
    session.add(IklanHarianToko(id="ads-nov1", akun_id="shop", tanggal=date(2020, 11, 1), expense=10))
    await session.commit()
    for day in (4, 11, 18, 25, 31):
        p = payload(tanggal=date(2020, 10, day), topup_iklan=100 if day == 4 else 60 if day == 31 else 0)
        p.preview_token = (await f.preview(session, p))["preview_token"]
        await f.close(session, p, actor)
    october = await f.overview(session, "2020-10")
    p = payload(tanggal=date(2020, 11, 1), settlement_ids=["paid"], topup_iklan=10)
    p.preview_token = (await f.preview(session, p))["preview_token"]
    await f.close(session, p, actor)
    november = await f.overview(session, "2020-11")
    assert f.money(october["laporan_bulanan"]["hasil"]["biaya_iklan"]) == 160
    assert f.money(november["laporan_bulanan"]["hasil"]["biaya_iklan"]) == 10
    assert f.money(october["laporan_bulanan"]["hasil"]["pemasukan_toko"]) == 0
    assert f.money(november["laporan_bulanan"]["hasil"]["pemasukan_toko"]) == 1000


@pytest.mark.asyncio
async def test_month_end_receivables_use_month_end_baseline_not_week_end(session):
    actor, *_ = await seed_report(session)
    for day in (4, 11, 18, 25, 31):
        p = payload(tanggal=date(2020, 10, day), topup_iklan=100 if day == 4 else 0, saldo_toko=600 if day == 31 else 0)
        p.preview_token = (await f.preview(session, p))["preview_token"]
        await f.close(session, p, actor)
    p = payload(tanggal=date(2020, 11, 1), penarikan_lain=500, saldo_toko=100)
    p.preview_token = (await f.preview(session, p))["preview_token"]
    await f.close(session, p, actor)
    oct_report = (await f.overview(session, "2020-10"))["laporan_bulanan"]
    nov_report = (await f.overview(session, "2020-11"))["laporan_bulanan"]
    assert f.money(oct_report["hasil"]["perubahan_piutang"]) == 600
    assert f.money(oct_report["hasil"]["pemasukan"]) == 600
    assert f.money(oct_report["hasil"]["saldo_akhir"]["saldo_toko"]) == 600
    assert f.money(nov_report["hasil"]["perubahan_piutang"]) == -500
    assert f.money(nov_report["hasil"]["pemasukan_toko"]) == 500
    assert f.money(nov_report["hasil"]["pemasukan"]) == 0
    assert f.money(nov_report["hasil"]["neraca"]["selisih"]) == 0


def test_monthly_presentation_is_next_tuesday_independent_of_weekly_presentation():
    assert f.next_presentation(date(2026, 8, 31)) == date(2026, 9, 1)
    assert f.next_presentation(f.week_bounds(date(2026, 8, 31))[1]) == date(2026, 9, 8)
    assert f.next_presentation(date(2026, 10, 31)) == date(2026, 11, 3)
    assert f.next_presentation(date(2026, 3, 31)) == date(2026, 4, 7)


@pytest.mark.asyncio
async def test_partial_month_not_final_and_legacy_reports_are_not_mixed(session):
    import json
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import LaporanKeuanganAwal

    actor, *_ = await seed_report(session)
    opening = await session.get(LaporanKeuanganAwal, "global")
    opening.tanggal = date(2020, 10, 25)
    await session.commit()
    p = payload(tanggal=date(2020, 10, 31))
    p.preview_token = (await f.preview(session, p))["preview_token"]
    await f.close(session, p, actor)
    view = await f.overview(session, "2020-10")
    assert view["bulanan"]["final"] is False
    assert view["laporan_bulanan"]["lengkap"] is False
    row = await session.get(LaporanKeuanganTutup, p.tanggal)
    snap = json.loads(row.snapshot_json)
    snap.pop("calendar_version")
    row.snapshot_json = json.dumps(snap)
    await session.commit()
    view = await f.overview(session, "2020-10")
    assert len(view["legacy"]) == 1
    assert view["laporan_bulanan"] is None
    with pytest.raises(HTTPException) as exc:
        await f.baseline(session, date(2020, 11, 1))
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_drive_import_primary_no_erp_overwrite_and_comparison(session):
    from tenants.marketplace_erp.modules.marketplace_erp.application.financial_drive_import import (
        DriveImport,
        import_drive,
        parse,
    )
    from tenants.marketplace_erp.adapters.api.v1.financial_reports_router import comparison

    # Construct the public numeric fixture in the report's B:I shape; no user export committed to git.
    rows = [[] for _ in range(105)]

    def setcell(row, col, value):
        while len(rows[row - 1]) <= col:
            rows[row - 1].append("")
        rows[row - 1][col] = value

    setcell(1, 0, "LAPORAN KEUANGAN")
    setcell(7, 1, "PEMASUKAN")
    setcell(24, 1, "PENGELUARAN")
    setcell(6, 2, "OK")
    setcell(8, 2, 46301)
    cells = {
        51: 5725000,
        76: 3585000,
        77: 3000000,
        78: 2000000,
        100: 5520000,
        91: 20000000,
        92: -170000,
        9: 257000,
        11: 747000,
        12: 1250000,
        13: 30000,
        14: 380000,
        15: 2211000,
        26: 2625000,
        27: 939500,
        28: 2249000,
        30: 937500,
        31: 1578000,
        62: 1578000,
        66: -937500,
        79: 2334462,
        80: 7207000,
        81: 937500,
        87: 937500,
        75: 2271000,
        19: 8896462,
        36: 8329000,
        45: 567462,
        68: 2271000,
        82: 21334962,
        97: 0,
    }
    for row, val in cells.items():
        setcell(row, 2, val)
    payload_import = DriveImport(
        source_url="https://docs.google.com/spreadsheets/d/fixture/edit",
        tab="LAPORAN",
        read_at=datetime.now(timezone.utc),
        rows=rows,
    )
    opening, history, _ = parse(payload_import)
    assert opening.tanggal == date(2026, 9, 30)
    assert history[0]["tanggal"] == "2026-10-06"
    actor = SimpleNamespace(id="admin", role="admin")
    assert (await import_drive(session, payload_import, actor))["jumlah"] == 1
    assert (await import_drive(session, payload_import, actor))["sudah_ada"] is True
    session.add(AkunMarketplace(id="compare", platform="shopee", nama_toko="Pembanding"))
    session.add(
        Pesanan(
            id="compare-order",
            platform="shopee",
            id_eksternal="cmp",
            akun_id="compare",
            status="shipped",
            total=9999999,
        )
    )
    session.add(IklanHarianToko(id="compare-ads", akun_id="compare", tanggal=date(2026, 10, 8), expense=888888))
    await session.commit()
    p = payload(tanggal=date(2026, 10, 11), piutang_pengiriman=2334462, saldo_toko=7207000, stok=3585000, biaya_iklan=0)
    preview = await f.preview(session, p)
    assert f.money(preview["hasil"]["biaya_iklan"]) == 0
    assert preview["sumber"]["utama"] == "google_drive"
    assert preview["sumber"]["biaya_iklan"] == "888888.00"
    compare = await comparison(date(2026, 10, 6), session)
    assert compare["laporan"]["saldo_toko"] == "7207000.00"
    assert compare["erp"]["saldo_toko"] is None
    assert compare["erp"]["piutang_belum_ditarik"] is None
    assert f.money(compare["erp"]["piutang_pengiriman_bruto"]) == 9999999
    view = await f.overview(session, "2026-10")
    assert view["sumber_utama"] == "google_drive"
    assert f.money(view["bulanan"]["laba"]) == 567462
    assert view["berikutnya"] == "2026-10-11"
    # Actual dated new-week supplement required; cannot silently import ERP ads.
    with pytest.raises(HTTPException):
        await f.preview(session, payload(tanggal=date(2026, 10, 11)))
    payload_import.rows[44][2] = 123
    with pytest.raises(HTTPException):
        parse(payload_import)


@pytest.mark.asyncio
async def test_shopee_weekly_comparison_boundaries_real_payout_and_missing(session):
    from tenants.marketplace_erp.modules.marketplace_erp.application.financial_shopee import weekly_income
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import SettlementPesanan

    session.add_all([
        AkunMarketplace(id="shop-a", platform="shopee", nama_toko="Toko A"),
        AkunMarketplace(id="shop-b", platform="shopee", nama_toko="Toko B"),
        AkunMarketplace(id="other", platform="lazada", nama_toko="Bukan Shopee"),
    ])
    for sn, day, status, total in [
        ("mon", "2026-09-28", "shipped", 100),
        ("sat", "2026-10-03", "completed", 200),
        ("sun", "2026-10-04", "shipped", 999),
        ("next", "2026-10-05", "shipped", 300),
        ("missing", "2026-10-12", "completed", 400),
        ("cancel", "2026-09-29", "cancelled", 999),
    ]:
        session.add(Pesanan(
            platform="shopee", id_eksternal=sn, akun_id="shop-a",
            dipesan_at=datetime.fromisoformat(day+"T12:00:00+07:00"), status=status, total=total))
    session.add(SettlementPesanan(akun_id="shop-a", order_sn="sat", jumlah_cair=180))
    await session.commit()
    result = await weekly_income(session, "2026-10", 5)
    assert result["minggu"][0] == {"mulai": "2026-09-28", "akhir": "2026-10-03"}
    assert result["minggu"][-1] == {"mulai": "2026-10-26", "akhir": "2026-10-31"}
    assert len(result["toko"]) == 2
    weeks = result["toko"][0]["minggu"]
    assert weeks[0] == {"estimasi": "100.00", "riil": "180.00", "belum_riil": 0, "jumlah": "280.00"}
    assert weeks[1]["jumlah"] == "300.00"
    assert weeks[2]["jumlah"] is None
    assert weeks[2]["belum_riil"] == 1
    assert result["toko"][1]["minggu"][0]["jumlah"] == "0"
