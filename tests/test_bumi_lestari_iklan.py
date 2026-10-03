"""bumi_lestari Fase 2.9/2.10: platform iklan, budget 25/75, top up, pengembalian kas iklan, plafon & lognya
(spesifikasi 8.7: AB-KI-1..6, KP-KI-1..3)."""
# ruff: noqa: F811  -- parameter `c` adalah fixture yang diimpor dari test_bumi_lestari_fase1
from datetime import date
from decimal import Decimal

import pytest

from test_bumi_lestari_fase1 import _kode, _modal, _trx, c  # noqa: F401
from tenants.bumi_lestari.modules.bumi_lestari.application import iklan_core
from tenants.bumi_lestari.modules.bumi_lestari.application import iklan_services as svc
from tenants.bumi_lestari.modules.bumi_lestari.application import kiriman_services as kirim
from tenants.bumi_lestari.modules.bumi_lestari.application import services
from tenants.bumi_lestari.modules.bumi_lestari.application.laba_core import ringkasan_laba
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_iklan import (
    PengaturanIklanIn,
    PengembalianIklanIn,
    PlafonIn,
    PlatformIklanIn,
    PlatformIklanPatch,
    TopupIklanIn,
)

SEP = (date(2026, 9, 1), date(2026, 9, 30))


async def _siap(c):
    await _modal(c)
    await services.catat_pengisian(c.s, c.admin, "kas_iklan", date(2026, 9, 1))  # 2 jt
    shopee = await svc.create_platform(c.s, PlatformIklanIn(nama="Shopee", grup="internal", saluran_id=c.shopee.id))
    meta = await svc.create_platform(c.s, PlatformIklanIn(nama="Meta", grup="eksternal"))
    return shopee, meta


def test_jumlah_selasa():
    assert iklan_core.jumlah_selasa(2026, 9) == 5 and iklan_core.jumlah_selasa(2026, 10) == 4


@pytest.mark.asyncio
async def test_topup_wajib_platform_budget_dan_tanda_melebihi_porsi(c):
    shopee, meta = await _siap(c)
    # Budget bawaan = plafon 2 jt × 5 Selasa (Sep 2026) = 10 jt → internal 2,5 jt, eksternal 7,5 jt.
    b = await svc.sisa_budget(c.s, date(2026, 9, 10))
    assert (b.budget_total, b.dasar) == (Decimal("10000000"), "plafon")
    assert [(g.grup, g.budget) for g in b.grup] == [("internal", Decimal("2500000.00")), ("eksternal", Decimal("7500000.00"))]

    # KP-KI-1: tanpa platform ditolak (lewat transaksi generik juga).
    assert await _kode(c, _trx, c, c.admin, "KAS_IKLAN", "Biaya iklan", "keluar", "1000", date(2026, 9, 2)) == 422
    assert await _kode(c, _trx, c, c.admin, "KAS_UTAMA", "Operasional", "keluar", "1000", date(2026, 9, 2), platform_iklan_id=meta.id) == 400

    t = await svc.topup(c.s, c.admin, TopupIklanIn(tanggal=date(2026, 9, 2), platform_iklan_id=shopee.id, jumlah=Decimal("1500000"), keterangan="iklan toko"))
    assert (t.status_kirim, t.platform_iklan_id, t.melebihi_porsi) == ("draf", shopee.id, False)
    # Sisa budget sudah memperhitungkan draf.
    b = await svc.sisa_budget(c.s, date(2026, 9, 10))
    assert b.grup[0].terpakai == Decimal("1500000") and b.grup[0].sisa == Decimal("1000000.00")
    # Melebihi porsi tetap tersimpan, dengan tanda (KP-KI-2).
    await services.catat_pengisian(c.s, c.admin, "kas_iklan", date(2026, 9, 8))
    t2 = await svc.topup(c.s, c.admin, TopupIklanIn(tanggal=date(2026, 9, 8), platform_iklan_id=shopee.id, jumlah=Decimal("1200000")))
    assert t2.melebihi_porsi
    # Bulan lain: budget baru.
    assert (await svc.sisa_budget(c.s, date(2026, 10, 1))).grup[0].terpakai == 0

    # Top up = biaya iklan, masuk laba setelah dikirim.
    assert (await ringkasan_laba(c.s, *SEP)).total_biaya == 0
    await kirim.kirim(c.s, c.admin, "kas_iklan")
    assert (await ringkasan_laba(c.s, *SEP)).total_biaya == Decimal("2700000")

    # Platform nonaktif tidak bisa dipakai; nama unik.
    await svc.update_platform(c.s, meta.id, PlatformIklanPatch(aktif=False))
    assert await _kode(c, svc.topup, c.s, c.admin, TopupIklanIn(platform_iklan_id=meta.id, jumlah=Decimal("1000"))) == 422
    assert await _kode(c, svc.create_platform, c.s, PlatformIklanIn(nama="Shopee", grup="internal")) == 409


@pytest.mark.asyncio
async def test_pengaturan_porsi_dan_budget(c):
    await _siap(c)
    assert await _kode(c, svc.set_pengaturan, c.s, c.admin, PengaturanIklanIn(porsi_internal=Decimal("30"), porsi_eksternal=Decimal("60"))) == 422
    p = await svc.set_pengaturan(c.s, c.admin, PengaturanIklanIn(porsi_internal=Decimal("40"), porsi_eksternal=Decimal("60"), budget_bulanan=Decimal("5000000")))
    assert p.budget_bulanan == Decimal("5000000")
    b = await svc.sisa_budget(c.s, date(2026, 9, 10))
    assert b.dasar == "pengaturan" and [g.budget for g in b.grup] == [Decimal("2000000.00"), Decimal("3000000.00")]


@pytest.mark.asyncio
async def test_plafon_turun_kelebihan_dikembalikan_dan_log(c):
    await _siap(c)
    iklan = c.akun["KAS_IKLAN"]
    log = await svc.ubah_plafon(c.s, c.admin, iklan.id, PlafonIn(plafon=Decimal("1500000"), alasan="iklan dikurangi"))
    assert (log.dari, log.ke, log.oleh) == (Decimal("2000000"), Decimal("1500000"), c.admin.id)
    assert await _kode(c, svc.ubah_plafon, c.s, c.owner, iklan.id, PlafonIn(plafon=Decimal("1"))) == 403  # kas iklan khusus admin
    assert await _kode(c, svc.ubah_plafon, c.s, c.admin, c.akun["KAS_UTAMA"].id, PlafonIn(plafon=Decimal("1"))) == 400
    # Kelebihan 500 rb dikembalikan ke Kas utama (bukan biaya).
    utama = await services.saldo_akun(c.s, c.akun["KAS_UTAMA"])
    t = await svc.pengembalian(c.s, c.admin, PengembalianIklanIn(tanggal=date(2026, 9, 8)))
    assert (t.jenis, t.jumlah) == ("pengembalian_kas_iklan", Decimal("500000"))
    assert await services.saldo_akun(c.s, c.akun["KAS_UTAMA"]) == utama + Decimal("500000")
    assert await _kode(c, svc.pengembalian, c.s, c.admin, PengembalianIklanIn()) == 400  # tidak ada kelebihan lagi
    # Iklan dihentikan: sisa boleh dikembalikan seluruhnya.
    await svc.pengembalian(c.s, c.admin, PengembalianIklanIn(jumlah=Decimal("1500000"), tanggal=date(2026, 9, 9)))
    assert await services.saldo_akun(c.s, iklan) == 0
    # Plafon kas kecil boleh diubah owner; log tercatat.
    await svc.ubah_plafon(c.s, c.owner, c.akun["KAS_KECIL"].id, PlafonIn(plafon=Decimal("4000000")))
    assert len(await svc.log_plafon(c.s, c.owner, c.akun["KAS_KECIL"].id)) == 1
    assert (await services.hitung_pengisian(c.s, "kas_kecil"))["perlu_diisi"] == Decimal("4000000")
