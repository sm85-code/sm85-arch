from datetime import date
from decimal import Decimal

import httpx2 as httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tenants.bumi_lestari.adapters.api.v1.keu_router import router
from tenants.bumi_lestari.modules.bumi_lestari.application import keu_import, keu_services as svc, schemas_keu as sc
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_keu as m
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import get_current_user_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlAuditLog, BlKategori, BlTutupBuku, BlUser
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.seeder import _create_schema


@pytest_asyncio.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await _create_schema(engine)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        user = BlUser(id="owner", nama="Owner", email="owner@test.com", password_hash="hash", role="owner")
        categories = [BlKategori(id="income", nama="Penjualan", jenis="pemasukan"), BlKategori(id="expense", nama="Operasional", jenis="pengeluaran")]
        session.add_all([user, *categories])
        await session.flush()
        sal = await svc.create_master(session, user, "saluran", sc.SaluranIn(nama="Manual", sistem="manual", akun_ref="manual", aktif=True))
        product = await svc.create_master(session, user, "produk", sc.ProdukIn(sku="K1", nama="Kayu", jenis="kayu"))
        account = await svc.create_master(session, user, "akun", sc.AkunIn(kode="KAS", nama="Kas", jenis="kas"))
        vendor = await svc.set_slot(session, user, "tk-1", sc.VendorIn(nama="Tukang", jenis="tukang_kayu"))
        yield session, user, sal, product, account, vendor
    await engine.dispose()


def order_payload(sal, product, ref="o1", qty=2):
    return sc.PesananIn(saluran_id=sal["id"], sumber_ref=ref, nomor=ref, tanggal=date(2026, 10, 6),
                        status_sumber="manual", total_sumber="20", items=[sc.ItemIn(sumber_ref="i1", produk_id=product["id"], nama_snapshot="Kayu", qty=qty, harga_satuan="10", subtotal_sumber="20")])


@pytest.mark.asyncio
async def test_legacy_slots_are_on_demand_without_fixed_limits(env):
    s, u, sal, product, account, vendor = env
    rows = await svc.slots(s)
    assert {r["kode"] for r in rows} == {"tk-1"}
    assert sum(r["vendor"] is not None for r in rows) == 1
    updated = await svc.set_slot(s, u, "tk-1", sc.VendorIn(nama="Nama diubah", jenis="tukang_kayu"))
    assert updated["vendor_id"] == vendor["vendor_id"]
    assert updated["vendor"]["nama"] == "Nama diubah"
    with pytest.raises(HTTPException):
        await svc.set_slot(s, u, "sup-1", sc.VendorIn(nama="Salah jenis", jenis="tukang_kayu"))
    extra = await svc.set_slot(s, u, "sup-99", sc.VendorIn(nama="Supplier tambahan", tipe="non_kayu"))
    assert extra["vendor"]["tipe"] == "non_kayu"


@pytest.mark.asyncio
async def test_order_retry_and_references_are_idempotent(env):
    s, u, sal, p, _, _ = env
    payload = order_payload(sal, p)
    first = await svc.create_order(s, u, payload)
    again = await svc.create_order(s, u, payload)
    assert first["id"] == again["id"]
    changed = payload.model_copy(update={"nomor": "changed"})
    with pytest.raises(HTTPException) as error:
        await svc.create_order(s, u, changed)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_vendor_capacity_type_and_mapping_rules(env):
    s, u, sal, p, _, v = env
    result = await svc.create_order(s, u, order_payload(sal, p))
    item = result["items"][0]
    await svc.allocate_vendor(s, u, sc.AlokasiVendorIn(item_id=item["id"], vendor_id=v["vendor_id"], qty=1, biaya_satuan="5"))
    v2 = await svc.set_slot(s, u, "tk-2", sc.VendorIn(nama="Tukang 2", jenis="tukang_kayu"))
    with pytest.raises(HTTPException):
        await svc.allocate_vendor(s, u, sc.AlokasiVendorIn(item_id=item["id"], vendor_id=v2["vendor_id"], qty=2, biaya_satuan="5"))
    with pytest.raises(HTTPException):
        await svc.map_item(s, u, item["id"], sc.ItemPetaIn(produk_id=p["id"]))
    with pytest.raises(HTTPException):
        await svc.order_status(s, u, result["id"], sc.PesananStatusIn(status="aktif"))
    await svc.allocate_vendor(s, u, sc.AlokasiVendorIn(item_id=item["id"], vendor_id=v2["vendor_id"], qty=1, biaya_satuan="5"))
    assert (await svc.order_status(s, u, result["id"], sc.PesananStatusIn(status="aktif")))["status"] == "aktif"


@pytest.mark.asyncio
async def test_settlement_posting_atomic_reconciled_and_not_double_counted(env):
    s, u, sal, p, account, _ = env
    order = await svc.create_order(s, u, order_payload(sal, p))
    settlement = await svc.create_settlement(s, u, sc.SettlementIn(saluran_id=sal["id"], sumber_ref="paid1", tanggal_cair=date(2026, 10, 6), bruto="20", potongan="2", neto="18"))
    posting = sc.PostingSettlementIn(akun_id=account["id"], kategori_id="income")
    with pytest.raises(HTTPException):
        await svc.post_settlement(s, u, settlement["id"], posting)
    await svc.allocate_settlement(s, u, sc.AlokasiSettlementIn(settlement_id=settlement["id"], item_id=order["items"][0]["id"], jumlah="18"))
    await svc.post_settlement(s, u, settlement["id"], posting)
    await svc.post_settlement(s, u, settlement["id"], posting)
    result = await svc.dashboard(s)
    assert Decimal(result["saldo_kas"]) == 18
    assert Decimal(result["nilai_pesanan"]) == 20
    assert len((await s.execute(select(m.KeuTransaksi))).scalars().all()) == 1
    assert (await s.execute(select(BlAuditLog).where(BlAuditLog.entitas == "keu_settlement", BlAuditLog.aksi == "posting"))).first()


@pytest.mark.asyncio
async def test_period_lock_and_category_checks(env):
    s, u, sal, _, account, _ = env
    tx = sc.TransaksiIn(saluran_id=sal["id"], sumber_ref="expense1", akun_id=account["id"], kategori_id="expense", tanggal=date(2026, 9, 1), jenis="keluar", jumlah="1.01")
    result = await svc.transaction(s, u, tx)
    s.add(BlTutupBuku(periode="2026-09", ditutup_oleh=u.id, snapshot={}))
    await s.flush()
    with pytest.raises(HTTPException) as error:
        await svc.post_transaction(s, u, result["id"])
    assert error.value.status_code == 409
    with pytest.raises(HTTPException):
        await svc.transaction(s, u, tx.model_copy(update={"kategori_id": "income", "sumber_ref": "wrong"}))


@pytest.mark.asyncio
async def test_csv_quotes_grouped_items_and_duplicate_file(env):
    s, u, sal, p, _, _ = env
    content = (keu_import.template("order") + f'o2,i1,o2,2026-10-06,{p["id"]},"Kayu, besar",1,10,,\n' + f'o2,i2,o2,2026-10-06,{p["id"]},Kayu,1,10,,\n').encode()
    first = await keu_import.preview(s, u, sal["id"], "order", "orders.csv", content)
    assert first["status"] == "valid"
    again = await keu_import.preview(s, u, sal["id"], "order", "renamed.csv", content)
    assert first["id"] == again["id"]
    applied = await keu_import.apply(s, u, first["id"])
    assert applied["status"] == "diterapkan"
    orders = await svc.page(s, m.KeuPesanan)
    assert len(orders["rows"][0]["items"]) == 2
    assert orders["rows"][0]["items"][0]["nama_snapshot"] in {"Kayu, besar", "Kayu"}


@pytest.mark.asyncio
async def test_invalid_import_and_xlsx_formula_rejected(env):
    s, u, sal, p, _, _ = env
    content = (keu_import.template("order") + f'o,i,o,2026-10-06,{p["id"]},Kayu,-1,10,,\n').encode()
    batch = await keu_import.preview(s, u, sal["id"], "order", "bad.csv", content)
    assert batch["status"] == "draf" and batch["rows"][0]["kesalahan"]
    with pytest.raises(HTTPException):
        await keu_import.apply(s, u, batch["id"])
    from openpyxl import Workbook
    from io import BytesIO
    book = Workbook()
    book.active.append(["value"])
    book.active.append(["=1+1"])
    output = BytesIO()
    book.save(output)
    with pytest.raises(HTTPException):
        keu_import.parse_file(output.getvalue(), "formula.xlsx")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1.001", "9999999999999999999.00"])
def test_money_bounds(value):
    with pytest.raises(ValueError):
        sc.AkunIn(kode="K", nama="Kas", jenis="kas", saldo_awal=value)


@pytest.mark.asyncio
async def test_api_guards_and_decimal_serialization(env):
    s, u, sal, _, account, _ = env
    await s.commit()
    app = FastAPI()
    app.include_router(router, prefix="/api/bumi-lestari")
    async def db():
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise
    async def principal():
        return u
    app.dependency_overrides[get_db_bumi_lestari] = db
    app.dependency_overrides[get_current_user_bumi_lestari] = principal
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/bumi-lestari/keu/dashboard")).status_code == 200
        u.role = "staff"
        assert (await client.get("/api/bumi-lestari/keu/dashboard")).status_code == 403
        u.role, u.must_change_password = "owner", True
        assert (await client.get("/api/bumi-lestari/keu/dashboard")).status_code == 403
        u.must_change_password = False
        response = await client.post("/api/bumi-lestari/keu/akun", json={"kode": "B", "nama": "Bank", "jenis": "bank", "saldo_awal": "9007199254740993.01"})
        assert response.status_code == 201, response.text
        assert response.json()["saldo_awal"] == "9007199254740993.01"
        duplicate = await client.post("/api/bumi-lestari/keu/akun", json={"kode": "B", "nama": "Bank", "jenis": "bank"})
        assert duplicate.status_code == 409
        assert "INSERT" not in duplicate.text and "keu_akun" not in duplicate.text


@pytest.mark.asyncio
async def test_import_rolls_back_all_rows_on_invalid_reference(env):
    s, u, sal, p, account, _ = env
    content = (keu_import.template("biaya") + f'first,2026-10-06,{account["id"]},expense,10,Valid\n' + 'second,2026-10-06,missing,expense,10,Invalid account\n').encode()
    batch = await keu_import.preview(s, u, sal["id"], "biaya", "atomic.csv", content)
    assert batch["status"] == "valid"
    await s.commit()
    with pytest.raises(HTTPException):
        await keu_import.apply(s, u, batch["id"])
    await s.rollback()
    assert (await s.execute(select(m.KeuTransaksi))).scalars().all() == []
    stored = await s.get(m.KeuImpor, batch["id"])
    assert stored.status == "valid"


@pytest.mark.asyncio
async def test_old_source_revision_cannot_replace_new_snapshot(env):
    from datetime import datetime, timezone
    s, u, sal, p, _, _ = env
    newer = order_payload(sal, p).model_copy(update={"sumber_updated_at": datetime(2026, 10, 6, 12, tzinfo=timezone.utc), "status_sumber": "new"})
    row = await svc.create_order(s, u, newer, from_source=True)
    older = newer.model_copy(update={"sumber_updated_at": datetime(2026, 10, 6, 11, tzinfo=timezone.utc), "status_sumber": "old", "total_sumber": Decimal("1")})
    again = await svc.create_order(s, u, older, from_source=True)
    assert again["id"] == row["id"]
    assert again["status_sumber"] == "new" and Decimal(again["total_sumber"]) == 20


@pytest.mark.asyncio
async def test_source_event_failure_is_durable_and_atomic(env):
    from tenants.bumi_lestari.modules.bumi_lestari.application import keu_sync
    s, u, sal, p, _, _ = env
    source = order_payload(sal, p).model_dump(mode="json")
    source["items"][0]["produk_id"] = "missing"
    envelope = sc.MasukanIn(saluran_id=sal["id"] , entitas="order", sumber_ref="failed", payload=source)
    event = m.KeuMasukan(**envelope.model_dump(), revisi_sha256=sc.checksum_revisi(envelope))
    s.add(event)
    await s.flush()
    assert await keu_sync.process(s, u, event) == "gagal"
    assert (await s.execute(select(m.KeuPesanan))).scalars().all() == []
    assert event.percobaan == 1 and event.kesalahan
    source["items"][0]["produk_id"] = p["id"]
    event.payload = source
    assert (await keu_sync.retry(s, u, event.id))["status"] == "terproses"


@pytest.mark.asyncio
async def test_overlong_import_reference_is_row_error_not_server_error(env):
    s, u, sal, p, _, _ = env
    content = (keu_import.template("order") + f'{"x" * 256},i,o,2026-10-06,{p["id"]},Kayu,1,10,,\n').encode()
    result = await keu_import.preview(s, u, sal["id"], "order", "long.csv", content)
    assert result["status"] == "draf" and result["rows"][0]["status"] == "gagal"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["store", "marketplace_erp"])
async def test_pull_reads_actual_source_models_and_deduplicates_cursor(env, monkeypatch, kind):
    from datetime import datetime, timezone
    from sqlalchemy import event
    from tenants.bumi_lestari.modules.bumi_lestari.application import keu_sync
    source_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(source_engine, expire_on_commit=False)
    now = datetime(2026, 10, 6, 17, 30, tzinfo=timezone.utc)
    if kind == "store":
        from tenants.store.modules.store.infrastructure import database, models
        base, order_model = database.StoreBase, models.PesananStore
        source_order = order_model(id="src-order", user_id="test-buyer", total=20, status="dibayar", created_at=now, updated_at=now)
        source_order.items = [models.ItemPesanan(id="src-item", produk_id="test-product", nama_produk="Kayu", qty=2, harga_satuan=10, subtotal=20, nama_varian="Besar")]
        source_account = []
    else:
        from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import database, models
        base, order_model = database.MarketplaceErpBase, models.Pesanan
        source_account = [models.AkunMarketplace(id="src-account", platform="shopee", nama_toko="Test")]
        source_order = order_model(id="src-order", akun_id="src-account", platform="shopee", id_eksternal="SRC-1", total=20, status="completed", created_at=now, updated_at=now)
        source_order.items = [models.ItemPesanan(id="src-item", nama_produk="Kayu", qty=2, harga_satuan=10, subtotal=20, model_name="Besar")]
    try:
        async with source_engine.begin() as conn:
            await conn.run_sync(base.metadata.create_all)
        async with maker.begin() as source:
            source.add_all([*source_account, source_order])
        monkeypatch.setattr(database, "SessionLocal", maker)
        reads = []
        @event.listens_for(source_engine.sync_engine, "before_cursor_execute")
        def observe(conn, cursor, statement, parameters, context, executemany):
            reads.append(statement)
        s, u, _, _, _, _ = env
        sal = await svc.create_master(s, u, "saluran", sc.SaluranIn(nama="Sumber", sistem=kind, akun_ref="store" if kind == "store" else "src-account", aktif=True))
        first = await keu_sync.pull(s, u, sal["id"], "order")
        assert first == {"dibaca": 1, "terproses": 1, "gagal": 0, "ada_lanjutan": False}
        second = await keu_sync.pull(s, u, sal["id"], "order")
        assert second["dibaca"] == 0
        row = (await s.execute(select(m.KeuPesanan).where(m.KeuPesanan.saluran_id == sal["id"]))).scalar_one()
        assert row.tanggal.isoformat() == "2026-10-07"  # Indonesia business date
        lines = (await s.execute(select(m.KeuItem).where(m.KeuItem.pesanan_id == row.id))).scalars().all()
        assert len(lines) == 1 and lines[0].produk_id is None and lines[0].varian_snapshot == "Besar"
        if kind == "marketplace_erp":
            options = await keu_sync.source_options()
            assert options["erp"] == [{"id": "src-account", "nama": "Test", "platform": "shopee"}]
        assert all(statement.lstrip().upper().startswith("SELECT") for statement in reads)
        assert all("access_token" not in statement and "refresh_token" not in statement for statement in reads)
        if kind == "store":
            with pytest.raises(HTTPException):
                await keu_sync.pull(s, u, sal["id"], "settlement")
    finally:
        await source_engine.dispose()


def test_pull_range_rejects_incomplete_or_reversed_dates():
    from datetime import datetime, timezone
    from tenants.bumi_lestari.modules.bumi_lestari.application.keu_sync import validate_range
    cases = [(date(2026, 9, 1), None, None, None),
             (None, date(2026, 9, 1), None, None),
             (date(2026, 9, 2), date(2026, 9, 1), None, None),
             (None, None, datetime.now(timezone.utc), "id"),
             (date(2026, 9, 1), date(2026, 9, 1), datetime.now(timezone.utc), None),
             (date(2026, 9, 1), date.max, None, None), (date.min, date.min, None, None)]
    for args in cases:
        with pytest.raises(HTTPException) as error:
            validate_range(*args)
        assert error.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,entity", [("store", "order"), ("marketplace_erp", "order"), ("marketplace_erp", "settlement")])
async def test_historical_range_wib_boundaries_pagination_and_global_cursor(env, monkeypatch, kind, entity):
    from datetime import datetime, timedelta, timezone
    from tenants.bumi_lestari.modules.bumi_lestari.application import keu_sync
    s, user, _, _, _, _ = env
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    begin = datetime(2026, 8, 31, 17, tzinfo=timezone.utc)  # 1 September 00:00 WIB
    stop = begin + timedelta(days=1)
    stamp = datetime(2026, 10, 5, tzinfo=timezone.utc)
    watermark = stamp + timedelta(days=1)
    if kind == "store":
        from tenants.store.modules.store.infrastructure import database, models
        base = database.StoreBase
    else:
        from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import database, models
        base = database.MarketplaceErpBase
    try:
        async with engine.begin() as conn:
            await conn.run_sync(base.metadata.create_all)
        async with maker.begin() as source:
            if kind == "marketplace_erp":
                source.add(models.AkunMarketplace(id="range-account", platform="shopee", nama_toko="Range"))
            for i in range(103):
                when = begin if i < 100 else stop - timedelta(microseconds=1) if i == 100 else begin - timedelta(microseconds=1) if i == 101 else stop
                key = f"range-{i:03}"
                if entity == "settlement":
                    row = models.SettlementPesanan(id=key, akun_id="range-account", order_sn=key, dirilis_at=when,
                                                  diambil_at=stamp, penjualan=20, jumlah_cair=18)
                elif kind == "store":
                    row = models.PesananStore(id=key, user_id="buyer", status="dibayar", total=20, created_at=when, updated_at=stamp)
                    row.items = [models.ItemPesanan(id=f"line-{i}", produk_id="p", nama_produk="Kayu", qty=2, harga_satuan=10, subtotal=20)]
                else:
                    row = models.Pesanan(id=key, platform="shopee", akun_id="range-account", id_eksternal=key, total=20,
                                         created_at=when if i == 0 else begin - timedelta(days=30), dipesan_at=None if i == 0 else when, updated_at=stamp)
                    row.items = [models.ItemPesanan(id=f"line-{i}", nama_produk="Kayu", qty=2, harga_satuan=10, subtotal=20)]
                source.add(row)
        monkeypatch.setattr(database, "SessionLocal", maker)
        sal = await svc.create_master(s, user, "saluran", sc.SaluranIn(nama="Historical", sistem=kind, akun_ref="store" if kind == "store" else "range-account", aktif=True))
        cursor = m.KeuCursor(saluran_id=sal["id"], entitas=entity, watermark_at=watermark, watermark_ref="last")
        s.add(cursor)
        await s.flush()
        dates = {"tanggal_awal": date(2026, 9, 1), "tanggal_akhir": date(2026, 9, 1)}
        first = await keu_sync.pull(s, user, sal["id"], entity, **dates)
        assert first["dibaca"] == first["terproses"] == 100 and first["gagal"] == 0 and first["ada_lanjutan"]
        position = first["halaman_berikutnya"]
        second = await keu_sync.pull(s, user, sal["id"], entity, **dates,
                                     setelah_at=datetime.fromisoformat(position["setelah_at"]), setelah_ref=position["setelah_ref"])
        assert second["dibaca"] == second["terproses"] == 1 and not second["ada_lanjutan"] and second["halaman_berikutnya"] is None
        model = m.KeuPesanan if entity == "order" else m.KeuSettlement
        rows = (await s.execute(select(model).where(model.saluran_id == sal["id"]))).scalars().all()
        assert len(rows) == 101
        assert all((row.tanggal if entity == "order" else row.tanggal_cair) == date(2026, 9, 1) for row in rows)
        repeated = await keu_sync.pull(s, user, sal["id"], entity, **dates)
        assert repeated["dibaca"] == 100 and repeated["terproses"] == repeated["gagal"] == 0
        assert cursor.watermark_at == watermark and cursor.watermark_ref == "last"
        empty = await keu_sync.pull(s, user, sal["id"], entity, tanggal_awal=date(2026, 7, 1), tanggal_akhir=date(2026, 7, 2))
        assert empty["dibaca"] == 0 and empty["halaman_berikutnya"] is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pull_route_validates_and_forwards_date_range(env, monkeypatch):
    from datetime import datetime, timezone
    from tenants.bumi_lestari.modules.bumi_lestari.application import keu_sync
    s, user, sal, _, _, _ = env
    app = FastAPI()
    app.include_router(router, prefix="/api/bumi-lestari")
    calls = []
    async def db():
        yield s
    async def principal():
        return user
    async def capture(session, actor, key, entity, **kwargs):
        keu_sync.validate_range(**kwargs)
        calls.append((key, entity, kwargs))
        return {"dibaca": 0, "terproses": 0, "gagal": 0, "ada_lanjutan": False, "halaman_berikutnya": None}
    app.dependency_overrides[get_db_bumi_lestari] = db
    app.dependency_overrides[get_current_user_bumi_lestari] = principal
    monkeypatch.setattr(keu_sync, "pull", capture)
    path = f'/api/bumi-lestari/keu/saluran/{sal["id"]}/tarik'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for params in [{"tanggal_awal": "bad", "tanggal_akhir": "2026-09-01"},
                       {"tanggal_awal": "2026-09-02", "tanggal_akhir": "2026-09-01"},
                       {"tanggal_awal": "2026-09-01"},
                       {"tanggal_awal": "2026-09-01", "tanggal_akhir": "2026-09-01", "setelah_at": "2026-10-06T10:00:00", "setelah_ref": "last"}]:
            response = await client.post(path, params=params)
            assert response.status_code == 422, response.text
        params = {"entitas": "settlement", "tanggal_awal": "2026-09-01", "tanggal_akhir": "2026-09-30", "setelah_at": "2026-10-06T10:00:00+00:00", "setelah_ref": "last"}
        response = await client.post(path, params=params)
        assert response.status_code == 200, response.text
        assert calls == [(sal["id"], "settlement", {"tanggal_awal": date(2026, 9, 1), "tanggal_akhir": date(2026, 9, 30), "setelah_at": datetime(2026, 10, 6, 10, tzinfo=timezone.utc), "setelah_ref": "last"})]
