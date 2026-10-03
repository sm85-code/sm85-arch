"""bumi_lestari Tahap 2: katalog, pemasok, saluran, reseller, order & alur status."""
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.bumi_lestari.modules.bumi_lestari.application import order_services as svc
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_order import (
    HargaGrosirIn,
    OrderIn,
    OrderOut,
    OrderPatch,
    OrderStatusIn,
    PelangganIn,
    PemasokIn,
    ProdukIn,
    SaluranIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(BumiLestariBase.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def data(session):
    class D:
        pass

    d = D()
    d.partisi = await svc.create_produk(
        session, ProdukIn(sku="par-01", nama="Partisi kayu 2x1", harga_jual=Decimal("850000"), biaya_pokok_default=Decimal("500000"))
    )
    d.lampu = await svc.create_produk(
        session, ProdukIn(sku="lmp-01", nama="Lampu", jenis_produk="non_kayu", harga_jual=Decimal("100000"), biaya_pokok_default=Decimal("60000"))
    )
    d.tukang = await svc.create_pemasok(session, PemasokIn(nama="Pak Budi", jenis="tukang_kayu"))
    d.supplier = await svc.create_pemasok(session, PemasokIn(nama="Toko Lampu", jenis="supplier"))
    d.shopee = await svc.create_saluran(session, SaluranIn(nama="Shopee", jenis="marketplace"))
    d.reseller_saluran = await svc.create_saluran(session, SaluranIn(nama="Reseller", jenis="reseller"))
    d.rina = await svc.create_pelanggan(session, PelangganIn(nama="Toko Rina", tempo_hari=14))
    return d


async def _maju(session, order, *langkah):
    for nama in langkah:
        order = await svc.ubah_status_order(session, order.id, OrderStatusIn(status=nama))
    return order


@pytest.mark.asyncio
async def test_katalog_sku_unique_and_normalized(session, data):
    assert data.partisi.sku == "PAR-01"
    with pytest.raises(HTTPException) as exc:
        await svc.create_produk(session, ProdukIn(sku="PAR-01", nama="dup", harga_jual=Decimal("1")))
    assert exc.value.status_code == 409
    assert [p.sku for p in await svc.list_produk(session, "non_kayu")] == ["LMP-01"]


@pytest.mark.asyncio
async def test_order_kayu_defaults_and_full_flow_with_cat(session, data):
    o = await svc.create_order(
        session, OrderIn(saluran_id=data.shopee.id, produk_id=data.partisi.id, qty=2, no_order="SHP1", potongan_marketplace=Decimal("85000"), pemasok_id=data.tukang.id)
    )
    assert (o.harga_satuan, o.biaya_pokok, o.butuh_cat) == (Decimal("850000"), Decimal("1000000"), True)
    out = OrderOut.model_validate(o)
    assert (out.total_penjualan, out.laba_kotor) == (Decimal("1700000"), Decimal("615000"))
    o = await _maju(session, o, "dikerjakan", "diambil", "dicat", "dikirim", "selesai")
    assert o.status == "selesai" and o.tgl_dicat and o.tgl_selesai


@pytest.mark.asyncio
async def test_status_must_follow_flow_and_skip_cat_when_not_needed(session, data):
    o = await svc.create_order(
        session, OrderIn(saluran_id=data.shopee.id, produk_id=data.partisi.id, butuh_cat=False, pemasok_id=data.tukang.id)
    )
    with pytest.raises(HTTPException) as exc:
        await svc.ubah_status_order(session, o.id, OrderStatusIn(status="diambil"))  # lompat
    assert exc.value.status_code == 400
    o = await _maju(session, o, "dikerjakan", "diambil", "dikirim")  # tanpa 'dicat'
    assert o.tgl_dicat is None


@pytest.mark.asyncio
async def test_pemasok_required_and_must_match_product_type(session, data):
    o = await svc.create_order(session, OrderIn(saluran_id=data.shopee.id, produk_id=data.partisi.id))
    with pytest.raises(HTTPException):
        await svc.ubah_status_order(session, o.id, OrderStatusIn(status="dikerjakan"))  # belum ada pemasok
    with pytest.raises(HTTPException) as exc:  # supplier untuk produk kayu
        await svc.create_order(session, OrderIn(saluran_id=data.shopee.id, produk_id=data.partisi.id, pemasok_id=data.supplier.id))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_order_non_kayu_flow_has_no_cat(session, data):
    o = await svc.create_order(
        session, OrderIn(saluran_id=data.shopee.id, produk_id=data.lampu.id, butuh_cat=True, pemasok_id=data.supplier.id)
    )
    assert o.butuh_cat is False and o.biaya_pokok == Decimal("60000")
    o = await _maju(session, o, "diterima", "dikirim", "selesai")
    assert o.tgl_diambil and o.status == "selesai"
    with pytest.raises(HTTPException) as exc:
        await svc.update_order(session, o.id, OrderPatch(catatan="x"))  # sudah selesai
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_reseller_order_uses_wholesale_price_and_requires_customer(session, data):
    with pytest.raises(HTTPException) as exc:
        await svc.create_order(session, OrderIn(saluran_id=data.reseller_saluran.id, produk_id=data.partisi.id))
    assert exc.value.status_code == 400
    await svc.set_harga_grosir(session, HargaGrosirIn(produk_id=data.partisi.id, pelanggan_id=data.rina.id, harga=Decimal("700000")))
    await svc.set_harga_grosir(session, HargaGrosirIn(produk_id=data.partisi.id, pelanggan_id=data.rina.id, harga=Decimal("720000")))  # upsert
    o = await svc.create_order(
        session, OrderIn(saluran_id=data.reseller_saluran.id, pelanggan_id=data.rina.id, produk_id=data.partisi.id, qty=10)
    )
    assert o.harga_satuan == Decimal("720000")
    assert len(await svc.list_harga_grosir(session, data.rina.id)) == 1
    eceran = await svc.create_order(session, OrderIn(saluran_id=data.shopee.id, produk_id=data.partisi.id))
    assert eceran.harga_satuan == Decimal("850000")


@pytest.mark.asyncio
async def test_cancel_and_list_filters(session, data):
    a = await svc.create_order(session, OrderIn(saluran_id=data.shopee.id, produk_id=data.partisi.id))
    b = await svc.create_order(session, OrderIn(saluran_id=data.shopee.id, produk_id=data.lampu.id))
    await svc.ubah_status_order(session, a.id, OrderStatusIn(status="batal"))
    assert [o.id for o in await svc.list_order(session, status_order="batal")] == [a.id]
    assert [o.id for o in await svc.list_order(session, jenis_produk="non_kayu")] == [b.id]
    with pytest.raises(HTTPException) as exc:
        await svc.ubah_status_order(session, a.id, OrderStatusIn(status="dipesan"))
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_reseller_three_price_components_polos_and_color(session, data):
    await svc.set_harga_grosir(
        session,
        HargaGrosirIn(
            produk_id=data.partisi.id, pelanggan_id=data.rina.id, harga=Decimal("600000"),
            harga_cat_jasa=Decimal("100000"),
            harga_packing_biasa=Decimal("20000"), harga_packing_kayu=Decimal("50000"),
        ),
    )

    def order(**kw):
        return OrderIn(
            saluran_id=data.reseller_saluran.id, pelanggan_id=data.rina.id, produk_id=data.partisi.id,
            pemasok_id=data.tukang.id, **kw,
        )

    dicat = await svc.create_order(session, order(qty=2, warna="Custom: hijau sage"))
    out = OrderOut.model_validate(dicat)
    assert dicat.warna == "Custom: hijau sage"
    assert (dicat.harga_satuan, dicat.harga_cat_jasa, dicat.harga_packing, dicat.biaya_proses) == (
        600000, 100000, 20000, 10000,  # biaya proses flat Rp 10.000 per order dari profil
    )
    assert out.total_penjualan == Decimal("1450000")  # (600rb + 100rb + packing biasa 20rb) x 2 + 10rb
    assert out.laba_kotor == Decimal("450000")  # - biaya pokok 2 x 500rb

    polos = await svc.create_order(session, order(qty=2, butuh_cat=False))
    assert polos.harga_cat_jasa == 0 and polos.butuh_cat is False
    assert polos.harga_packing == 20000  # polos tetap bayar packing
    assert OrderOut.model_validate(polos).total_penjualan == Decimal("1250000")  # (600rb + 20rb) x 2 + 10rb

    kayu = await svc.create_order(session, order(qty=1, butuh_cat=False, jenis_packing="kayu"))
    assert kayu.harga_packing == 50000
    assert OrderOut.model_validate(kayu).total_penjualan == Decimal("660000")  # 600rb + 50rb + 10rb (biaya proses tetap 10rb walau qty 1)
    await svc.update_order(session, kayu.id, OrderPatch(jenis_packing="biasa"))
    assert kayu.harga_packing == 20000
    with pytest.raises(HTTPException):
        await svc.update_order(session, kayu.id, OrderPatch(jenis_packing="peti"))

    await svc.update_order(session, dicat.id, OrderPatch(butuh_cat=False))  # jadi polos -> cat/jasa nol
    assert dicat.harga_cat_jasa == 0 and dicat.harga_packing == 20000


@pytest.mark.asyncio
async def test_biaya_proses_is_flat_per_order_and_configurable_in_profil(session, data):
    from tenants.bumi_lestari.modules.bumi_lestari.application import services
    from tenants.bumi_lestari.modules.bumi_lestari.application.schemas import ProfilIn

    await svc.set_harga_grosir(session, HargaGrosirIn(produk_id=data.partisi.id, pelanggan_id=data.rina.id, harga=Decimal("600000")))

    def order(qty):
        return OrderIn(
            saluran_id=data.reseller_saluran.id, pelanggan_id=data.rina.id, produk_id=data.partisi.id, qty=qty
        )

    kecil, besar = await svc.create_order(session, order(1)), await svc.create_order(session, order(5))
    assert kecil.biaya_proses == besar.biaya_proses == Decimal("10000")  # flat, tidak tergantung qty/ukuran

    await services.update_profil(session, ProfilIn(nama_usaha="Bumi Lestari", biaya_proses_order=Decimal("12500")))
    assert (await svc.create_order(session, order(1))).biaya_proses == Decimal("12500")
    assert kecil.biaya_proses == Decimal("10000")  # order lama tidak berubah

    eceran = await svc.create_order(session, OrderIn(saluran_id=data.shopee.id, produk_id=data.partisi.id))
    assert eceran.biaya_proses == 0  # saluran lain: harga all-in
