"""bumi_lestari Fase 2.4/2.5/2.8: mesin format file penghasilan, tabel standar pencairan, pencocokan per order,
pencatatan bruto, kirim/batal, entri manual iPaymu (spesifikasi 8.3, 10.3; KP-MP-1..8)."""
# ruff: noqa: F811  -- parameter `c` adalah fixture yang diimpor dari test_bumi_lestari_fase1
import io
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from test_bumi_lestari_fase1 import _kode, _order_tukang, c  # noqa: F401
from tenants.bumi_lestari.modules.bumi_lestari.application import kiriman_services as kirim
from tenants.bumi_lestari.modules.bumi_lestari.application import laporan_services as lap
from tenants.bumi_lestari.modules.bumi_lestari.application import order_services as osvc
from tenants.bumi_lestari.modules.bumi_lestari.application import pencairan_format as pf
from tenants.bumi_lestari.modules.bumi_lestari.application import pencairan_services as svc
from tenants.bumi_lestari.modules.bumi_lestari.application import services
from tenants.bumi_lestari.modules.bumi_lestari.application.laba_core import ringkasan_laba
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_order import OrderStatusIn, SaluranIn
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_pencairan import (
    FormatPenghasilanIn,
    KolomPetaIn,
    PencairanManualIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlTransaksi

HEADER = "No. Pesanan;Tanggal Dana Dilepaskan;Harga Asli Produk;Biaya Administrasi;Biaya Layanan;Total Penghasilan;Jenis"
OKT = (date(2026, 10, 1), date(2026, 10, 31))


def _csv(*baris: str, judul: str = "Laporan Penghasilan\n\n") -> bytes:
    return (judul + HEADER + "\n" + "\n".join(baris) + "\n").encode()


def _konfig(**kw) -> pf.KonfigFormat:
    return pf.KonfigFormat(
        kolom=[
            pf.KolomPeta("kode_pesanan", "No. Pesanan"), pf.KolomPeta("tanggal_cair", "Tanggal Dana Dilepaskan"),
            pf.KolomPeta("harga_jual", "Harga Asli Produk"),
            pf.KolomPeta("potongan_biaya", "Biaya Administrasi", nama_rincian="Admin"),
            pf.KolomPeta("potongan_biaya", "Biaya Layanan", nama_rincian="Layanan"),
            pf.KolomPeta("jumlah_cair", "Total Penghasilan"),
        ],
        jenis_file="csv", baris_header=3, aturan_jenis_baris={"kolom": "Jenis", "retur": ["Pengembalian"]}, **kw,
    )


def _payload(saluran_id: str, **kw) -> FormatPenghasilanIn:
    k = _konfig()
    data = dict(
        saluran_id=saluran_id, nama="Shopee uji", jenis_file="csv", baris_header=3, format_tanggal="dd/mm/yyyy",
        aturan_jenis_baris=k.aturan_jenis_baris,
        kolom=[KolomPetaIn(kolom_tujuan=x.kolom_tujuan, kolom_sumber=x.kolom_sumber, nama_rincian=x.nama_rincian) for x in k.kolom],
    )
    data.update(kw)
    return FormatPenghasilanIn(**data)


# ---- mesin format (tanpa database) ----


def test_mesin_format_csv_angka_tanggal_masalah():
    _s, rows = pf.baca_sheet(_csv(
        "SHP-1;24/09/2026;Rp 1.250.000;-30.000;(20.000);1.200.000;Pesanan",
        "SHP-2;31/02/2026;100.000;0;0;100.000;Pesanan",  # tanggal tidak sah
        ";;;;;;",
        "Total;;;;;1.300.000;",
        "SHP-3;25/09/2026;0;0;0;-50.000;Pengembalian",
        "SHP-4;25/09/2026;500.000;-10.000;-5.000;480.000;Pesanan",  # 500rb − 15rb ≠ 480rb
    ), "csv")
    hasil = pf.terapkan(rows, _konfig())
    b1, b3, b4 = hasil.baris
    assert (b1.kode_pesanan, b1.tanggal_cair, b1.harga_jual, b1.potongan_biaya, b1.jumlah_cair) == (
        "SHP-1", date(2026, 9, 24), Decimal("1250000"), Decimal("50000"), Decimal("1200000"))
    assert b1.rincian_biaya == {"Admin": Decimal("30000"), "Layanan": Decimal("20000")} and b1.mode_catat == "bruto"
    assert b3.jenis_baris == "retur" and b3.jumlah_cair == Decimal("-50000")
    assert b4.catatan and "≠ jumlah cair" in b4.catatan[0]
    [m] = hasil.masalah
    assert (m.baris, m.kolom, m.nilai) == (5, "Tanggal Dana Dilepaskan", "31/02/2026")
    assert m.teks().startswith("baris 5, Tanggal Dana Dilepaskan: '31/02/2026'")


def test_mesin_format_neto_per_produk_kolom_hilang():
    neto = pf.KonfigFormat(
        kolom=[pf.KolomPeta("kode_pesanan", "No. Pesanan"), pf.KolomPeta("tanggal_cair", "Tanggal Dana Dilepaskan"),
               pf.KolomPeta("jumlah_cair", "Total Penghasilan")],
        jenis_file="csv", baris_header=3, satuan_baris="per_produk",
    )
    _s, rows = pf.baca_sheet(_csv("A;24/09/2026;;;;100.000;", "A;26/09/2026;;;;50.000;", "B;24/09/2026;;;;-10.000;"), "csv")
    a, b = pf.terapkan(rows, neto).baris
    assert (a.jumlah_cair, a.tanggal_cair, a.mode_catat, a.harga_jual) == (Decimal("150000"), date(2026, 9, 26), "neto", None)
    assert b.jenis_baris == "penyesuaian"  # negatif tanpa kolom jenis
    with pytest.raises(pf.FormatError, match="Kolom 'Saldo' tidak ditemukan — format file mungkin berubah"):
        pf.terapkan(rows, pf.KonfigFormat(kolom=[pf.KolomPeta("kode_pesanan", "Saldo")], baris_header=3))
    assert pf.cek_konfig(pf.KonfigFormat(kolom=[pf.KolomPeta("kode_pesanan", "x"), pf.KolomPeta("tanggal_cair", "y")]))


def test_baca_header_xlsx_saran():
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Income"
    ws.append(["Laporan Penghasilan"])
    ws.append([])
    ws.append(["No. Pesanan", "Tanggal Dana Dilepaskan", "Harga Asli Produk", "Total Penghasilan"])
    ws.append(["SHP-1", date(2026, 9, 24), 850000, 800000])
    buf = io.BytesIO()
    wb.save(buf)
    out = svc.baca_header(buf.getvalue(), "income.xlsx")
    assert out.sheets == ["Income"] and out.baris_header == 3
    assert out.saran["kode_pesanan"] == "No. Pesanan" and out.saran["jumlah_cair"] == "Total Penghasilan"
    _s, rows = pf.baca_sheet(buf.getvalue(), "xlsx", "Income")
    konfig = pf.KonfigFormat(
        kolom=[pf.KolomPeta("kode_pesanan", "No. Pesanan"), pf.KolomPeta("tanggal_cair", "B"),
               pf.KolomPeta("harga_jual", "Harga Asli Produk"), pf.KolomPeta("jumlah_cair", "Total Penghasilan")],
        baris_header=3,
    )
    [b] = pf.terapkan(rows, konfig).baris
    assert (b.tanggal_cair, b.potongan_biaya, b.rincian_biaya) == (date(2026, 9, 24), Decimal("50000"), {"Potongan marketplace": Decimal("50000")})


# ---- alur pencairan (database) ----


async def _siap(c):
    c.shopee.akun_id = c.akun["SALDO_SHOPEE"].id
    fmt = await svc.create_format(c.s, c.admin, _payload(c.shopee.id))
    assert (await svc.uji_format(c.s, fmt.id, _csv("SHP-1;24/09/2026;850.000;-30.000;-20.000;800.000;Pesanan"), "uji.csv")).lulus
    await svc.aktifkan_format(c.s, c.admin, fmt.id)
    orders = {}
    for no in ("SHP-1", "SHP-2", "SHP-3"):
        o = await _order_tukang(c, c.tukang, date(2026, 9, 22), no)
        o.potongan_marketplace = Decimal("50000")
        await osvc.ubah_status_order(c.s, o.id, OrderStatusIn(status="dikirim", tanggal=date(2026, 9, 23)))
        orders[no] = o
    return fmt, orders


FILE_OKT = _csv(
    "SHP-1;02/10/2026;850.000;-30.000;-20.000;800.000;Pesanan",  # cocok (850rb − 50rb)
    "SHP-2;02/10/2026;850.000;-40.000;-20.000;790.000;Pesanan",  # selisih −10rb
    "SHP-9;02/10/2026;100.000;0;0;100.000;Pesanan",  # tidak cocok
)


@pytest.mark.asyncio
async def test_format_versi_dan_aktifkan(c):
    fmt = await svc.create_format(c.s, c.admin, _payload(c.shopee.id))
    assert fmt.versi == 1 and fmt.status == "draf"
    assert await _kode(c, svc.aktifkan_format, c.s, c.admin, fmt.id) == 409  # belum lulus uji
    salah = _payload(c.shopee.id, kolom=[KolomPetaIn(kolom_tujuan="kode_pesanan", kolom_sumber="No. Pesanan"),
                                         KolomPetaIn(kolom_tujuan="tanggal_cair", kolom_sumber="Tgl")])
    assert await _kode(c, svc.create_format, c.s, c.admin, salah) == 422
    await svc.uji_format(c.s, fmt.id, _csv("SHP-1;24/09/2026;850.000;-30.000;-20.000;800.000;Pesanan"), "uji.csv")
    await svc.aktifkan_format(c.s, c.admin, fmt.id)
    assert await _kode(c, svc.update_format, c.s, fmt.id, _payload(c.shopee.id)) == 409
    v2 = await svc.versi_baru(c.s, c.admin, fmt.id)
    assert (v2.versi, v2.status, v2.lulus_uji) == (2, "draf", False)
    await svc.uji_format(c.s, v2.id, _csv("SHP-1;24/09/2026;850.000;-30.000;-20.000;800.000;Pesanan"), "uji.csv")
    await svc.aktifkan_format(c.s, c.admin, v2.id)
    assert {f.versi: f.status for f in await svc.list_format(c.s, c.shopee.id)} == {1: "arsip", 2: "aktif"}
    # File dengan kolom berubah: pesan jelas.
    hilang = "No. Pesanan;Tanggal;Total\nSHP-1;24/09/2026;1\n".encode()
    with pytest.raises(Exception) as exc:
        await svc.pratinjau(c.s, c.shopee.id, b"x\n\n" + hilang, "baru.csv")
    assert "tidak ditemukan — format file mungkin berubah" in exc.value.detail


@pytest.mark.asyncio
async def test_pratinjau_simpan_kirim_bruto_dan_duplikat(c):
    _fmt, orders = await _siap(c)
    p = await svc.pratinjau(c.s, c.shopee.id, FILE_OKT, "okt.csv")
    assert {k: v.jumlah for k, v in p.kelompok.items()} == {"cocok": 1, "selisih": 1, "tidak_cocok": 1, "duplikat": 0, "penyesuaian": 0}
    assert next(b for b in p.baris if b.kode_pesanan == "SHP-2").selisih == Decimal("-10000")
    assert p.total_dibukukan == Decimal("1590000") and p.jumlah_disimpan == 3

    u = await svc.simpan(c.s, c.admin, c.shopee.id, FILE_OKT, "okt.csv")
    assert (u.status_kirim, u.total, u.total_harga_jual, u.total_potongan) == ("draf", Decimal("1590000"), Decimal("1700000"), Decimal("110000"))
    trx = (await c.s.execute(select(BlTransaksi).where(BlTransaksi.ref_id == u.id))).scalars().all()
    assert {(t.jenis, t.keterangan.split(":")[-1].strip() if "Biaya" in t.keterangan else "jual", t.jumlah) for t in trx} == {
        ("masuk", "jual", Decimal("1700000")),
        ("keluar", "Admin (02-10-2026)", Decimal("70000")),
        ("keluar", "Layanan (02-10-2026)", Decimal("40000")),
    }
    assert all(t.status_kirim == "draf" for t in trx)
    assert orders["SHP-1"].status_cair == "belum"  # baru ditandai saat dikirim

    # Unggah ulang (tumpang tindih): tidak menggandakan (KP-MP-3).
    ulang = await svc.pratinjau(c.s, c.shopee.id, FILE_OKT, "okt.csv")
    assert (ulang.kelompok["duplikat"].jumlah, ulang.kelompok["tidak_cocok"].jumlah) == (2, 1)

    await kirim.kirim(c.s, c.admin, "pencairan")
    assert (orders["SHP-1"].status_cair, orders["SHP-1"].tgl_cair, orders["SHP-1"].potongan_aktual) == ("cair", date(2026, 10, 2), Decimal("50000"))
    assert orders["SHP-2"].potongan_aktual == Decimal("60000") and orders["SHP-3"].status_cair == "belum"
    laba = await ringkasan_laba(c.s, *OKT)
    assert {b.kategori: b.jumlah for b in laba.pemasukan} == {"Penjualan marketplace": Decimal("1700000")}
    assert {b.kategori: b.jumlah for b in laba.biaya} == {"Biaya marketplace": Decimal("110000")}
    assert await services.saldo_akun(c.s, c.akun["SALDO_SHOPEE"]) == Decimal("1590000")
    assert [o.order_id for g in (await lap.belum_cair(c.s, date(2026, 10, 3))).per_saluran for o in g.order] == [orders["SHP-3"].id]

    # Batal: kiriman dulu, baru unggahan (KP-MP-7).
    assert await _kode(c, svc.batal_unggahan, c.s, c.admin, u.id, "salah file") == 409
    [kr] = await kirim.list_kiriman(c.s, c.admin, "pencairan")
    await kirim.batal_kiriman(c.s, c.admin, kr.id, "salah file")
    assert orders["SHP-1"].status_cair == "belum" and orders["SHP-1"].potongan_aktual is None
    await svc.batal_unggahan(c.s, c.admin, u.id, "salah file")
    assert all(t.dibatalkan for t in trx)
    lagi = await svc.pratinjau(c.s, c.shopee.id, FILE_OKT, "okt.csv")
    assert lagi.kelompok["duplikat"].jumlah == 0  # boleh diunggah ulang setelah dibatalkan


@pytest.mark.asyncio
async def test_hubungkan_baris_tidak_cocok_dan_retur_dari_file(c):
    _fmt, orders = await _siap(c)
    u = await svc.simpan(c.s, c.admin, c.shopee.id, _csv("SHP-03;02/10/2026;850.000;-30.000;-20.000;800.000;Pesanan"), "a.csv")
    [r] = await svc.baris_unggahan(c.s, u.id)
    assert r.status_cocok == "tidak_cocok" and u.total == Decimal("0")  # tidak dibukukan
    await svc.hubungkan(c.s, c.admin, r.id, orders["SHP-3"].id)
    assert r.status_cocok == "cocok" and u.total == Decimal("800000")
    await kirim.kirim(c.s, c.admin, "pencairan")
    assert orders["SHP-3"].status_cair == "cair"

    retur = _csv("SHP-3;05/10/2026;0;0;0;-800.000;Pengembalian")
    p = await svc.pratinjau(c.s, c.shopee.id, retur, "retur.csv")
    assert p.kelompok["penyesuaian"].jumlah == 1
    await svc.simpan(c.s, c.admin, c.shopee.id, retur, "retur.csv")
    kr2 = await kirim.kirim(c.s, c.admin, "pencairan")
    assert (orders["SHP-3"].status, orders["SHP-3"].tgl_retur) == ("retur", date(2026, 10, 5))
    laba = await ringkasan_laba(c.s, *OKT)
    assert {b.kategori: b.jumlah for b in laba.pemasukan} == {"Penjualan marketplace": Decimal("50000")}  # 850rb − 800rb retur
    await kirim.batal_kiriman(c.s, c.admin, kr2.id, "salah")
    assert orders["SHP-3"].status == "dikirim" and orders["SHP-3"].tgl_retur is None


@pytest.mark.asyncio
async def test_entri_manual_ipaymu(c):
    web = await osvc.create_saluran(c.s, SaluranIn(nama="Toko web", jenis="web"))
    web.akun_id = c.akun["SALDO_IPAYMU"].id
    o = await _order_tukang(c, c.tukang, date(2026, 9, 22), "WEB-1")
    o.saluran_id = web.id
    await osvc.ubah_status_order(c.s, o.id, OrderStatusIn(status="dikirim", tanggal=date(2026, 9, 23)))
    data = dict(saluran_id=web.id, kode_pesanan="WEB-1", tanggal_cair=date(2026, 10, 2), harga_jual=Decimal("850000"), potongan=Decimal("8500"))
    assert await _kode(c, svc.entri_manual, c.s, c.admin, PencairanManualIn(**{**data, "saluran_id": c.shopee.id})) == 400
    assert await _kode(c, svc.entri_manual, c.s, c.admin, PencairanManualIn(**{**data, "kode_pesanan": "WEB-X"})) == 400
    u = await svc.entri_manual(c.s, c.admin, PencairanManualIn(**data))
    assert u.total == Decimal("841500") and u.nama_file == "Catat manual Toko web" and u.sumber_sistem == "manual"
    assert await _kode(c, svc.entri_manual, c.s, c.admin, PencairanManualIn(**data)) == 409
    await kirim.kirim(c.s, c.admin, "pencairan")
    laba = await ringkasan_laba(c.s, *OKT)
    assert {b.kategori: b.jumlah for b in laba.pemasukan} == {"Penjualan toko web": Decimal("850000")}
    assert {b.kategori: b.jumlah for b in laba.biaya} == {"Biaya marketplace": Decimal("8500")}
    assert o.status_cair == "cair"


@pytest.mark.asyncio
async def test_penjualan_marketplace_manual_tetap_ditolak(c):
    from test_bumi_lestari_fase1 import _trx

    assert await _kode(c, _trx, c, c.admin, "SALDO_SHOPEE", "Penjualan marketplace", "masuk", "100000") in (400, 422)
