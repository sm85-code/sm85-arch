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
            ("GET", "/piutang-pengiriman-shopee?periode=2026-10"),
            ("GET", "/piutang-pengiriman-shopee/shop?periode=2026-10"),
            ("GET", "/saldo-penjual-shopee?periode=2026-10"),
            ("GET", "/saldo-wallet-shopee/shop?tanggal=2026-10-04"),
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
    session.add(AkunMarketplace(id="compare", platform="shopee", nama_toko="BUMI TANI IND"))
    session.add(
        Pesanan(
            id="compare-order",
            tanggal_kirim=datetime(2026, 10, 1, tzinfo=timezone.utc),
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
async def test_shopee_weekly_unfinished_estimates_shipping_dates_and_selected_shops(session):
    from tenants.marketplace_erp.modules.marketplace_erp.application.financial_shopee import weekly_income
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import SettlementPesanan

    session.add_all([
        AkunMarketplace(id="shop-a", platform="shopee", nama_toko="BUMI TANI IND"),
        AkunMarketplace(id="shop-b", platform="shopee", nama_toko=" AZFA\u00a0Furniture   Official "),
        AkunMarketplace(id="other", platform="lazada", nama_toko="BUMI TANI IND"),
        AkunMarketplace(id="excluded", platform="shopee", nama_toko="AZFA Furniture"),
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
            tanggal_kirim=datetime.fromisoformat(day+"T12:00:00+07:00"),
            dipesan_at=datetime(2026, 8, 1, tzinfo=timezone.utc), status=status, total=total))
    session.add(Pesanan(
        platform="shopee", id_eksternal="no-shipping-date", akun_id="shop-a",
        dipesan_at=datetime(2026, 9, 29, tzinfo=timezone.utc), status="shipped", total=999))
    session.add(Pesanan(
        platform="shopee", id_eksternal="excluded-order", akun_id="excluded",
        tanggal_kirim=datetime(2026, 9, 29, tzinfo=timezone.utc), status="shipped", total=999))
    session.add_all([
        Pesanan(platform="shopee", id_eksternal="azfa-received", akun_id="shop-b",
                tanggal_kirim=datetime(2026, 9, 29, tzinfo=timezone.utc),
                status="shipped", status_marketplace="TO_CONFIRM_RECEIVE", total=50),
        Pesanan(platform="shopee", id_eksternal="stale-completed", akun_id="shop-a",
                tanggal_kirim=datetime(2026, 9, 29, tzinfo=timezone.utc),
                status="shipped", status_marketplace="COMPLETED", total=999),
    ])
    session.add(SettlementPesanan(akun_id="shop-a", order_sn="sat", jumlah_cair=180))
    await session.commit()
    result = await weekly_income(session, "2026-10", 5)
    assert result["minggu"][0] == {"mulai": "2026-09-28", "akhir": "2026-10-03"}
    assert result["minggu"][-1] == {"mulai": "2026-10-26", "akhir": "2026-10-31"}
    assert [s["nama"] for s in result["toko"]] == [
        "BUMI TANI IND", "BUMI LESTARI INDONESIA", "RESTU BUMI IND",
        "MAJAPAHIT STORE IND", "CAHAYA LANGIT IND", "AZFA Furniture Official",
    ]
    assert result["toko"][1]["akun_ditemukan"] is False
    assert result["toko"][1]["minggu"][0]["estimasi"] is None
    weeks = result["toko"][0]["minggu"]
    assert weeks[0] == {"estimasi": "100.00", "riil": "0", "belum_riil": 0, "jumlah": "100.00"}
    assert weeks[1]["jumlah"] == "300.00"
    assert weeks[2]["jumlah"] == "0"
    assert weeks[2]["belum_riil"] == 0
    assert result["toko"][5]["minggu"][0]["jumlah"] == "50.00"
    assert result["toko"][5]["akun_ditemukan"] is True


@pytest.mark.asyncio
async def test_shopee_released_funds_use_release_date_not_shipping_or_order_status(session):
    from tenants.marketplace_erp.modules.marketplace_erp.application.financial_shopee import weekly_income
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import SettlementPesanan

    session.add_all([
        AkunMarketplace(id="official", platform="shopee", nama_toko="AZFA Furniture Official"),
        AkunMarketplace(id="other-azfa", platform="shopee", nama_toko="AZFA Furniture"),
        Pesanan(platform="shopee", id_eksternal="cross-week", akun_id="official",
                status="completed", total=999, tanggal_kirim=datetime(2026, 9, 29, tzinfo=timezone.utc)),
    ])
    for sn, account, stamp, payout in [
        ("cross-week", "official", "2026-10-05T00:00:00+07:00", 125),
        ("no-order-record", "official", "2026-10-03T23:59:59+07:00", 80),
        ("sunday", "official", "2026-10-04T12:00:00+07:00", 999),
        ("missing-release-date", "official", None, 999),
        ("other-azfa", "other-azfa", "2026-10-05T12:00:00+07:00", 999),
    ]:
        session.add(SettlementPesanan(
            akun_id=account, order_sn=sn, dirilis_at=datetime.fromisoformat(stamp) if stamp else None,
            jumlah_cair=payout))
    await session.commit()
    result = await weekly_income(session, "2026-10", 5, released=True)
    assert len(result["toko"]) == 6
    azfa = result["toko"][5]
    assert azfa["nama"] == "AZFA Furniture Official"
    assert azfa["minggu"][0]["jumlah"] == "80.00"
    assert azfa["minggu"][1]["jumlah"] == "125.00"
    assert azfa["minggu"][1]["riil"] == "125.00"
    assert azfa["minggu"][1]["estimasi"] == "0"
    assert sum(D(w["jumlah"]) for w in azfa["minggu"]) == 205


@pytest.mark.asyncio
async def test_comparison_matches_income_totals_six_shops_and_same_end_date(session, monkeypatch):
    import json
    from tenants.marketplace_erp.adapters.api.v1.financial_reports_router import comparison
    from tenants.marketplace_erp.modules.marketplace_erp.application.financial_shopee import REPORT_SHOPS, weekly_income

    session.add_all([AkunMarketplace(id=f"s{i}", platform="shopee", nama_toko=name, id_toko_eksternal=str(i+1)) for i, name in enumerate(REPORT_SHOPS)])
    session.add(AkunMarketplace(id="other-azfa", platform="shopee", nama_toko="AZFA Furniture"))
    for sn, shop, day, status, amount in [
        ("first", "s0", "2026-09-28T00:00:00+07:00", "shipped", 100),
        ("last", "s0", "2026-10-13T23:59:59+07:00", "shipped", 200),
        ("official", "s5", "2026-10-08T12:00:00+07:00", "shipped", 30),
        ("future", "s0", "2026-10-14T00:00:00+07:00", "shipped", 9000),
        ("sunday", "s0", "2026-10-04T12:00:00+07:00", "shipped", 8000),
        ("prior", "s0", "2026-09-27T12:00:00+07:00", "shipped", 7000),
        ("missing", "s0", None, "shipped", 6000),
        ("completed", "s0", "2026-10-07T12:00:00+07:00", "completed", 5000),
        ("other", "other-azfa", "2026-10-07T12:00:00+07:00", "shipped", 4000),
    ]:
        session.add(Pesanan(platform="shopee", id_eksternal=sn, akun_id=shop, status=status, total=amount,
                            tanggal_kirim=datetime.fromisoformat(day) if day else None))
    for cutoff, start, ads in [(6, 1, 40), (13, 7, 60)]:
        session.add(LaporanKeuanganTutup(
            tanggal=date(2026, 10, cutoff), created_by="admin",
            snapshot_json=json.dumps({"mulai": f"2026-10-{start:02d}", "hasil": {
                "saldo_akhir": {"piutang_pengiriman": "300", "saldo_toko": "20"}, "biaya_iklan": str(ads)}})))
    session.add_all([
        IklanHarianToko(akun_id="s0", tanggal=date(2026, 9, 29), expense=11),
        IklanHarianToko(akun_id="s0", tanggal=date(2026, 10, 13), expense=22),
        IklanHarianToko(akun_id="s0", tanggal=date(2026, 10, 14), expense=999),
        IklanHarianToko(akun_id="other-azfa", tanggal=date(2026, 10, 13), expense=999),
    ])
    await session.commit()
    from tenants.marketplace_erp.modules.marketplace_erp.application import workflows
    called = []
    async def historical_wallet(session, account_id, *, cutoff):
        assert cutoff == date(2026, 10, 13)
        called.append(account_id)
        return {"saldo_terakhir": {"s1": "100", "s4": "50", "s2": "25"}[account_id],
                "currency": "IDR", "transaksi_at": 1791900000, "diperiksa_at": "2026-10-15T00:00:00Z"}
    monkeypatch.setattr(workflows, "shop_balance_snapshot", historical_wallet)
    result = await comparison(date(2026, 10, 13), session)
    assert called == ["s1", "s4", "s2"]
    assert D(result["erp"]["saldo_toko"]) == 175
    assert result["erp"]["saldo_tertahan_cutoff"] == "2026-10-13"
    assert result["erp"]["saldo_tertahan_lengkap"] is True
    assert result["periode"] == {"mulai": "2026-09-28", "akhir": "2026-10-13", "hari": "Senin–Sabtu"}
    assert D(result["erp"]["piutang_pengiriman_bruto"]) == 330
    assert D(result["erp"]["estimasi_bumi_tani"]) == 300
    assert D(result["erp"]["estimasi_cakupan_drive"]) == 30
    assert result["erp"]["rincian_toko"][0]["masuk_estimasi_drive"] is False
    assert all(t["masuk_estimasi_drive"] for t in result["erp"]["rincian_toko"][1:])
    assert {p["nomor"] for p in result["erp"]["pesanan"]} == {"first", "last", "official"}
    assert len(result["erp"]["rincian_toko"]) == 6
    assert [D(w["nominal"]) for w in result["erp"]["rincian_minggu"]] == [100, 30, 200]
    assert sum(D(t["nominal"]) for t in result["erp"]["rincian_toko"]) == 330
    table = await weekly_income(session, "2026-10", 5, sampai=date(2026, 10, 13))
    assert sum(D(w["jumlah"]) for t in table["toko"] for w in t["minggu"]) == 330
    assert D(result["erp"]["biaya_iklan"]) == 33
    assert D(result["laporan"]["biaya_iklan"]) == 100
    assert result["periode_iklan_laporan"]["mulai"] == "2026-10-01"


@pytest.mark.asyncio
async def test_held_wallet_total_requires_three_complete_idr_balances_and_preserves_zero(session, monkeypatch):
    from tenants.marketplace_erp.modules.marketplace_erp.application import workflows
    from tenants.marketplace_erp.modules.marketplace_erp.application.financial_held_balance import HELD_SHOPS, held_balance

    for i, name in enumerate(HELD_SHOPS):
        session.add(AkunMarketplace(id=f"held-{i}", platform="shopee", nama_toko=name, id_toko_eksternal=str(i+1)))
    session.add(AkunMarketplace(id="other", platform="shopee", nama_toko="BUMI TANI IND", id_toko_eksternal="other"))
    await session.commit()
    values = {"held-0": "100", "held-1": "50", "held-2": "0"}

    async def wallet(session, account_id, *, cutoff):
        assert account_id != "other"
        assert cutoff == date(2026, 10, 13)
        return {"saldo_terakhir": values[account_id], "currency": "IDR", "transaksi_at": 1791900000, "diperiksa_at": "2026-10-15T00:00:00Z"}

    monkeypatch.setattr(workflows, "shop_balance_snapshot", wallet)
    complete = await held_balance(session, date(2026, 10, 13))
    assert D(complete["saldo_toko"]) == 150
    assert complete["lengkap"] is True
    values["held-1"] = None
    missing = await held_balance(session, date(2026, 10, 13))
    assert missing["saldo_toko"] is None and missing["lengkap"] is False
    assert missing["rincian"][1]["nominal"] is None
    assert D(missing["rincian"][2]["nominal"]) == 0

    async def unavailable(*args, **kwargs):
        raise HTTPException(502, "Unavailable")

    monkeypatch.setattr(workflows, "shop_balance_snapshot", unavailable)
    failed = await held_balance(session, date(2026, 10, 13))
    assert failed["saldo_toko"] is None
    assert all(r["keterangan"] for r in failed["rincian"])


@pytest.mark.asyncio
async def test_shipping_receivable_cohorts_old_carry_and_previous_week_cash(session):
    from tenants.marketplace_erp.modules.marketplace_erp.application.financial_shopee import weekly_shipping_receivables
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import SettlementPesanan
    session.add(AkunMarketplace(id='ship-main', platform='shopee', nama_toko='BUMI LESTARI INDONESIA'))
    for sn, day, total, status, native in [
        ('old-open', '2026-09-21', 40, 'shipped', 'SHIPPED'),
        ('old-paid', '2026-09-24', 60, 'completed', 'COMPLETED'),
        ('last-paid', '2026-09-28', 100, 'completed', 'COMPLETED'),
        ('received', '2026-10-03', 200, 'shipped', 'TO_CONFIRM_RECEIVE'),
        ('new', '2026-10-05', 300, 'shipped', 'SHIPPED'),
        ('same-week', '2026-10-05', 80, 'shipped', 'SHIPPED'),
        ('sunday', '2026-10-04', 999, 'shipped', 'SHIPPED'),
        ('cancelled', '2026-09-29', 999, 'cancelled', 'CANCELLED'),
        ('after-limit', '2026-10-14', 999, 'shipped', 'SHIPPED'),
    ]:
        session.add(Pesanan(platform='shopee', akun_id='ship-main', id_eksternal=sn,
                            tanggal_kirim=datetime.fromisoformat(day+'T12:00:00+07:00'),
                            total=total, status=status, status_marketplace=native))
    for sn, amount in [('old-paid', 55), ('last-paid', 90), ('same-week', 70), ('sunday', 900)]:
        session.add(SettlementPesanan(akun_id='ship-main', order_sn=sn, jumlah_cair=amount,
                                     dirilis_at=datetime.fromisoformat('2026-10-06T12:00:00+07:00')))
    await session.commit()
    result = await weekly_shipping_receivables(session, '2026-10', 5, sampai=date(2026, 10, 13),
                                               as_of=datetime.fromisoformat('2026-11-01T12:00:00+07:00'))
    row = next(s for s in result['toko'] if s['nama'] == 'BUMI LESTARI INDONESIA')
    assert [(w['piutang'], w['piutang_lama'], w['cair_minggu_lalu']) for w in row['minggu'][:3]] == [
        ('300.00', '100.00', '0'), ('300.00', '240.00', '90.00'), ('0', '540.00', '0')]
    assert result['minggu'][0]['mulai'] == '2026-09-28'
    assert result['minggu'][2]['hingga'] == '2026-10-13'
    assert row['minggu'][3]['piutang'] is None
    assert result['toko'][0]['minggu'][0]['piutang'] is None  # Missing account is not a confirmed zero.
    assert len(result['toko']) == 6 and result['toko'][-1]['nama'] == 'AZFA Furniture Official'


@pytest.mark.asyncio
async def test_shipping_receivable_incomplete_evidence_is_not_zero_and_accounts_are_isolated(session):
    from tenants.marketplace_erp.modules.marketplace_erp.application.financial_shopee import weekly_shipping_receivables
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import SettlementPesanan
    session.add_all([AkunMarketplace(id='sa', platform='shopee', nama_toko='BUMI TANI IND'),
                     AkunMarketplace(id='sb', platform='shopee', nama_toko='AZFA Furniture Official')])
    for account, sn, status in [('sa', 'same-sn', 'completed'), ('sb', 'sb-sn', 'shipped'),
                                ('sa', 'unknown-release', 'completed')]:
        session.add(Pesanan(platform='shopee', akun_id=account, id_eksternal=sn, status=status,
                            tanggal_kirim=datetime.fromisoformat('2026-10-02T12:00:00+07:00'), total=100))
    session.add_all([
        SettlementPesanan(akun_id='sa', order_sn='same-sn', jumlah_cair=90,
                          dirilis_at=datetime.fromisoformat('2026-10-06T12:00:00+07:00')),
        SettlementPesanan(akun_id='sa', order_sn='unknown-release', jumlah_cair=90),
        SettlementPesanan(akun_id='sa', order_sn='sb-sn', jumlah_cair=50,
                          dirilis_at=datetime.fromisoformat('2026-10-07T12:00:00+07:00')),
    ])
    await session.commit()
    result = await weekly_shipping_receivables(session, '2026-10', 5,
                                               as_of=datetime.fromisoformat('2026-10-08T12:00:00+07:00'))
    a, b = result['toko'][0], result['toko'][-1]
    assert a['minggu'][0]['piutang'] is None and a['minggu'][0]['piutang_tidak_pasti'] == 1
    assert a['minggu'][1]['piutang_lama'] is None
    assert a['minggu'][1]['cair_minggu_lalu'] is None and a['minggu'][1]['cair_tidak_terpetakan'] == 2
    assert b['minggu'][0]['piutang'] == '100.00'
    assert b['minggu'][1]['piutang_lama'] == '100.00' and b['minggu'][1]['cair_minggu_lalu'] == '0'
    assert result['minggu'][1]['hingga'] == '2026-10-08'
    assert b['minggu'][2]['piutang'] is None


@pytest.mark.asyncio
async def test_live_pending_uses_estimate_even_when_received_or_completed(session, monkeypatch):
    from unittest.mock import AsyncMock
    from tenants.marketplace_erp.modules.marketplace_erp.application import financial_pending as pending

    session.add(AkunMarketplace(id='pending-shop', platform='shopee', nama_toko='CAHAYA LANGIT IND'))
    session.add_all([
        Pesanan(akun_id='pending-shop', platform='shopee', id_eksternal='RECEIVED',
                status='completed', status_marketplace='COMPLETED', total=999999,
                tanggal_kirim=datetime(2026, 10, 5, 10, tzinfo=f.WIB)),
        Pesanan(akun_id='pending-shop', platform='shopee', id_eksternal='RELEASED',
                status='completed', total=888888,
                tanggal_kirim=datetime(2026, 9, 29, 10, tzinfo=f.WIB)),
    ])
    await session.commit()
    income = AsyncMock(side_effect=[
        {'items': [{'order_sn': 'RECEIVED', 'estimated_escrow_amount': '120000'}],
         'pagination_known': True, 'next_cursor': 'page2'},
        {'items': [{'order_sn': 'NOTIMPORTED', 'estimated_escrow_amount': '50000'},
                   {'order_sn': 'NOTSHIPPED', 'estimated_escrow_amount': '999999'}],
         'pagination_known': True, 'next_cursor': ''},
    ])
    monkeypatch.setattr(pending.commerce, 'income', income)
    details = AsyncMock(return_value={'items': [{'order_sn': 'NOTIMPORTED', 'pickup_done_time':
                        int(datetime(2026, 10, 6, 10, tzinfo=f.WIB).timestamp())},
                       {'order_sn': 'NOTSHIPPED', 'order_status': 'PROCESSED'}]})
    monkeypatch.setattr(pending.commerce, 'income_orders', details)
    monkeypatch.setattr(pending.erp_shopee, 'daftar_escrow', AsyncMock(return_value=[{
        'order_sn': 'RELEASED', 'payout': '90000', 'dirilis': datetime(2026, 10, 6, 17, tzinfo=f.WIB),
    }]))
    result = await pending.weekly_pending_receivables(session, 'pending-shop', '2026-10', 4,
             sampai=date(2026, 10, 13), as_of=datetime(2026, 10, 16, 12, tzinfo=f.WIB))
    weeks = result['minggu']
    assert weeks[0]['piutang'] == '90000.00'  # Net payout reconstructs historical unpaid balance.
    assert weeks[1]['piutang'] == '170000.00'  # Estimates, never the gross ERP order total.
    assert weeks[1]['cair_minggu_lalu'] == '90000.00'
    assert weeks[2]['piutang_lama'] == '170000.00'  # Still unpaid despite COMPLETED / 2x24h elapsed.
    assert weeks[2]['piutang_lama_tidak_pasti'] == 0
    assert weeks[3]['piutang'] is None
    assert result['halaman_lengkap'] is True
    assert result['tanpa_tanggal_kirim'] == 0
    assert income.call_args_list[1].args[-2] == 'page2'
    assert set(details.call_args.args[-1]) == {'NOTIMPORTED', 'NOTSHIPPED'}
    assert (await session.execute(select(Pesanan).where(Pesanan.id_eksternal == 'NOTIMPORTED'))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_pending_partial_pages_and_missing_dates_are_explicit(session, monkeypatch):
    from unittest.mock import AsyncMock
    from tenants.marketplace_erp.modules.marketplace_erp.application import financial_pending as pending

    session.add(AkunMarketplace(id='partial-pending', platform='shopee', nama_toko='RESTU BUMI IND'))
    await session.commit()
    monkeypatch.setattr(pending.commerce, 'income', AsyncMock(return_value={
        'items': [{'order_sn': 'NODATE', 'estimated_escrow_amount': '100'}], 'pagination_known': False,
    }))
    monkeypatch.setattr(pending.commerce, 'income_orders', AsyncMock(return_value={'items': []}))
    monkeypatch.setattr(pending.commerce, 'income_overview', AsyncMock(return_value={'pending_amount': None}))
    monkeypatch.setattr(pending.erp_shopee, 'daftar_escrow', AsyncMock(return_value=[]))
    result = await pending.weekly_pending_receivables(session, 'partial-pending', '2026-10', 2,
              as_of=datetime(2026, 10, 13, 12, tzinfo=f.WIB))
    assert result['halaman_lengkap'] is False
    assert result['tanpa_tanggal_kirim'] == 1
    with pytest.raises(HTTPException) as exc:
        await pending.weekly_pending_receivables(session, 'not-a-report-shop', '2026-10')
    assert exc.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_total,accepted", [("0", True), ("100", False), (None, False)])
async def test_empty_pending_response_requires_independent_zero_confirmation(monkeypatch, pending_total, accepted):
    from unittest.mock import AsyncMock
    from tenants.marketplace_erp.modules.marketplace_erp.application import financial_pending as pending
    from tenants.marketplace_erp.modules.marketplace_erp.application import management_schemas

    monkeypatch.setattr(management_schemas, 'today', lambda: date(2026, 10, 18))
    request = AsyncMock(return_value={'error': '', 'response': {}})
    monkeypatch.setattr(pending.commerce.provider, 'signed_shop_request', request)
    monkeypatch.setattr(pending.commerce, 'income_overview', AsyncMock(return_value={'pending_amount': pending_total}))
    if accepted:
        assert await pending.pending_pages(None, SimpleNamespace(id='shop'), date(2026, 10, 18)) == ({}, True)
    else:
        with pytest.raises(HTTPException):
            await pending.pending_pages(None, SimpleNamespace(id='shop'), date(2026, 10, 18))
    assert [call.kwargs['params']['page_size'] for call in request.call_args_list] == ([100] if accepted else [100, 10])


@pytest.mark.asyncio
@pytest.mark.parametrize("total,complete", [("150", True), ("200", False), (None, False)])
async def test_missing_pending_pagination_verified_by_total(monkeypatch, total, complete):
    from unittest.mock import AsyncMock
    from tenants.marketplace_erp.modules.marketplace_erp.application import financial_pending as pending

    monkeypatch.setattr(pending.commerce, 'income', AsyncMock(return_value={
        'items': [{'order_sn': 'A', 'estimated_escrow_amount': '100'},
                  {'order_sn': 'B', 'estimated_escrow_amount': '50'}], 'pagination_known': False,
    }))
    monkeypatch.setattr(pending.commerce, 'income_overview', AsyncMock(return_value={'pending_amount': total}))
    rows, verified = await pending.pending_pages(None, SimpleNamespace(id='shop'), date(2026, 10, 18))
    assert set(rows) == {'A', 'B'}
    assert verified is complete


@pytest.mark.asyncio
async def test_fast_escrow_read_applies_only_to_bumi_tani(session, monkeypatch):
    from unittest.mock import AsyncMock
    from tenants.marketplace_erp.modules.marketplace_erp.application import financial_pending as pending
    from tenants.marketplace_erp.modules.marketplace_erp.application import financial_fast_escrow as fast

    session.add_all([
        AkunMarketplace(id='fast-shop', platform='shopee', nama_toko='BUMI TANI IND'),
        AkunMarketplace(id='ordinary-shop', platform='shopee', nama_toko='CAHAYA LANGIT IND'),
        Pesanan(akun_id='fast-shop', platform='shopee', id_eksternal='FASTORDER', status='completed', total=999,
                tanggal_kirim=datetime(2026, 9, 30, 12, tzinfo=f.WIB)),
        Pesanan(akun_id='ordinary-shop', platform='shopee', id_eksternal='NORMALORDER', status='shipped', total=999,
                tanggal_kirim=datetime(2026, 9, 30, 12, tzinfo=f.WIB)),
    ])
    await session.commit()
    monkeypatch.setattr(pending, 'pending_pages', AsyncMock(side_effect=[
        ({'FASTORDER': {'estimated_escrow_amount': '20'}}, True),
        ({'NORMALORDER': {'estimated_escrow_amount': '20'}}, True),
    ]))
    monkeypatch.setattr(pending.erp_shopee, 'daftar_escrow', AsyncMock(return_value=[]))
    wallet = AsyncMock(return_value=[dict(at=datetime(2026, 10, 6, 17, tzinfo=f.WIB),
                     amount=f.money(80), kind='advance', orders={'FASTORDER'})])
    monkeypatch.setattr(fast, 'wallet_income', wallet)
    result = await pending.weekly_pending_receivables(session, 'fast-shop', '2026-10', 3,
             sampai=date(2026, 10, 13), as_of=datetime(2026, 10, 16, 12, tzinfo=f.WIB))
    assert result['dana_cepat'] is True
    assert f.money(result['minggu'][0]['piutang']) == 100
    assert f.money(result['minggu'][1]['piutang_lama']) == 20
    assert f.money(result['minggu'][1]['dana_cair']) == 80
    normal = await pending.weekly_pending_receivables(session, 'ordinary-shop', '2026-10', 3,
             sampai=date(2026, 10, 13), as_of=datetime(2026, 10, 16, 12, tzinfo=f.WIB))
    assert 'dana_cepat' not in normal
    assert f.money(normal['minggu'][0]['piutang']) == 20
    assert wallet.await_count == 1
