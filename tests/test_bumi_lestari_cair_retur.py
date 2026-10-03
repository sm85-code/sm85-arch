"""bumi_lestari Fase 2.6/2.7: akun saldo per saluran, status cair & retur order, belum cair per saluran
(spesifikasi 8.3.5, 8.4: AB-BC-1..3)."""
# ruff: noqa: F811  -- parameter `c` adalah fixture yang diimpor dari test_bumi_lestari_fase1
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from test_bumi_lestari_fase1 import SELASA, _kode, _modal, _order_reseller, _order_tukang, c  # noqa: F401
from tenants.bumi_lestari.modules.bumi_lestari.application import kiriman_services as kirim
from tenants.bumi_lestari.modules.bumi_lestari.application import laporan_services as lap
from tenants.bumi_lestari.modules.bumi_lestari.application import order_services as osvc
from tenants.bumi_lestari.modules.bumi_lestari.application import pembayaran_services as pembayaran
from tenants.bumi_lestari.modules.bumi_lestari.application.laba_core import ringkasan_laba
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_order import (
    OrderOut,
    OrderPatch,
    OrderReturIn,
    OrderStatusIn,
    SaluranIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_pembayaran import PembayaranPemasokIn
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlAuditLog, BlTutupBuku
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.seeder import DEFAULT_AKUN, DEFAULT_SALURAN

SEP = (date(2026, 9, 1), date(2026, 9, 30))
OKT = (date(2026, 10, 1), date(2026, 10, 31))


async def _dikirim(c, no, tgl_kirim, *, saluran=None, potongan=None):
    o = await _order_tukang(c, c.tukang, date(2026, 9, 22), no)
    if saluran is not None or potongan is not None:
        o.saluran_id = (saluran or c.shopee).id
        if potongan is not None:
            o.potongan_marketplace = Decimal(potongan)
    await osvc.ubah_status_order(c.s, o.id, OrderStatusIn(status="dikirim", tanggal=tgl_kirim))
    return o


def _biaya(r, kategori):
    return next((b.jumlah for b in r.biaya if b.kategori == kategori), Decimal("0"))


def test_saluran_bawaan_punya_akun_saldo():
    kode_akun = {k for k, _n, jenis, _p in DEFAULT_AKUN if jenis == "ewallet"}
    assert {s[2] for s in DEFAULT_SALURAN} == kode_akun
    assert {s[0] for s in DEFAULT_SALURAN} == {"Shopee", "TikTok Shop", "Lazada", "Blibli", "Toko web"}
    assert dict((s[0], s[1]) for s in DEFAULT_SALURAN)["Toko web"] == "web"


@pytest.mark.asyncio
async def test_seed_menghubungkan_saluran_ke_akun_saldo(monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import database as bl_database
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import seeder
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlAkunKas
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import BlSaluran

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(bl_database, "engine", engine)
    monkeypatch.setenv("BUMI_LESTARI_SEED_OWNER_PASSWORD", "rahasia-123")
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        await seeder._create_schema(engine)
        s.add(BlSaluran(nama="Toko web", jenis="web"))  # saluran lama tanpa akun
        await s.commit()
        await seeder.seed_bumi_lestari(s)
        await seeder.seed_bumi_lestari(s)  # idempoten
        akun = {a.id: a.kode for a in (await s.execute(select(BlAkunKas))).scalars()}
        from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_pencairan import BlFormatPenghasilan

        formats = (await s.execute(select(BlFormatPenghasilan))).scalars().all()
        saluran = {x.nama: akun.get(x.akun_id) for x in (await s.execute(select(BlSaluran))).scalars()}
    await engine.dispose()
    assert saluran == {
        "Shopee": "SALDO_SHOPEE", "TikTok Shop": "SALDO_TIKTOK", "Lazada": "SALDO_LAZADA", "Blibli": "SALDO_BLIBLI",
        "Toko web": "SALDO_IPAYMU",
    }
    # Format Shopee sementara (2.5a): draf v1, ditandai SEMENTARA, sekali saja.
    assert [(f.versi, f.status) for f in formats] == [(1, "draf")] and "SEMENTARA" in formats[0].nama


@pytest.mark.asyncio
async def test_belum_cair_per_saluran_dan_per_tanggal(c):
    web = await osvc.create_saluran(c.s, SaluranIn(nama="Toko web", jenis="web"))
    a = await _dikirim(c, "SHP-1", date(2026, 9, 24), potongan="50000")
    b = await _dikirim(c, "WEB-1", date(2026, 9, 25), saluran=web)
    await _order_reseller(c, date(2026, 9, 24))  # penjual lain: piutang reseller, bukan belum cair
    await _order_tukang(c, c.tukang2, date(2026, 9, 22), "SHP-2")  # belum dikirim

    bc = await lap.belum_cair(c.s, date(2026, 9, 30))
    assert bc.jumlah_order == 2 and bc.tgl_kirim_tertua == date(2026, 9, 24)
    shp = next(g for g in bc.per_saluran if g.saluran_id == c.shopee.id)
    assert shp.total_penjualan == Decimal("850000") and shp.total_perkiraan_cair == Decimal("800000")
    assert [o.order_id for o in shp.order] == [a.id]
    assert bc.total_perkiraan_cair == Decimal("1650000")
    assert (await lap.belum_cair(c.s, date(2026, 9, 24))).jumlah_order == 1

    # Cair 1 Okt (diisi oleh pencairan): akhir September masih belum cair, sesudahnya tidak.
    a.status_cair, a.tgl_cair, a.potongan_aktual = "cair", date(2026, 10, 1), Decimal("60000")
    await c.s.flush()
    assert (await lap.belum_cair(c.s, date(2026, 9, 30))).jumlah_order == 2
    sesudah = await lap.belum_cair(c.s, date(2026, 10, 2))
    assert [o.order_id for g in sesudah.per_saluran for o in g.order] == [b.id]
    assert [o.id for o in await osvc.list_order(c.s, status_cair="cair")] == [a.id]

    lu = await lap.laporan_umum(c.s, c.admin, *SEP)  # potongan aktual (60rb) dipakai begitu diketahui
    assert lu.belum_cair == Decimal("1640000") and lu.perkiraan_laba_jika_cair == lu.laba_bersih + Decimal("1640000")


@pytest.mark.asyncio
async def test_retur_sebelum_cair(c):
    o = await _order_tukang(c, c.tukang, date(2026, 9, 22), "SHP-R")
    assert await _kode(c, osvc.retur_order, c.s, o.id, OrderReturIn(alasan="rusak")) == 409  # belum dikirim
    await osvc.ubah_status_order(c.s, o.id, OrderStatusIn(status="dikirim", tanggal=date(2026, 9, 24)))
    assert await _kode(c, osvc.retur_order, c.s, o.id, OrderReturIn(alasan="rusak", tanggal=date(2026, 9, 23))) == 400

    r = await osvc.retur_order(c.s, o.id, OrderReturIn(alasan="barang rusak", tanggal=date(2026, 9, 28), kembali_stok=True), c.admin.id)
    out = OrderOut.model_validate(r)
    assert (out.status, out.tgl_retur, out.kembali_stok) == ("retur", date(2026, 9, 28), True)
    assert (await lap.belum_cair(c.s, date(2026, 9, 27))).jumlah_order == 1  # sebelum tanggal retur masih belum cair
    assert (await lap.belum_cair(c.s, date(2026, 9, 30))).jumlah_order == 0
    assert await _kode(c, osvc.retur_order, c.s, o.id, OrderReturIn(alasan="lagi")) == 409
    assert await _kode(c, osvc.ubah_status_order, c.s, o.id, OrderStatusIn(status="selesai")) == 409
    assert await _kode(c, osvc.update_order, c.s, o.id, OrderPatch(catatan="x")) == 409
    log = (await c.s.execute(select(BlAuditLog).where(BlAuditLog.entitas == "order"))).scalar_one()
    assert (log.aksi, log.alasan, log.user_id) == ("retur", "barang rusak", c.admin.id)

    res = await _order_reseller(c, date(2026, 9, 24))
    assert await _kode(c, osvc.retur_order, c.s, res.id, OrderReturIn(alasan="rusak")) == 400  # bukan marketplace/web
    cair = await _dikirim(c, "SHP-C", date(2026, 9, 24))
    cair.status_cair = "cair"
    assert await _kode(c, osvc.retur_order, c.s, cair.id, OrderReturIn(alasan="rusak")) == 409  # lewat file pencairan


@pytest.mark.asyncio
async def test_retur_bulan_tertutup_ditolak(c):
    o = await _dikirim(c, "SHP-T", date(2026, 9, 24))
    c.s.add(BlTutupBuku(periode="2026-09", ditutup_oleh=c.admin.id, snapshot={}))
    await c.s.flush()
    assert await _kode(c, osvc.retur_order, c.s, o.id, OrderReturIn(alasan="rusak", tanggal=date(2026, 9, 28))) == 409
    r = await osvc.retur_order(c.s, o.id, OrderReturIn(alasan="rusak", tanggal=date(2026, 10, 2)))
    assert r.status == "retur"


@pytest.mark.asyncio
async def test_biaya_tukang_order_retur_pindah_ke_kerugian_retur(c):
    await _modal(c)
    a = await _dikirim(c, "SHP-A", date(2026, 9, 24))
    b = await _dikirim(c, "SHP-B", date(2026, 9, 24))
    await pembayaran.buat_pembayaran_pemasok(c.s, c.admin, PembayaranPemasokIn(tanggal=SELASA))
    await kirim.kirim(c.s, c.admin, "pembayaran_pemasok")
    sep = await ringkasan_laba(c.s, *SEP)
    assert _biaya(sep, "Biaya produksi / pembelian barang") == Decimal("1000000")

    # Retur di bulan yang sama dengan pembayaran: pindah di bulan itu.
    await osvc.retur_order(c.s, a.id, OrderReturIn(alasan="rusak", tanggal=date(2026, 9, 30)))
    sep2 = await ringkasan_laba(c.s, *SEP)
    assert _biaya(sep2, "Biaya produksi / pembelian barang") == Decimal("500000")
    assert _biaya(sep2, "Kerugian retur") == Decimal("500000")
    assert sep2.laba == sep.laba  # hanya kelompok biaya yang berpindah

    # Retur sesudah bulan pembayaran: dipindah di bulan retur (bulan lalu tidak berubah).
    await osvc.retur_order(c.s, b.id, OrderReturIn(alasan="salah kirim", tanggal=date(2026, 10, 2)))
    assert _biaya(await ringkasan_laba(c.s, *SEP), "Kerugian retur") == Decimal("500000")
    okt = await ringkasan_laba(c.s, *OKT)
    assert _biaya(okt, "Biaya produksi / pembelian barang") == Decimal("-500000")
    assert _biaya(okt, "Kerugian retur") == Decimal("500000")
    assert okt.laba == Decimal("0")
