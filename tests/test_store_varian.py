"""Variants, pre-order lead time and weight/size on store products."""
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.store.modules.store.application import services
from tenants.store.modules.store.application.schemas import ProdukIn, ProdukPatch, VarianIn
from tenants.store.modules.store.infrastructure.database import StoreBase
from tenants.store.modules.store.infrastructure.models import PembeliStore


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(StoreBase.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def _buyer(session):
    u = PembeliStore(nama="P", email="p@test.com", password_hash="x")
    session.add(u)
    await session.flush()
    return u


async def _kaos(session):
    p = await services.create_produk(session, ProdukIn(nama="Kaos", harga="100000", stok=0))
    await services.ganti_varian(
        session,
        p.id,
        [
            VarianIn(nama="S", stok=2),
            VarianIn(nama="XL", stok=5, harga=Decimal("120000"), berat_gram=300),
        ],
    )
    return await services.get_produk(session, p.id)


def test_preorder_rules():
    assert ProdukIn(nama="a", harga="1").hari_proses == 2
    assert ProdukIn(nama="a", harga="1", hari_proses=9).hari_proses == 2  # ready stock is always 2 days
    assert ProdukIn(nama="a", harga="1", preorder=True, hari_proses=14).hari_proses == 14
    for bad in (2, 15):
        with pytest.raises(ValidationError):
            ProdukIn(nama="a", harga="1", preorder=True, hari_proses=bad)


@pytest.mark.asyncio
async def test_update_preorder_validates_and_resets(session):
    p = await services.create_produk(session, ProdukIn(nama="a", harga="1"))
    await services.update_produk(session, p.id, ProdukPatch(preorder=True, hari_proses=7))
    assert (p.preorder, p.hari_proses) == (True, 7)
    with pytest.raises(HTTPException) as exc:
        await services.update_produk(session, p.id, ProdukPatch(hari_proses=20))
    assert exc.value.status_code == 400
    await services.update_produk(session, p.id, ProdukPatch(preorder=False))
    assert (p.preorder, p.hari_proses) == (False, 2)


@pytest.mark.asyncio
async def test_variants_drive_price_range_and_total_stock(session):
    out = services.produk_out(await _kaos(session))
    assert (Decimal(out["harga_min"]), Decimal(out["harga_max"]), out["stok"]) == (Decimal(100000), Decimal(120000), 7)
    xl = next(v for v in out["varian"] if v["nama"] == "XL")
    assert xl["berat_gram"] == 300 and Decimal(xl["harga"]) == 120000


@pytest.mark.asyncio
async def test_duplicate_variant_names_rejected(session):
    p = await services.create_produk(session, ProdukIn(nama="a", harga="1"))
    with pytest.raises(HTTPException) as exc:
        await services.ganti_varian(session, p.id, [VarianIn(nama="S"), VarianIn(nama=" s ")])
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_replace_keeps_ids_and_deletes_missing(session):
    p = await _kaos(session)
    s, xl = p.varian
    p = await services.ganti_varian(session, p.id, [VarianIn(id=xl.id, nama="XXL", stok=1)])
    assert [(v.id, v.nama) for v in p.varian] == [(xl.id, "XXL")]


@pytest.mark.asyncio
async def test_cart_requires_a_variant_when_product_has_variants(session):
    u, p = await _buyer(session), await _kaos(session)
    with pytest.raises(HTTPException) as exc:
        await services.tambah_ke_keranjang(session, u.id, p.id, 1)
    assert exc.value.status_code == 400
    plain = await services.create_produk(session, ProdukIn(nama="b", harga="1", stok=1))
    with pytest.raises(HTTPException):
        await services.tambah_ke_keranjang(session, u.id, plain.id, 1, p.varian[0].id)


@pytest.mark.asyncio
async def test_checkout_uses_variant_price_and_stock_and_cancel_restocks(session):
    u, p = await _buyer(session), await _kaos(session)
    s, xl = p.varian
    await services.tambah_ke_keranjang(session, u.id, p.id, 2, xl.id)
    await services.tambah_ke_keranjang(session, u.id, p.id, 1, s.id)
    keranjang = {i["nama_varian"]: i for i in map(services.keranjang_item_out, await services.get_keranjang(session, u.id))}
    assert Decimal(keranjang["XL"]["harga"]) == 120000 and Decimal(keranjang["S"]["harga"]) == 100000

    pesanan = await services.checkout(session, u.id)
    assert pesanan.total == Decimal("340000")
    out = services.pesanan_out(pesanan)
    assert {i["nama_varian"] for i in out["items"]} == {"S", "XL"}
    p = await services.get_produk(session, p.id)
    assert {v.nama: v.stok for v in p.varian} == {"S": 1, "XL": 3}

    await services.ubah_status_pesanan(session, pesanan.id, "dibatalkan")
    p = await services.get_produk(session, p.id)
    await session.refresh(p, attribute_names=["varian"])
    for v in p.varian:
        await session.refresh(v)
    assert {v.nama: v.stok for v in p.varian} == {"S": 2, "XL": 5}


@pytest.mark.asyncio
async def test_variant_stock_is_enforced_and_cart_line_addressed_by_id(session):
    u, p = await _buyer(session), await _kaos(session)
    s = p.varian[0]
    item = await services.tambah_ke_keranjang(session, u.id, p.id, 3, s.id)
    with pytest.raises(HTTPException) as exc:
        await services.checkout(session, u.id)
    assert exc.value.status_code == 409
    await services.ubah_qty_keranjang(session, u.id, item.id, 2)
    await services.hapus_dari_keranjang(session, u.id, item.id)
    assert await services.get_keranjang(session, u.id) == []
