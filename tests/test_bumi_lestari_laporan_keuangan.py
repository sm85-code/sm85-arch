# ruff: noqa: F811
"""Fase 2.13: laba rugi per saluran, neraca dengan pemeriksaan selisih, HPP & margin per order, ringkasan Owner."""
from datetime import date
from decimal import Decimal

import pytest

from tenants.bumi_lestari.modules.bumi_lestari.application import laporan_keuangan as keu
from tenants.bumi_lestari.modules.bumi_lestari.application.laba_core import ringkasan_laba
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlTransaksi
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import BlOrder
from tests.test_bumi_lestari_fase1 import _order_reseller, _order_tukang, _trx, c  # noqa: F401

D = Decimal


async def _sys(c, akun, kategori, jenis, jumlah, tgl):
    """Entri otomatis (kategori sistem) yang sudah terkirim, mis. hasil pencairan / penerimaan / pembayaran pemasok."""
    c.s.add(BlTransaksi(
        tanggal=tgl, akun_id=c.akun[akun].id, kategori_id=c.kat[kategori].id, jenis=jenis, jumlah=D(jumlah),
        dibuat_oleh=c.admin.id, status_kirim="terkirim",
    ))
    await c.s.flush()


async def _buku_september(c):
    c.shopee.akun_id = c.akun["SALDO_SHOPEE"].id
    await c.s.flush()
    await _trx(c, c.admin, "KAS_UTAMA", "Setoran modal", "masuk", "20000000", date(2026, 9, 1))
    await _sys(c, "SALDO_SHOPEE", "Penjualan marketplace", "masuk", "1000000", date(2026, 9, 10))
    await _sys(c, "SALDO_SHOPEE", "Biaya marketplace", "keluar", "100000", date(2026, 9, 10))
    await _sys(c, "KAS_UTAMA", "Penjualan reseller", "masuk", "600000", date(2026, 9, 12))
    await _sys(c, "KAS_UTAMA", "Biaya produksi / pembelian barang", "keluar", "500000", date(2026, 9, 13))
    await _trx(c, c.admin, "KAS_UTAMA", "Transport", "keluar", "50000", date(2026, 9, 14))
    await _trx(c, c.admin, "KAS_UTAMA", "Prive", "keluar", "100000", date(2026, 9, 15))


@pytest.mark.asyncio
async def test_laba_rugi_per_saluran_sama_dengan_laba_bagi_hasil(c):
    await _buku_september(c)
    lr = await keu.laba_rugi(c.s, "2026-09")
    assert {b.label: b.jumlah for b in lr.penjualan} == {"Shopee": D("1000000"), "Penjual lain": D("600000")}
    assert [(b.label, b.jumlah) for b in lr.biaya_marketplace] == [("Shopee", D("100000"))]
    assert (lr.penjualan_bersih, lr.hpp, lr.laba_kotor, lr.margin_persen) == (D("1500000"), D("500000"), D("1000000"), D("62.5"))
    assert lr.biaya_operasional[0].label.startswith("Kas kecil") and lr.total_biaya_operasional == D("50000")
    assert lr.laba_bersih == D("950000") == (await ringkasan_laba(c.s, date(2026, 9, 1), date(2026, 9, 30))).laba
    assert [(b.label, b.jumlah) for b in lr.di_luar_laba] == [("Prive", D("100000"))]
    assert lr.sementara is True and lr.belum_cair.jumlah_order == 0


@pytest.mark.asyncio
async def test_neraca_seimbang_dengan_piutang_utang_dan_belum_cair(c):
    await _buku_september(c)
    await _order_tukang(c, c.tukang, date(2026, 9, 20), "SHP-1")  # diambil, belum dibayar -> utang
    await _order_reseller(c, date(2026, 9, 22))  # dikirim, belum dibayar -> piutang (+ utang tukangnya)
    n = await keu.neraca(c.s, date(2026, 9, 30))
    assert sum(b.jumlah for b in n.aset_kas) == D("20850000")
    assert n.piutang_penjual_lain > 0 and sum(b.jumlah for b in n.utang_pemasok) > 0
    modal = {m.label: m.jumlah for m in n.modal}
    assert modal["Setoran modal"] == D("20000000") and modal["Laba ditahan"] == D("950000") and modal["Prive"] == D("-100000")
    assert n.selisih == 0


@pytest.mark.asyncio
async def test_hpp_margin_per_order_yang_cair_di_bulan_itu(c):
    o = await _order_tukang(c, c.tukang, date(2026, 9, 3), "SHP-9")
    row = await c.s.get(BlOrder, o.id)
    row.status, row.tgl_dikirim, row.status_cair, row.tgl_cair = "dikirim", date(2026, 9, 4), "cair", date(2026, 9, 18)
    await _order_tukang(c, c.tukang, date(2026, 9, 3), "SHP-10")  # belum cair: tidak dihitung
    await c.s.flush()
    m = await keu.hpp_margin(c.s, "2026-09")
    assert m.total.jumlah_order == 1 and m.total.hpp == D(row.biaya_pokok) and m.per_saluran[0].label == "Shopee"
    assert m.total.laba_kotor == m.total.penjualan - m.total.potongan - m.total.hpp
    assert (await keu.hpp_margin(c.s, "2026-10")).total.jumlah_order == 0
    assert (await keu.laba_rugi(c.s, "2026-09")).hpp_dicocokkan == D(row.biaya_pokok)


@pytest.mark.asyncio
async def test_ringkasan_owner(c):
    await _buku_september(c)
    r = await keu.ringkasan_owner(c.s)
    assert r.sementara is True and len(r.tren) == 6 and r.tren[-1].periode == r.periode
    assert r.setoran_modal == D("20000000") and r.total_kas == D("20850000") and r.bagi_hasil == []
    sept = next(t for t in r.tren if t.periode == "2026-09")
    assert sept.laba_bersih == D("950000")
