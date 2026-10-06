"""Product identity, draft confirmation, metadata imports and source adapters."""
import csv
import io
import json
from datetime import datetime, timezone

import httpx2 as httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import test_keu
from test_keu import order_payload
from tenants.bumi_lestari.adapters.api.v1.keu_router import router
from tenants.bumi_lestari.modules.bumi_lestari.application import keu_import, keu_services as svc, keu_sync, schemas_keu as sc
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_keu as m
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import get_current_user_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari


env = test_keu.env


def source_line():
    return sc.ProdukSumberIn(sku="CHILD-L", sku_induk="PARENT", nama_asli="Nama sumber panjang",
        gambar_url="https://img.example.test/photo.jpg", harga_jual="100.01", varian_list=[{"kategori": "Ukuran", "nilai": "L"}])


@pytest.mark.asyncio
async def test_draft_confirmation_and_resync_preserve_mapping(env):
    s, u, sal, p, _, v = env
    payload = order_payload(sal, p, "source")
    payload.items[0].produk_id = None
    payload.items[0].produk_sumber = source_line()
    order = await svc.create_order(s, u, payload, from_source=True)
    item = order["items"][0]
    product = await s.get(m.KeuProduk, item["produk_id"])
    assert product.status == "draf" and product.jenis is None and not product.aktif
    assert (await svc.dashboard(s))["belum_dipetakan"] == 1
    assert product.sku_induk == "PARENT" and product.nama_asli == "Nama sumber panjang"
    with pytest.raises(HTTPException):
        await svc.allocate_vendor(s, u, sc.AlokasiVendorIn(item_id=item["id"], vendor_id=v["vendor_id"], qty=1, biaya_satuan="5"))
    edited = sc.ProdukEditIn(nama="Alias", jenis="kayu", biaya_acuan="23.45",
        varian_list=[{"kategori": f"Dimensi {i}", "nilai": str(i)} for i in range(12)])
    saved = await svc.save_product(s, u, product.id, edited)
    assert saved["status"] == "master" and saved["aktif"] and len(saved["varian_list"]) == 12
    assert saved["nama_asli"] == "Nama sumber panjang" and saved["harga_jual"] == "100.01"
    await svc.create_order(s, u, payload, from_source=True)
    assert product.nama == "Alias" and product.biaya_acuan == edited.biaya_acuan
    second = payload.model_copy(update={"sumber_ref": "other", "nomor": "other"})
    order2 = await svc.create_order(s, u, second, from_source=True)
    assert order2["items"][0]["produk_id"] == product.id
    assert len((await s.execute(select(m.KeuProduk).where(m.KeuProduk.sku == "CHILD-L"))).scalars().all()) == 1
    await svc.allocate_vendor(s, u, sc.AlokasiVendorIn(item_id=item["id"], vendor_id=v["vendor_id"], qty=1, biaya_satuan="5"))
    with pytest.raises(HTTPException) as error:
        await svc.save_product(s, u, product.id, edited.model_copy(update={"jenis": "non_kayu"}))
    assert error.value.status_code == 409
    assert (await svc.page(s, m.KeuProduk, search="CHILD"))["total"] == 1


@pytest.mark.asyncio
async def test_excel_product_metadata_and_atomic_rollback(env):
    from openpyxl import Workbook
    s, u, sal, p, _, _ = env
    book = Workbook()
    book.active.append(keu_import.HEADERS["order"] + keu_import.PRODUCT_HEADERS)
    book.active.append(["excel", "line", "EXCEL", "2026-10-07", "", "Nama", 2, "10.01", "", "L",
        "EX-L", "EX", "Nama asli Excel", "https://img.example.test/ex.jpg", json.dumps([{"kategori": "Ukuran", "nilai": "L"}]), "20.02"])
    output = io.BytesIO()
    book.save(output)
    batch = await keu_import.preview(s, u, sal["id"], "order", "orders.xlsx", output.getvalue())
    assert batch["status"] == "valid"
    assert not (await s.execute(select(m.KeuProduk).where(m.KeuProduk.sku == "EX-L"))).first()
    await keu_import.apply(s, u, batch["id"])
    product = (await s.execute(select(m.KeuProduk).where(m.KeuProduk.sku == "EX-L"))).scalar_one()
    assert product.status == "draf" and product.nama_asli == "Nama asli Excel" and str(product.harga_jual) == "20.02"
    await keu_import.apply(s, u, batch["id"])
    # A conflicting explicit product_id must not silently relink a different SKU.
    payload = order_payload(sal, p, "conflict")
    payload.items[0].produk_sumber = source_line()
    with pytest.raises(HTTPException):
        await svc.create_order(s, u, payload)
    content = io.StringIO()
    writer = csv.writer(content)
    writer.writerow(keu_import.HEADERS["order"] + ["sku", "varian_list"])
    writer.writerow(["bad", "line", "BAD", "2026-10-07", "", "Name", 1, 10, "", "", "BAD-SKU", "{bad json"])
    bad = await keu_import.preview(s, u, sal["id"], "order", "bad.csv", content.getvalue().encode())
    assert bad["status"] == "draf" and bad["rows"][0]["status"] == "gagal"


@pytest.mark.asyncio
async def test_import_failure_rolls_back_new_products_and_orders(env):
    s, u, sal, _, _, _ = env
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(keu_import.HEADERS["order"] + ["sku"])
    writer.writerow(["good", "line1", "GOOD", "2026-10-07", "", "New", 1, 10, "", "", "ROLLBACK-SKU"])
    writer.writerow(["bad", "line2", "BAD", "2026-10-07", "missing", "Bad", 1, 10, "", "", ""])
    batch = await keu_import.preview(s, u, sal["id"], "order", "atomic.csv", output.getvalue().encode())
    assert batch["status"] == "valid"
    key = batch["id"]
    await s.commit()
    with pytest.raises(HTTPException):
        await keu_import.apply(s, u, key)
    await s.rollback()
    assert not (await s.execute(select(m.KeuProduk).where(m.KeuProduk.sku == "ROLLBACK-SKU"))).first()
    assert not (await s.execute(select(m.KeuPesanan).where(m.KeuPesanan.sumber_ref == "good"))).first()
    assert (await s.get(m.KeuImpor, key)).status == "valid"


@pytest.mark.parametrize("field,value", [("gambar_url", "javascript:alert(1)"), ("gambar_url", "https://user:secret@example.test/img"), ("harga_jual", "NaN"), ("varian_list", {}), ("varian_list", [{"kategori": "", "nilai": "L"}])])
def test_metadata_validation(field, value):
    data = source_line().model_dump()
    data[field] = value
    with pytest.raises(ValueError):
        sc.ProdukSumberIn.model_validate(data)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["store", "marketplace_erp"])
async def test_actual_sync_populates_draft_and_images(env, monkeypatch, kind):
    now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    if kind == "store":
        from tenants.store.modules.store.infrastructure import database, models
        base = database.StoreBase
        monkeypatch.setenv("MEDIA_BASE_URL", "https://img.example.test")
        product = models.ProdukStore(id="catalog", nama="Catalog", harga=10, foto_key="produk/parent.jpg")
        product.varian = [models.VarianProduk(id="variant", nama="L", sku="SYNC-L", foto_id="photo")]
        product.foto = [models.FotoProduk(id="photo", foto_key="produk/variant.jpg", urutan=0)]
        order = models.PesananStore(id="order", user_id="buyer", total=10, status="dibayar", created_at=now, updated_at=now)
        order.items = [models.ItemPesanan(id="line", produk_id="catalog", varian_id="variant", nama_produk="Sumber", nama_varian="L", qty=1, harga_satuan=10, subtotal=10)]
        rows = [product, order]
        expected_image = "https://img.example.test/produk/variant.jpg"
    else:
        from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import database, models
        base = database.MarketplaceErpBase
        product = models.Produk(id="catalog", sku_induk="PARENT", nama="Catalog", foto_url="https://img.example.test/parent.jpg")
        account = models.AkunMarketplace(id="acct", platform="shopee", nama_toko="Test")
        order = models.Pesanan(id="order", akun_id="acct", platform="shopee", id_eksternal="SN", total=10, status="completed", created_at=now, updated_at=now)
        order.items = [models.ItemPesanan(id="line", produk_id="catalog", nama_produk="Sumber", model_name="L", item_sku="PARENT", model_sku="SYNC-L", foto_url="https://img.example.test/variant.jpg", qty=1, harga_satuan=10, subtotal=10)]
        rows = [product, account, order]
        expected_image = "https://img.example.test/variant.jpg"
    try:
        async with engine.begin() as conn:
            await conn.run_sync(base.metadata.create_all)
        async with maker.begin() as source:
            source.add_all(rows)
        monkeypatch.setattr(database, "SessionLocal", maker)
        s, u, _, _, _, _ = env
        sal = await svc.create_master(s, u, "saluran", sc.SaluranIn(nama="Source", sistem=kind, akun_ref="store" if kind == "store" else "acct", aktif=True))
        result = await keu_sync.pull(s, u, sal["id"], "order")
        assert result["terproses"] == 1 and result["gagal"] == 0
        draft = (await s.execute(select(m.KeuProduk).where(m.KeuProduk.sku == "SYNC-L"))).scalar_one()
        assert draft.status == "draf" and draft.gambar_url == expected_image
        assert draft.varian_list == [{"kategori": "Varian", "nilai": "L"}]
        assert draft.sku_induk == (None if kind == "store" else "PARENT")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_edit_endpoint_readonly_identity_and_guards(env):
    s, u, _, p, _, _ = env
    app = FastAPI()
    app.include_router(router)
    async def db():
        yield s
    async def principal():
        return u
    app.dependency_overrides[get_db_bumi_lestari] = db
    app.dependency_overrides[get_current_user_bumi_lestari] = principal
    body = {"nama": "Ringkas", "jenis": "kayu", "biaya_acuan": "10.01", "varian_list": []}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = f'/keu/produk/{p["id"]}'
        for field in ["sku", "nama_asli", "status", "gambar_url", "harga_jual", "sku_induk"]:
            assert (await client.patch(path, json={**body, field: "changed"})).status_code == 422
        response = await client.patch(path, json=body)
        assert response.status_code == 200 and response.json()["nama_asli"] == "Kayu"
        u.role = "staff"
        assert (await client.patch(path, json=body)).status_code == 403
        u.role, u.must_change_password = "owner", True
        assert (await client.patch(path, json=body)).status_code == 403
