# ruff: noqa: F811  -- parameter `c` adalah fixture yang diimpor dari test_bumi_lestari_fase1
"""Harga jual produk opsional (harga acuan), urutan harga order, margin produk tanpa biaya proses,
dan Catat manual pencairan untuk semua saluran marketplace (masa transisi)."""
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from tenants.bumi_lestari.modules.bumi_lestari.application import kiriman_services as kirim
from tenants.bumi_lestari.modules.bumi_lestari.application import laporan_keuangan as keu
from tenants.bumi_lestari.modules.bumi_lestari.application import laporan_services as lap
from tenants.bumi_lestari.modules.bumi_lestari.application import order_services as osvc
from tenants.bumi_lestari.modules.bumi_lestari.application import pembayaran_services as pembayaran
from tenants.bumi_lestari.modules.bumi_lestari.application import pencairan_services as svc
from tenants.bumi_lestari.modules.bumi_lestari.application.laba_core import ringkasan_laba
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_order import (
    HargaGrosirIn,
    OrderIn,
    OrderOut,
    OrderStatusIn,
    ProdukIn,
    ProdukOut,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_pembayaran import PenerimaanResellerIn
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_pencairan import PencairanManualIn
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_pencairan import BlPencairanUnggahan
from tests.test_bumi_lestari_fase1 import _kode, _order_reseller, _order_tukang, _trx, c  # noqa: F401
from tests.test_bumi_lestari_pencairan import _csv, _payload

D = Decimal
OKT = (date(2026, 10, 1), date(2026, 10, 31))


async def _rak(c):
    return await osvc.create_produk(c.s, ProdukIn(sku="RAK", nama="Rak", biaya_pokok_default=D("300000")))


async def _kirim_order(c, o, tgl=date(2026, 9, 23)):
    for st in ("dikerjakan", "diambil"):
        await osvc.ubah_status_order(c.s, o.id, OrderStatusIn(status=st, tanggal=tgl))
    await osvc.ubah_status_order(c.s, o.id, OrderStatusIn(status="dikirim", tanggal=tgl))
    return o


@pytest.mark.asyncio
async def test_produk_tanpa_harga_jual_dan_urutan_harga_order(c):
    rak = await _rak(c)
    assert rak.harga_jual is None and ProdukOut.model_validate(rak).harga_jual is None
    # Marketplace: harga tidak wajib -> 0, mengikuti file penghasilan.
    mp = await osvc.create_order(c.s, OrderIn(no_order="SHP-50", saluran_id=c.shopee.id, produk_id=rak.id, butuh_cat=False))
    assert mp.harga_satuan == 0 and OrderOut.model_validate(mp).total_penjualan == 0
    # Penjual lain tanpa harga grosir & tanpa harga acuan: wajib harga satuan, pesan ramah.
    base = dict(saluran_id=c.res.id, pelanggan_id=c.rina.id, produk_id=rak.id, butuh_cat=False)
    with pytest.raises(HTTPException) as exc:
        await osvc.create_order(c.s, OrderIn(**base))
    assert exc.value.status_code == 422 and "Harga untuk Rak belum ada" in exc.value.detail
    assert (await osvc.create_order(c.s, OrderIn(**base, harga_satuan=D("450000")))).harga_satuan == D("450000")
    # Harga grosir penjual lain dipakai bila ada; harga eksplisit tetap menang.
    await osvc.set_harga_grosir(c.s, HargaGrosirIn(produk_id=rak.id, pelanggan_id=c.rina.id, harga=D("400000")))
    assert (await osvc.create_order(c.s, OrderIn(**base))).harga_satuan == D("400000")
    assert (await osvc.create_order(c.s, OrderIn(**base, harga_satuan=D("420000")))).harga_satuan == D("420000")
    # Harga acuan produk dipakai saat tidak ada harga grosir (produk lama tetap seperti sebelumnya).
    o = await osvc.create_order(c.s, OrderIn(no_order="SHP-51", saluran_id=c.shopee.id, produk_id=c.partisi.id, butuh_cat=False))
    assert o.harga_satuan == D("850000")


@pytest.mark.asyncio
async def test_laporan_tidak_rusak_dan_margin_dari_harga_file(c):
    c.shopee.akun_id = c.akun["SALDO_SHOPEE"].id
    rak = await _rak(c)
    o = await osvc.create_order(
        c.s, OrderIn(no_order="SHP-60", saluran_id=c.shopee.id, produk_id=rak.id, butuh_cat=False, pemasok_id=c.tukang.id),
    )
    await _kirim_order(c, o)
    bc = await lap.belum_cair(c.s, date(2026, 9, 30))
    assert bc.jumlah_order == 1 and bc.total_penjualan == 0  # harga mengikuti file: 0 sampai cair
    await keu.laba_rugi(c.s, "2026-09")
    # Catat manual (marketplace): harga order 0 -> dianggap cocok, tidak selisih.
    u = await svc.entri_manual(c.s, c.admin, PencairanManualIn(
        saluran_id=c.shopee.id, kode_pesanan="shp-60", tanggal_cair=date(2026, 10, 2),
        harga_jual=D("900000"), potongan=D("60000"),
    ))
    assert (u.sumber_sistem, u.status_kirim, u.total, u.total_harga_jual) == ("manual", "draf", D("840000"), D("900000"))
    baris = (await svc.baris_unggahan(c.s, u.id))[0]
    assert (baris.status_cocok, baris.selisih, baris.rincian_biaya) == ("cocok", 0, {"Potongan biaya": "60000"})
    await kirim.kirim(c.s, c.admin, "pencairan")
    assert (o.status_cair, o.potongan_aktual, o.pencairan_baris_id) == ("cair", D("60000"), baris.id)
    laba = await ringkasan_laba(c.s, *OKT)
    assert {b.kategori: b.jumlah for b in laba.pemasukan} == {"Penjualan marketplace": D("900000")}  # bruto
    assert {b.kategori: b.jumlah for b in laba.biaya} == {"Biaya marketplace": D("60000")}
    m = await keu.hpp_margin(c.s, "2026-10")
    t = m.total
    assert (t.penjualan, t.potongan, t.hpp, t.laba_kotor) == (D("900000"), D("60000"), D("300000"), D("540000"))
    await keu.laba_rugi(c.s, "2026-10")


@pytest.mark.asyncio
async def test_margin_produk_tanpa_biaya_proses(c):
    o = await _order_reseller(c, date(2026, 9, 22))  # 600rb + biaya proses 10rb, biaya pokok 500rb
    out = OrderOut.model_validate(o)
    assert (out.pendapatan_produk, out.total_penjualan, out.laba_kotor) == (D("600000"), D("610000"), D("100000"))
    assert pembayaran.tagihan_order(o) == D("610000")  # tagihan penjual lain tetap termasuk biaya proses
    await pembayaran.buat_penerimaan_reseller(
        c.s, c.admin, PenerimaanResellerIn(pelanggan_id=c.rina.id, order_ids=[o.id], tanggal=date(2026, 9, 29)),
    )
    await kirim.kirim(c.s, c.admin, "penerimaan_reseller")
    m = await keu.hpp_margin(c.s, "2026-09")
    pl = next(b for b in m.per_saluran if b.label == "Penjual lain")
    assert (pl.penjualan, pl.hpp, pl.laba_kotor, pl.biaya_proses) == (D("600000"), D("500000"), D("100000"), D("10000"))
    assert m.pendapatan_biaya_proses == D("10000") and m.per_produk[0].laba_kotor == D("100000")
    lr = await keu.laba_rugi(c.s, "2026-09")
    jual = {b.label: b.jumlah for b in lr.penjualan}
    assert jual == {"Penjual lain": D("600000"), "Pendapatan biaya proses": D("10000")}
    assert lr.total_penjualan == D("610000") and lr.pendapatan_biaya_proses == D("10000")


async def _order_shopee_dikirim(c, no):
    o = await _order_tukang(c, c.tukang, date(2026, 9, 22), no)
    await osvc.ubah_status_order(c.s, o.id, OrderStatusIn(status="dikirim", tanggal=date(2026, 9, 23)))
    return o


async def _format_aktif(c):
    fmt = await svc.create_format(c.s, c.admin, _payload(c.shopee.id))
    await svc.uji_format(c.s, fmt.id, _csv("SHP-1;24/09/2026;850.000;-30.000;-20.000;800.000;Pesanan"), "uji.csv")
    await svc.aktifkan_format(c.s, c.admin, fmt.id)


@pytest.mark.asyncio
async def test_catat_manual_marketplace_lalu_file_tidak_dobel(c):
    c.shopee.akun_id = c.akun["SALDO_SHOPEE"].id
    await _format_aktif(c)
    o1, o2 = await _order_shopee_dikirim(c, "SHP-1"), await _order_shopee_dikirim(c, "SHP-2")
    manual = dict(saluran_id=c.shopee.id, tanggal_cair=date(2026, 10, 1), harga_jual=D("850000"), potongan=D("50000"))
    # Penjual lain tidak lewat Catat manual pencairan.
    assert await _kode(c, svc.entri_manual, c.s, c.admin, PencairanManualIn(**{**manual, "saluran_id": c.res.id, "kode_pesanan": "X"})) == 400
    m1 = await svc.entri_manual(c.s, c.admin, PencairanManualIn(**manual, kode_pesanan="SHP-1"))
    assert await _kode(c, svc.entri_manual, c.s, c.admin, PencairanManualIn(**manual, kode_pesanan="SHP-1")) == 409

    file = _csv(
        "SHP-1;02/10/2026;850.000;-30.000;-20.000;800.000;Pesanan",  # tanggal/jumlah beda dari entri manual
        "SHP-2;02/10/2026;850.000;-30.000;-20.000;800.000;Pesanan",
    )
    p = await svc.pratinjau(c.s, c.shopee.id, file, "okt.csv")
    b1 = next(b for b in p.baris if b.kode_pesanan == "SHP-1")
    assert (b1.kelompok, b1.alasan, b1.dicatat_manual, b1.manual_unggahan_id, b1.manual_bisa_diganti) == (
        "duplikat", "sudah dicatat manual", True, m1.id, True,
    )
    assert (p.sudah_manual, p.manual_bisa_diganti, p.jumlah_disimpan, p.total_dibukukan) == (1, 1, 1, D("800000"))

    # Bawaan: dilewati, tidak dihitung dua kali.
    u = await svc.simpan(c.s, c.admin, c.shopee.id, file, "okt.csv")
    assert u.jumlah_baris == 1 and not m1.dibatalkan
    await kirim.kirim(c.s, c.admin, "pencairan")
    laba = await ringkasan_laba(c.s, *OKT)
    assert {b.kategori: b.jumlah for b in laba.pemasukan} == {"Penjualan marketplace": D("1700000")}
    assert o1.status_cair == o2.status_cair == "cair"
    # Sudah dikirim: tetap dilewati dan tidak bisa diganti.
    p2 = await svc.pratinjau(c.s, c.shopee.id, file, "okt.csv")
    b1 = next(b for b in p2.baris if b.kode_pesanan == "SHP-1")
    assert b1.dicatat_manual and not b1.manual_bisa_diganti and "sudah dikirim" in b1.alasan


@pytest.mark.asyncio
async def test_file_bisa_mengganti_entri_manual_yang_masih_draf(c):
    c.shopee.akun_id = c.akun["SALDO_SHOPEE"].id
    await _format_aktif(c)
    o1 = await _order_shopee_dikirim(c, "SHP-1")
    m1 = await svc.entri_manual(c.s, c.admin, PencairanManualIn(
        saluran_id=c.shopee.id, kode_pesanan="SHP-1", tanggal_cair=date(2026, 10, 1), harga_jual=D("850000"), potongan=D("40000"),
    ))
    file = _csv("SHP-1;02/10/2026;850.000;-30.000;-20.000;800.000;Pesanan")
    u = await svc.simpan(c.s, c.admin, c.shopee.id, file, "okt.csv", ganti_manual=True)
    assert m1.dibatalkan and u.jumlah_baris == 1 and u.total == D("800000")
    await kirim.kirim(c.s, c.admin, "pencairan")
    laba = await ringkasan_laba(c.s, *OKT)
    assert {b.kategori: b.jumlah for b in laba.pemasukan} == {"Penjualan marketplace": D("850000")}
    assert (o1.status_cair, o1.potongan_aktual) == ("cair", D("50000"))
    aktif = (await c.s.execute(select(BlPencairanUnggahan).where(BlPencairanUnggahan.dibatalkan.is_(False)))).scalars().all()
    assert [x.id for x in aktif] == [u.id]


@pytest.mark.asyncio
async def test_penjualan_marketplace_tidak_lewat_catat_manual_kas(c):
    with pytest.raises(HTTPException) as exc:
        await _trx(c, c.admin, "SALDO_SHOPEE", "Penjualan marketplace", "masuk", "100000")
    assert exc.value.status_code == 422 and "Pencairan > Catat manual" in exc.value.detail
