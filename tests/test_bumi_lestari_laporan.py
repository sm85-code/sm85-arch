"""bumi_lestari: dashboard, laporan umum, laporan kas kecil (kas iklan hanya admin)."""
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from test_bumi_lestari_fase1 import _platform
from tenants.bumi_lestari.modules.bumi_lestari.application import laporan_services as lap
from tenants.bumi_lestari.modules.bumi_lestari.application import provisi_services as ps
from tenants.bumi_lestari.modules.bumi_lestari.application import kiriman_services, services, pembayaran_services as pembayaran
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas import TransaksiIn, TransferIn
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_pembayaran import KaryawanIn
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_pembayaran  # noqa: F401
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import (
    BlAkunKas,
    BlKategori,
    BlProporsiBagiHasil,
    BlUser,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.seeder import DEFAULT_AKUN, DEFAULT_KATEGORI


@pytest_asyncio.fixture
async def ctx():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(BumiLestariBase.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        for kode, nama, jenis, plafon in DEFAULT_AKUN:
            s.add(BlAkunKas(kode=kode, nama=nama, jenis=jenis, plafon=plafon))
        for nama, jenis in DEFAULT_KATEGORI:
            s.add(BlKategori(nama=nama, jenis=jenis))
        s.add_all([BlProporsiBagiHasil(penerima="admin", persen=40), BlProporsiBagiHasil(penerima="owner", persen=60)])
        for uid, role in (("a", "admin"), ("o", "owner"), ("s", "staff")):
            s.add(BlUser(id=uid, nama=role, email=f"{role}@t.com", password_hash="x", role=role))
        await s.flush()

        class C:
            pass

        c = C()
        c.s = s
        c.admin, c.owner, c.staf = [await s.get(BlUser, u) for u in ("a", "o", "s")]
        c.akun = {a.kode: a for a in (await s.execute(select(BlAkunKas))).scalars()}
        c.kat = {k.nama: k for k in (await s.execute(select(BlKategori))).scalars()}
        yield c
    await engine.dispose()


async def _trx(c, user, akun, kategori, jenis, jumlah, tanggal, **kw):
    return await services.create_transaksi(
        c.s, user,
        TransaksiIn(akun_id=c.akun[akun].id, kategori_id=c.kat[kategori].id, jenis=jenis, jumlah=Decimal(jumlah), tanggal=tanggal, **kw),
    )


async def _trf(c, user, dari, ke, jumlah, tanggal):
    return await services.create_transfer(
        c.s, user, TransferIn(dari_akun_id=c.akun[dari].id, ke_akun_id=c.akun[ke].id, jumlah=Decimal(jumlah), tanggal=tanggal)
    )


async def _skenario_september(c):
    """September 2026 (1 Sep = Selasa). Kas kecil: isi awal 3jt, dipakai, digenapkan tiap Selasa."""
    await _trx(c, c.admin, "KAS_UTAMA", "Pemasukan lain", "masuk", "10000000", date(2026, 9, 2))
    await _trf(c, c.owner, "KAS_UTAMA", "KAS_KECIL", "3000000", date(2026, 9, 1))
    await _trx(c, c.staf, "KAS_KECIL", "Operasional", "keluar", "300000", date(2026, 9, 3))
    await _trf(c, c.owner, "KAS_UTAMA", "KAS_KECIL", "300000", date(2026, 9, 8))
    await _trx(c, c.staf, "KAS_KECIL", "Operasional", "keluar", "150000", date(2026, 9, 10))
    await _trf(c, c.owner, "KAS_UTAMA", "KAS_KECIL", "150000", date(2026, 9, 15))
    await _trx(c, c.staf, "KAS_KECIL", "Transport", "keluar", "50000", date(2026, 9, 17))
    await _trf(c, c.owner, "KAS_UTAMA", "KAS_KECIL", "50000", date(2026, 9, 22))
    # Kas iklan (admin): isi 2jt, pakai 400rb
    await _trf(c, c.admin, "KAS_UTAMA", "KAS_IKLAN", "2000000", date(2026, 9, 1))
    await _trx(c, c.admin, "KAS_IKLAN", "Biaya iklan", "keluar", "400000", date(2026, 9, 5), platform_iklan_id=(await _platform(c)).id)
    await _trx(c, c.admin, "KAS_UTAMA", "Prive", "keluar", "100000", date(2026, 9, 12))
    # Gaji 2jt dicicil: 2 cicilan (500rb) pada Selasa 1 & 8 Sep
    await pembayaran.create_karyawan(c.s, KaryawanIn(nama="Sari", peran="kas_kecil_packing", gaji_bulanan=Decimal("2000000")))
    await ps.catat_sisihan(c.s, c.owner, date(2026, 9, 1))
    await ps.catat_sisihan(c.s, c.owner, date(2026, 9, 8))
    # Kas kecil & kas iklan masuk laporan setelah "Kirim ke laporan keuangan" (langkah akhir Tutup Kas Mingguan).
    await kiriman_services.kirim_semua(c.s, c.admin)


@pytest.mark.asyncio
async def test_laporan_kas_kecil_weekly_breakdown_and_physical_cash_difference(ctx):
    await _skenario_september(ctx)
    lp = await lap.laporan_imprest(ctx.s, "kas_kecil", "2026-09", saldo_fisik=Decimal("2950000"))
    assert (lp.saldo_awal, lp.saldo_akhir, lp.plafon) == (0, Decimal("3000000"), Decimal("3000000"))
    assert (lp.total_pemakaian, lp.total_pengisian, lp.sesuai_plafon) == (Decimal("500000"), Decimal("3500000"), True)
    assert {b.kategori: b.jumlah for b in lp.per_kategori} == {"Operasional": Decimal("450000"), "Transport": Decimal("50000")}
    assert [(w.minggu_ke, w.dari, w.sampai, w.pemakaian, w.pengisian, w.saldo_akhir) for w in lp.per_minggu] == [
        (1, date(2026, 9, 1), date(2026, 9, 6), 300000, 3000000, 2700000),
        (2, date(2026, 9, 7), date(2026, 9, 13), 150000, 300000, 2850000),
        (3, date(2026, 9, 14), date(2026, 9, 20), 50000, 150000, 2950000),
        (4, date(2026, 9, 21), date(2026, 9, 27), 0, 50000, 3000000),
        (5, date(2026, 9, 28), date(2026, 9, 30), 0, 0, 3000000),
    ]
    assert len(lp.transaksi) == 3 and len(lp.pengisian) == 4
    assert (lp.selisih, lp.status_selisih) == (Decimal("-50000"), "kurang")  # uang fisik kurang 50rb

    pas = await lap.laporan_imprest(ctx.s, "kas_kecil", "2026-09", saldo_fisik=Decimal("3000000"))
    assert (pas.selisih, pas.status_selisih) == (0, "sesuai")
    sep_awal = await lap.laporan_imprest(ctx.s, "kas_kecil", "2026-10")  # bulan berikutnya: saldo awal = saldo akhir sebelumnya
    assert (sep_awal.saldo_awal, sep_awal.total_pemakaian) == (Decimal("3000000"), 0)


@pytest.mark.asyncio
async def test_laporan_umum_profit_and_cash_flow_with_iklan_visible_only_to_admin(ctx):
    await _skenario_september(ctx)
    dari, sampai = date(2026, 9, 1), date(2026, 9, 30)
    lu = await lap.laporan_umum(ctx.s, ctx.owner, dari, sampai)
    assert lu.total_pemasukan == Decimal("10000000")
    biaya = {b.kategori: b.jumlah for b in lu.biaya}
    assert biaya == {
        "Biaya iklan": Decimal("400000"), "Operasional": Decimal("450000"), "Transport": Decimal("50000"),
        "Gaji karyawan (cicilan)": Decimal("1000000"),
    }
    assert lu.total_biaya == Decimal("1900000") and lu.laba_bersih == Decimal("8100000")
    assert [(b.kategori, b.jumlah) for b in lu.di_luar_laba] == [("Prive", Decimal("100000"))]

    kode_owner = {a.kode: a for a in lu.arus_kas}
    assert "KAS_IKLAN" not in kode_owner  # owner tidak melihat akun kas iklan (biayanya tetap masuk laba)
    ku = kode_owner["KAS_UTAMA"]
    assert (ku.saldo_awal, ku.masuk, ku.keluar, ku.transfer_keluar, ku.saldo_akhir) == (
        0, Decimal("10000000"), Decimal("100000"), Decimal("3500000") + Decimal("2000000") + Decimal("1000000"), Decimal("3400000"),
    )
    assert kode_owner["KAS_KECIL"].saldo_akhir == Decimal("3000000")
    assert kode_owner["DANA_CADANGAN"].saldo_akhir == Decimal("1000000")

    la = await lap.laporan_umum(ctx.s, ctx.admin, dari, sampai)
    assert la.laba_bersih == lu.laba_bersih  # laba sama untuk admin
    assert {a.kode: a for a in la.arus_kas}["KAS_IKLAN"].saldo_akhir == Decimal("1600000")
    assert la.total_kas_akhir == lu.total_kas_akhir + Decimal("1600000")

    with pytest.raises(HTTPException) as exc:
        await lap.laporan_umum(ctx.s, ctx.owner, sampai, dari)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_dashboard_owner_vs_admin(ctx):
    await _skenario_september(ctx)
    d_owner = await lap.dashboard(ctx.s, ctx.owner, "2026-09")
    assert d_owner.selasa.weekday() == 1 and d_owner.periode == "2026-09"
    assert (d_owner.pemasukan_bulan_ini, d_owner.biaya_bulan_ini, d_owner.laba_bulan_ini) == (
        Decimal("10000000"), Decimal("1900000"), Decimal("8100000"),
    )
    assert (d_owner.bagian_admin_pratinjau, d_owner.bagian_owner_pratinjau) == (Decimal("3240000.00"), Decimal("4860000.00"))
    assert d_owner.kas_iklan is None and "KAS_IKLAN" not in [a.kode for a in d_owner.akun]
    assert d_owner.total_kas == Decimal("3400000") + Decimal("3000000") + Decimal("1000000")  # kas utama + kecil + dana cadangan
    assert (d_owner.kas_kecil.saldo, d_owner.kas_kecil.perlu_diisi) == (Decimal("3000000"), 0)
    assert d_owner.dana_cadangan == Decimal("1000000")
    assert (d_owner.order_bulan_ini, d_owner.omzet_order_bulan_ini, d_owner.piutang_penjual_lain) == (0, 0, 0)

    d_admin = await lap.dashboard(ctx.s, ctx.admin, "2026-09")
    assert (d_admin.kas_iklan.saldo, d_admin.kas_iklan.perlu_diisi) == (Decimal("1600000"), Decimal("400000"))
    assert d_admin.total_kas == d_owner.total_kas + Decimal("1600000")
