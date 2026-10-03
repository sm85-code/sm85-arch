"""bumi_lestari: data Purchase Order & Invoice mingguan, dicocokkan dengan contoh PDF asli (Minggu ke-4 September 2026)."""
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.bumi_lestari.modules.bumi_lestari.application import dokumen_services as dok
from tenants.bumi_lestari.modules.bumi_lestari.application import order_services as osvc
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas import ProfilIn
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_order import (
    HargaGrosirIn,
    OrderIn,
    OrderStatusIn,
    PelangganIn,
    PemasokIn,
    ProdukIn,
    SaluranIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.services import update_profil
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_pembayaran  # noqa: F401
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase

SELASA = date(2026, 9, 29)  # Tgl. Pembayaran / Jatuh Tempo di contoh; periode Senin 21 - Sabtu 26 September


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(BumiLestariBase.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def test_info_minggu_matches_sample():
    minggu, no, bulan_tahun = dok.info_minggu(SELASA)
    assert (minggu.label, no) == ("Minggu ke-4 September", 4)
    assert (minggu.periode_awal, minggu.periode_akhir) == (date(2026, 9, 21), date(2026, 9, 26))
    assert dok._nomor("PO", SELASA, "005") == "PO/MG.4-005/IX/2026"
    # Minggu yang memuat tanggal 1 (Senin 28 Sep - Sabtu 3 Okt) = Minggu ke-1 Oktober
    minggu, no, _ = dok.info_minggu(date(2026, 10, 6))
    assert (minggu.label, dok._nomor("INV", date(2026, 10, 6), "002")) == ("Minggu ke-1 Oktober", "INV/MG.1-002/X/2026")


@pytest.mark.asyncio
async def test_purchase_order_matches_sample(session):
    tukang = await osvc.create_pemasok(
        session,
        PemasokIn(nama="AHMAD NUR ALIM", jenis="tukang_kayu", kode="005", nama_bank="Bank Mandiri", no_rekening="1770022968629"),
    )
    palang = await osvc.create_produk(session, ProdukIn(sku="PRP", nama="Partisi Rak Palang", ukuran="100x20x200", harga_jual=Decimal("1")))
    kandang = await osvc.create_produk(session, ProdukIn(sku="KK", nama="Kandang Kelinci", ukuran="120x50x90", harga_jual=Decimal("1")))
    shopee = await osvc.create_saluran(session, SaluranIn(nama="Shopee", jenis="marketplace"))
    for produk, no, tgl, biaya in (
        (palang, "260919E0ABRN4W", date(2026, 9, 21), "425000"),
        (kandang, "260922PJ9B35EN", date(2026, 9, 26), "800000"),
    ):
        o = await osvc.create_order(
            session, OrderIn(saluran_id=shopee.id, produk_id=produk.id, pemasok_id=tukang.id, no_order=no, biaya_pokok=Decimal(biaya), butuh_cat=False)
        )
        for s in ("dikerjakan", "diambil"):
            await osvc.ubah_status_order(session, o.id, OrderStatusIn(status=s, tanggal=tgl))

    (po,) = await dok.po_dari_siap(session, SELASA)
    assert po.nomor == "PO/MG.4-005/IX/2026" and po.minggu.label == "Minggu ke-4 September"
    assert po.tgl_pembayaran == SELASA and po.kepada.no_rekening == "1770022968629"
    assert po.perusahaan.nama == "CV. Bumi Lestari Indonesia"  # September: masih CV
    assert [(i.hari, i.kode_pesanan, i.nama_barang, i.ukuran, i.qty, i.harga_barang, i.total) for i in po.items] == [
        ("Senin", "260919E0ABRN4W", "Partisi Rak Palang", "100x20x200", 1, Decimal("425000"), Decimal("425000")),
        ("Sabtu", "260922PJ9B35EN", "Kandang Kelinci", "120x50x90", 1, Decimal("800000"), Decimal("800000")),
    ]
    assert (po.total_qty, po.grand_total) == (2, Decimal("1225000"))


@pytest.mark.asyncio
async def test_invoice_matches_sample_and_company_name_switches_to_pt(session):
    mandalawangi = await osvc.create_pelanggan(
        session, PelangganIn(nama="MANDALAWANGI", kode="002", alamat="Desa Wonoharjo, Kec. Pangandaran")
    )
    await update_profil(
        session,
        ProfilIn(
            nama_usaha="PT. Bumi Lestari Indonesia", alamat="Jl. Ampel Kuning, Padasuka, Desa Wonoharjo, Kec. Pangandaran, Kab. Pangandaran, Jawa Barat",
            info_pembayaran="QRIS Pangeran Homeware", nama_usaha_lama="CV. Bumi Lestari Indonesia",
            nama_usaha_berlaku_mulai=date(2026, 10, 4),
        ),
    )
    tukang = await osvc.create_pemasok(session, PemasokIn(nama="Pak Budi", jenis="tukang_kayu"))
    saluran = await osvc.create_saluran(session, SaluranIn(nama="Reseller", jenis="reseller"))
    baris = (  # (tanggal, nama, ukuran, barang, jasa pengecatan, tanggal kirim)
        (date(2026, 9, 21), "Partisi Rak Tengah [tanpa rak]", "150x20x200", "600000", "240000"),
        (date(2026, 9, 23), "Partisi Rak Palang", "120x20x200", "475000", "140000"),
        (date(2026, 9, 23), "Partisi Rak Tengah [tanpa rak]", "120x20x200", "550000", "200000"),
        (date(2026, 9, 26), "Partisi Rak Tengah [2 rak]", "150x20x200", "725000", "220000"),
    )
    for i, (tgl, nama, ukuran, barang, jasa) in enumerate(baris):
        produk = await osvc.create_produk(session, ProdukIn(sku=f"S{i}", nama=nama, ukuran=ukuran, harga_jual=Decimal("1")))
        await osvc.set_harga_grosir(
            session, HargaGrosirIn(produk_id=produk.id, pelanggan_id=mandalawangi.id, harga=Decimal(barang), harga_cat_jasa=Decimal(jasa))
        )
        o = await osvc.create_order(
            session, OrderIn(saluran_id=saluran.id, pelanggan_id=mandalawangi.id, produk_id=produk.id, pemasok_id=tukang.id)
        )
        for s in ("dikerjakan", "diambil"):  # tanggal invoice = tanggal barang jadi & diambil dari tukang
            await osvc.ubah_status_order(session, o.id, OrderStatusIn(status=s, tanggal=tgl))
        # dicat & dikirim belakangan (tanggal lain) tidak mengubah tanggal invoice
        for s in ("dicat", "dikirim"):
            await osvc.ubah_status_order(session, o.id, OrderStatusIn(status=s, tanggal=date(2026, 9, 28)))

    (inv,) = await dok.invoice_reseller(session, SELASA)
    assert inv.nomor == "INV/MG.4-002/IX/2026" and inv.minggu.label == "Minggu ke-4 September"
    assert (inv.tgl_invoice, inv.jatuh_tempo) == (date(2026, 9, 26), date(2026, 9, 29))  # Sabtu minggu lalu, Selasa ini
    assert (inv.kepada.nama, inv.kepada.alamat) == ("MANDALAWANGI", "Desa Wonoharjo, Kec. Pangandaran")
    assert [(i.hari, i.nama_barang, i.harga_barang, i.biaya_jasa_pengecatan, i.biaya_proses, i.total) for i in inv.items][0] == (
        "Senin", "Partisi Rak Tengah [tanpa rak]", Decimal("600000"), Decimal("240000"), Decimal("10000"), Decimal("850000"),
    )
    assert [i.total for i in inv.items] == [Decimal(x) for x in ("850000", "625000", "760000", "955000")]
    assert (inv.total_barang, inv.total_jasa_pengecatan, inv.total_biaya_proses, inv.grand_total) == (
        Decimal("2350000"), Decimal("800000"), Decimal("40000"), Decimal("3190000"),
    )
    assert inv.info_pembayaran == "QRIS Pangeran Homeware" and len(inv.syarat) == 2
    assert inv.perusahaan.nama == "CV. Bumi Lestari Indonesia"  # tgl. invoice 26/09/2026: masih CV

    # Dokumen bertanggal mulai 4 Oktober 2026 memakai PT; yang sebelumnya tetap CV.
    assert dok.nama_usaha_pada(await _profil(session), date(2026, 10, 3)).startswith("CV.")
    assert dok.nama_usaha_pada(await _profil(session), date(2026, 10, 4)).startswith("PT.")
    # Invoice minggu 28 Sep-3 Okt bertanggal Sabtu 3 Okt (masih CV); minggu berikutnya bertanggal 10 Okt (PT).
    (pekan_1,) = await dok.invoice_reseller(session, date(2026, 10, 6))
    assert (pekan_1.tgl_invoice, pekan_1.perusahaan.nama) == (date(2026, 10, 3), "CV. Bumi Lestari Indonesia")
    (pekan_2,) = await dok.invoice_reseller(session, date(2026, 10, 13))  # belum dibayar -> terbawa, terlambat
    assert (pekan_2.tgl_invoice, pekan_2.perusahaan.nama) == (date(2026, 10, 10), "PT. Bumi Lestari Indonesia")
    assert pekan_2.items[0].terlambat is True


async def _profil(session):
    from tenants.bumi_lestari.modules.bumi_lestari.application.services import get_profil

    return await get_profil(session)
