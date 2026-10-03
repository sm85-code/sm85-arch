"""bumi_lestari Fase 2.3: tutup buku bulanan (kesiapan, kunci bulan, snapshot, buka darurat) & bagi hasil dari bulan
tertutup (spesifikasi 8.10, 8.11; KP-TB-1..4, KP-BH-1..2)."""
# ruff: noqa: F811  -- parameter `c` adalah fixture yang diimpor dari test_bumi_lestari_fase1
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from test_bumi_lestari_fase1 import _kode, _order_tukang, _trx, c  # noqa: F401  (fixture "c" dipakai ulang)
from tenants.bumi_lestari.modules.bumi_lestari.application import kiriman_services as kirim
from tenants.bumi_lestari.modules.bumi_lestari.application import laporan_services as lap
from tenants.bumi_lestari.modules.bumi_lestari.application import pembayaran_services as pembayaran
from tenants.bumi_lestari.modules.bumi_lestari.application import provisi_services as ps
from tenants.bumi_lestari.modules.bumi_lestari.application import services
from tenants.bumi_lestari.modules.bumi_lestari.application import tutup_buku_services as tb
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas import TransferIn
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_pembayaran import KaryawanIn, LanggananIn
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlAuditLog, BlTransaksi

OKT = date(2026, 10, 6)  # hari "sekarang" di tes: Selasa awal Oktober


def _butir(k, kode):
    return next(b for b in k.butir if b.kode == kode)


async def _sept_siap(c):
    """September berisi setoran modal (tidak masuk laba), pemasukan 3jt, dan biaya kas kecil 500rb (draf)."""
    await _trx(c, c.admin, "KAS_UTAMA", "Setoran modal", "masuk", "20000000", date(2026, 9, 1))
    await _trx(c, c.admin, "KAS_UTAMA", "Pemasukan lain", "masuk", "3000000", date(2026, 9, 10))
    await services.catat_pengisian(c.s, c.admin, "kas_kecil", date(2026, 9, 2))
    await _trx(c, c.staf, "KAS_KECIL", "Packing", "keluar", "500000", date(2026, 9, 20))


@pytest.mark.asyncio
async def test_kesiapan_draf_menghalangi_belum_cair_catatan(c):
    await _sept_siap(c)
    k = await tb.kesiapan(c.s, "2026-09", OKT)
    assert k.status == "terbuka"
    assert not _butir(k, "draf").siap and _butir(k, "draf").penghalang
    assert not k.boleh_tutup
    assert _butir(k, "cek_fisik").penghalang is False  # pengingat saja
    assert {b.kode for b in k.butir} >= {"bulan_berakhir", "gaji", "tagihan", "sisihan", "penjual_lain", "tukang", "draf"}
    assert await _kode(c, tb.tutup, c.s, c.admin, "2026-09", OKT) == 409

    berjalan = await tb.kesiapan(c.s, "2026-10", OKT)
    assert not _butir(berjalan, "bulan_berakhir").siap

    await kirim.kirim(c.s, c.admin, "kas_kecil")
    k = await tb.kesiapan(c.s, "2026-09", OKT)
    assert k.boleh_tutup
    assert k.pratinjau.laba_bersih == Decimal("2500000")  # 3jt - 500rb; setoran modal tidak masuk laba
    assert await _kode(c, tb.kesiapan, c.s, "2026-13") == 422


@pytest.mark.asyncio
async def test_kesiapan_gaji_tagihan_sisihan_tukang(c):
    await pembayaran.create_karyawan(c.s, KaryawanIn(nama="Sari", gaji_bulanan=Decimal("2000000")))
    await ps.create_langganan(c.s, LanggananIn(nama="Listrik", jumlah_bulanan=Decimal("400000")))
    await _order_tukang(c, c.tukang, date(2026, 9, 15), "T1")
    k = await tb.kesiapan(c.s, "2026-09", OKT)
    for kode in ("gaji", "tagihan", "sisihan", "tukang"):
        assert not _butir(k, kode).siap, kode
    assert "Listrik" in _butir(k, "tagihan").keterangan
    assert "Selasa ke-1, 2, 3, 4" in _butir(k, "sisihan").keterangan
    assert not k.boleh_tutup


@pytest.mark.asyncio
async def test_tutup_mengunci_bulan_dan_snapshot(c):
    await _sept_siap(c)
    await kirim.kirim(c.s, c.admin, "kas_kecil")
    trx_sept = (await c.s.execute(select(BlTransaksi).where(BlTransaksi.tanggal == date(2026, 9, 10)))).scalar_one()
    row = await tb.tutup(c.s, c.admin, "2026-09", OKT)
    assert row.status == "ditutup" and Decimal(row.snapshot["laba_rugi"]["laba_bersih"]) == Decimal("2500000")
    assert {s["kode"] for s in row.snapshot["saldo_akun"]} >= {"KAS_UTAMA", "KAS_KECIL"}
    assert await _kode(c, tb.tutup, c.s, c.admin, "2026-09", OKT) == 409  # sudah ditutup

    # KP-TB-2: semua penulisan/pembatalan bertanggal September ditolak.
    assert await _kode(c, _trx, c, c.admin, "KAS_UTAMA", "Pemasukan lain", "masuk", "1000", date(2026, 9, 30)) == 409
    assert await _kode(c, services.batalkan_transaksi, c.s, trx_sept.id, "salah", c.admin) == 409
    tf = TransferIn(tanggal=date(2026, 9, 30), dari_akun_id=c.akun["KAS_UTAMA"].id, ke_akun_id=c.akun["DANA_CADANGAN"].id, jumlah=Decimal("1"))
    assert await _kode(c, services.create_transfer, c.s, c.admin, tf) == 409
    kiriman = (await kirim.list_kiriman(c.s, c.admin))[0]
    assert await _kode(c, kirim.batal_kiriman, c.s, c.admin, kiriman.id, "salah kirim") == 409
    # Pengaman tingkat mapper: penulisan langsung tanpa service juga ditolak.
    c.s.add(BlTransaksi(tanggal=date(2026, 9, 5), akun_id=c.akun["KAS_UTAMA"].id, kategori_id=c.kat["Pemasukan lain"].id,
                        jenis="masuk", jumlah=Decimal("1"), dibuat_oleh="a"))
    with pytest.raises(HTTPException) as exc:
        await c.s.flush()
    assert exc.value.status_code == 409 and "September 2026" in exc.value.detail
    await c.s.rollback()


@pytest.mark.asyncio
async def test_laporan_bulan_tertutup_dari_snapshot_dan_koreksi(c):
    await _sept_siap(c)
    await kirim.kirim(c.s, c.admin, "kas_kecil")
    await tb.tutup(c.s, c.admin, "2026-09", OKT)
    # Data berubah di luar aplikasi (mis. perbaikan manual DB): laporan September tetap angka snapshot (KP-TB-3).
    await c.s.execute(text(
        "INSERT INTO bl_transaksi (id, tanggal, akun_id, kategori_id, jenis, jumlah, keterangan, dibuat_oleh, status_kirim, dibatalkan, created_at) "
        f"VALUES ('raw1', '2026-09-15', '{c.akun['KAS_UTAMA'].id}', '{c.kat['Pemasukan lain'].id}', 'masuk', 999, '', 'a', 'terkirim', 0, "
        "'2026-10-01 00:00:00')"
    ))
    sept = await lap.laporan_umum(c.s, c.owner, date(2026, 9, 1), date(2026, 9, 30))
    assert sept.dari_snapshot and sept.laba_bersih == Decimal("2500000")
    assert all(a.jenis != "kas_iklan" for a in sept.arus_kas)  # owner tidak melihat kas iklan
    live = await lap.laporan_umum(c.s, c.owner, date(2026, 9, 1), date(2026, 9, 30), pakai_snapshot=False)
    assert live.laba_bersih == Decimal("2500999")
    sebagian = await lap.laporan_umum(c.s, c.owner, date(2026, 9, 1), date(2026, 9, 15))
    assert not sebagian.dari_snapshot

    # Koreksi bulan lalu: dicatat di bulan berjalan dengan tanda koreksi.
    kor = await _trx(c, c.admin, "KAS_UTAMA", "Operasional", "keluar", "10000", date(2026, 10, 2), koreksi_periode="2026-09")
    assert kor.koreksi_periode == "2026-09"
    for salah in ("2026-10", "2026-08"):
        assert await _kode(c, _trx, c, c.admin, "KAS_UTAMA", "Operasional", "keluar", "1", date(2026, 10, 2), koreksi_periode=salah) == 422


@pytest.mark.asyncio
async def test_bagi_hasil_dari_snapshot_dan_buka_darurat(c):
    await _sept_siap(c)
    await kirim.kirim(c.s, c.admin, "kas_kecil")
    assert await _kode(c, pembayaran.simpan_bagi_hasil, c.s, c.admin, "2026-09") == 409  # KP-BH-1
    await tb.tutup(c.s, c.admin, "2026-09", OKT)
    h = await pembayaran.hitung_bagi_hasil(c.s, "2026-09")
    assert h.final and h.laba_bersih == Decimal("2500000")
    assert (h.bagian_admin, h.bagian_owner) == (Decimal("1000000.00"), Decimal("1500000.00"))  # KP-BH-2

    # Buka darurat sebelum bagi hasil dibayar: boleh, wajib alasan, tercatat di log; bulan bisa ditulis lagi.
    await tb.buka_darurat(c.s, c.admin, "2026-09", "salah catat penjualan")
    log = (await c.s.execute(select(BlAuditLog).where(BlAuditLog.aksi == "buka_tutup_buku"))).scalar_one()
    assert log.alasan == "salah catat penjualan"
    await _trx(c, c.admin, "KAS_UTAMA", "Pemasukan lain", "masuk", "100000", date(2026, 9, 30))
    assert not (await pembayaran.hitung_bagi_hasil(c.s, "2026-09")).final
    row = await tb.tutup(c.s, c.admin, "2026-09", OKT)  # ditutup ulang dengan snapshot baru
    assert Decimal(row.snapshot["laba_rugi"]["laba_bersih"]) == Decimal("2600000")

    bh = await pembayaran.simpan_bagi_hasil(c.s, c.admin, "2026-09")
    await pembayaran.bayar_bagi_hasil(c.s, c.admin, bh.id, date(2026, 10, 6))
    assert await _kode(c, tb.buka_darurat, c.s, c.admin, "2026-09", "ubah lagi") == 409  # KP-TB-4
    assert await _kode(c, tb.buka_darurat, c.s, c.admin, "2026-08", "belum ditutup") == 409
