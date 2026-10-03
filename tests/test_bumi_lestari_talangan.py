"""bumi_lestari Fase 2 (item 1.10): talangan kas kecil/kas iklan + isi ulang di luar jadwal (spesifikasi 8.8, 11.3:
AB-TL-1..3, KP-TL-1/2/4)."""
# ruff: noqa: F811  -- parameter `c` adalah fixture yang diimpor dari test_bumi_lestari_fase1
from datetime import date
from decimal import Decimal

import pytest

from test_bumi_lestari_fase1 import _kode, _modal, _platform, _trx, c  # noqa: F401
from tenants.bumi_lestari.modules.bumi_lestari.application import kiriman_services as kirim
from tenants.bumi_lestari.modules.bumi_lestari.application import laporan_services as lap
from tenants.bumi_lestari.modules.bumi_lestari.application import services
from tenants.bumi_lestari.modules.bumi_lestari.application import talangan_services as tl
from tenants.bumi_lestari.modules.bumi_lestari.application.laba_core import ringkasan_laba
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas import TransferIn
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_talangan import LunasiTalanganIn
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlTransaksi
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_talangan import BlTalangan

SEP = (date(2026, 9, 1), date(2026, 9, 30))


async def _saldo(c, kode, draf=True):
    return await services.saldo_akun(c.s, c.akun[kode], termasuk_draf=draf)


async def _siap(c):
    """Kas kecil terisi 3 jt lalu terpakai 2,9 jt (sisa 100 rb)."""
    await _modal(c)
    await services.catat_pengisian_kas_kecil(c.s, c.owner, date(2026, 9, 1))
    await _trx(c, c.staf, "KAS_KECIL", "Packing", "keluar", "2900000", date(2026, 9, 2))


@pytest.mark.asyncio
async def test_pengeluaran_melebihi_saldo_dipecah_menjadi_talangan(c):
    await _siap(c)
    # Tanpa talangan: ditolak seperti sebelumnya.
    assert await _kode(c, _trx, c, c.staf, "KAS_KECIL", "Packing", "keluar", "300000", date(2026, 9, 3)) == 400
    t = await _trx(c, c.staf, "KAS_KECIL", "Packing", "keluar", "300000", date(2026, 9, 3), keterangan="lakban", talangan_oleh=" Sari ")
    assert t.akun_id == c.akun["KAS_KECIL"].id and t.jumlah == Decimal("100000") and t.ref_jenis is None
    assert await _saldo(c, "KAS_KECIL") == 0
    daftar = await tl.list_talangan(c.s, c.owner, status_filter="belum_lunas")
    assert len(daftar) == 1
    x = daftar[0]
    assert (x.nama, x.jumlah, x.sisa, x.total_pengeluaran, x.status_kirim) == ("Sari", Decimal("200000"), Decimal("200000"), Decimal("300000"), "draf")
    assert await lap.total_talangan_belum_lunas(c.s, c.owner) == Decimal("200000")
    assert "Sari" in await tl.daftar_nama(c.s)

    # Kirim kas kecil: bagian talangan ikut terkirim; biaya penuh 300 rb masuk laba.
    k = await kirim.kirim(c.s, c.owner, "kas_kecil")
    assert k.jumlah_entri == 3
    assert (await _utama(c, x.id, bagian=True)).status_kirim == "terkirim"
    laba = await ringkasan_laba(c.s, *SEP)
    assert laba.total_biaya == Decimal("3200000")
    assert (await tl.list_talangan(c.s, c.owner))[0].status_kirim == "terkirim"
    # TALANGAN tidak tampil sebagai akun kas, tidak bisa dipakai langsung.
    assert "TALANGAN" not in {a.kode for a, _s, _d in await services.list_akun(c.s, c.admin)}
    assert await _saldo(c, "TALANGAN", draf=False) == Decimal("-200000")
    assert await _kode(c, _trx, c, c.admin, "TALANGAN", "Packing", "keluar", "1000") == 400
    tf = TransferIn(dari_akun_id=c.akun["KAS_UTAMA"].id, ke_akun_id=c.akun["TALANGAN"].id, jumlah=Decimal("1000"))
    assert await _kode(c, services.create_transfer, c.s, c.admin, tf) == 400


@pytest.mark.asyncio
async def test_pelunasan_sebagian_dan_batal(c):
    await _siap(c)
    await _trx(c, c.staf, "KAS_KECIL", "Packing", "keluar", "300000", date(2026, 9, 3), talangan_oleh="Sari")
    x = (await tl.list_talangan(c.s, c.owner))[0]
    utama_awal = await _saldo(c, "KAS_UTAMA")
    # Pengeluaran bertalangan tidak bisa dibatalkan dari Kas & transaksi.
    assert await _kode(c, services.batalkan_transaksi, c.s, (await _utama(c, x.id)).id, "salah", c.admin) == 409

    y = await tl.lunasi(c.s, c.owner, x.id, LunasiTalanganIn(jumlah=Decimal("50000"), tanggal=date(2026, 9, 8)))
    assert (y.terbayar, y.sisa) == (Decimal("50000"), Decimal("150000"))
    assert await _kode(c, tl.lunasi, c.s, c.owner, x.id, LunasiTalanganIn(jumlah=Decimal("999999"))) == 400
    # Pelunasan tidak menambah biaya; Kas utama berkurang.
    biaya = (await ringkasan_laba(c.s, *SEP)).total_biaya
    y = await tl.lunasi(c.s, c.owner, x.id, LunasiTalanganIn(tanggal=date(2026, 9, 8)))
    assert y.sisa == 0 and len(y.bayar) == 2
    assert (await ringkasan_laba(c.s, *SEP)).total_biaya == biaya
    assert await _saldo(c, "KAS_UTAMA") == utama_awal - Decimal("200000")
    assert await tl.list_talangan(c.s, c.owner, status_filter="belum_lunas") == []
    assert await _saldo(c, "TALANGAN") == 0
    assert await _kode(c, tl.lunasi, c.s, c.owner, x.id, LunasiTalanganIn()) == 409

    # Transfer pelunasan hanya dibatalkan dari daftar talangan.
    assert await _kode(c, services.batalkan_transfer, c.s, y.bayar[0].transfer_id, "salah", c.admin) == 409
    assert await _kode(c, tl.batal_talangan, c.s, c.admin, x.id, "salah catat") == 409  # sudah ada pelunasan
    for b in y.bayar:
        y = await tl.batal_bayar(c.s, c.admin, b.id, "salah catat")
    assert y.sisa == Decimal("200000")
    y = await tl.batal_talangan(c.s, c.admin, x.id, "salah catat")
    assert y.dibatalkan and y.sisa == 0
    assert await _saldo(c, "KAS_KECIL") == Decimal("100000")  # bagian kas kecil ikut batal
    assert (await ringkasan_laba(c.s, *SEP)).total_biaya == 0  # semua masih draf


async def _utama(c, talangan_id, bagian=False):
    row = await c.s.get(BlTalangan, talangan_id)
    return await c.s.get(BlTransaksi, row.transaksi_talangan_id if bagian else row.transaksi_id)


@pytest.mark.asyncio
async def test_saldo_nol_seluruhnya_talangan_dan_batal_setelah_kirim_ditolak(c):
    await _modal(c)
    t = await _trx(c, c.staf, "KAS_KECIL", "Packing", "keluar", "75000", date(2026, 9, 3), talangan_oleh="Sari")
    assert t.akun_id == c.akun["TALANGAN"].id and t.ref_jenis == "talangan" and t.status_kirim == "draf"
    x = (await tl.list_talangan(c.s, c.owner))[0]
    assert x.total_pengeluaran == x.jumlah == Decimal("75000")
    k = await kirim.kirim(c.s, c.owner, "kas_kecil")
    assert await _kode(c, tl.batal_talangan, c.s, c.admin, x.id, "salah catat") == 409
    await kirim.batal_kiriman(c.s, c.admin, k.id, "koreksi")
    assert (await tl.batal_talangan(c.s, c.admin, x.id, "salah catat")).dibatalkan
    # Kas iklan: talangan hanya terlihat oleh admin.
    await _trx(c, c.admin, "KAS_IKLAN", "Biaya iklan", "keluar", "10000", date(2026, 9, 4), talangan_oleh="Admin", platform_iklan_id=(await _platform(c)).id)
    assert len(await tl.list_talangan(c.s, c.admin, status_filter="belum_lunas")) == 1
    assert await tl.list_talangan(c.s, c.owner, status_filter="belum_lunas") == []


@pytest.mark.asyncio
async def test_talangan_hanya_untuk_akun_imprest(c):
    await _modal(c)
    assert await _kode(c, _trx, c, c.admin, "KAS_UTAMA", "Operasional", "keluar", "1000", talangan_oleh="Sari") == 400


@pytest.mark.asyncio
async def test_isi_ulang_di_luar_jadwal_wajib_alasan(c):
    await _modal(c)
    assert await _kode(c, services.catat_pengisian_kas_kecil, c.s, c.owner, date(2026, 9, 3), di_luar_jadwal=True) == 422
    t = await services.catat_pengisian_kas_kecil(c.s, c.owner, date(2026, 9, 3), di_luar_jadwal=True, alasan="stok packing habis")
    assert t.di_luar_jadwal and t.alasan_luar_jadwal == "stok packing habis" and "di luar jadwal" in t.keterangan
