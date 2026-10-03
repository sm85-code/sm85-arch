"""bumi_lestari Fase 1: aturan server (kategori, batal otomatis, kunci order, cutoff penjual lain),
posting berkelompok "Kirim ke laporan keuangan", setoran modal, dan keamanan sesi."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
import json

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

from tenants.bumi_lestari.modules.bumi_lestari.application import kiriman_services as kirim
from tenants.bumi_lestari.modules.bumi_lestari.application import laporan_services as lap
from tenants.bumi_lestari.modules.bumi_lestari.application import order_services as osvc
from tenants.bumi_lestari.modules.bumi_lestari.application import pembayaran_services as pembayaran
from tenants.bumi_lestari.modules.bumi_lestari.application import provisi_services as ps
from tenants.bumi_lestari.modules.bumi_lestari.application import dokumen_services as dok
from tenants.bumi_lestari.modules.bumi_lestari.application import services
from tenants.bumi_lestari.modules.bumi_lestari.application.laba_core import ringkasan_laba
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas import (
    KategoriOut,
    TransaksiIn,
    UserPatchIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_order import (
    OrderIn,
    OrderOut,
    OrderPatch,
    OrderStatusIn,
    PelangganIn,
    PemasokIn,
    ProdukIn,
    SaluranIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_pembayaran import (
    KaryawanIn,
    PembayaranPemasokIn,
    PenerimaanResellerIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_pembayaran  # noqa: F401
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import (
    get_current_user_bumi_lestari,
    issue_bumi_lestari_token,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import (
    BlAkunKas,
    BlAuditLog,
    BlKategori,
    BlProporsiBagiHasil,
    BlTransaksi,
    BlUser,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.seeder import (
    DEFAULT_AKUN,
    DEFAULT_KATEGORI,
    _seed_setoran_modal,
)

SELASA = date(2026, 9, 29)  # Selasa; minggu tagihan = Senin 21 .. Sabtu 26 Sep


@pytest_asyncio.fixture
async def c():
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

        x = C()
        x.s = s
        x.admin, x.owner, x.staf = [await s.get(BlUser, u) for u in ("a", "o", "s")]
        x.akun = {a.kode: a for a in (await s.execute(select(BlAkunKas))).scalars()}
        x.kat = {k.nama: k for k in (await s.execute(select(BlKategori))).scalars()}
        x.partisi = await osvc.create_produk(s, ProdukIn(sku="P1", nama="Partisi", harga_jual=Decimal("850000"), biaya_pokok_default=Decimal("500000")))
        x.tukang = await osvc.create_pemasok(s, PemasokIn(nama="Pak Budi", jenis="tukang_kayu"))
        x.tukang2 = await osvc.create_pemasok(s, PemasokIn(nama="Pak Asep", jenis="tukang_kayu"))
        x.shopee = await osvc.create_saluran(s, SaluranIn(nama="Shopee", jenis="marketplace"))
        x.res = await osvc.create_saluran(s, SaluranIn(nama="Penjual lain", jenis="reseller"))
        x.rina = await osvc.create_pelanggan(s, PelangganIn(nama="Toko Rina"))
        yield x
    await engine.dispose()


async def _trx(c, user, akun, kategori, jenis, jumlah, tanggal=None, **kw):
    return await services.create_transaksi(
        c.s, user,
        TransaksiIn(
            akun_id=c.akun[akun].id, kategori_id=c.kat[kategori].id, jenis=jenis, jumlah=Decimal(jumlah),
            tanggal=tanggal, **kw,
        ),
    )


async def _kode(c, fn, *a, **kw):
    with pytest.raises(HTTPException) as exc:
        await fn(*a, **kw)
    return exc.value.status_code


async def _modal(c, jumlah="50000000"):
    await _trx(c, c.admin, "KAS_UTAMA", "Pemasukan lain", "masuk", jumlah, date(2026, 9, 1))


async def _order_reseller(c, tgl_kirim, *, qty=1, potongan="0"):
    o = await osvc.create_order(
        c.s, OrderIn(saluran_id=c.res.id, pelanggan_id=c.rina.id, produk_id=c.partisi.id, qty=qty,
                     tanggal_order=tgl_kirim - timedelta(days=5),
                     pemasok_id=c.tukang.id, butuh_cat=False, harga_satuan=Decimal("600000"),
                     potongan_marketplace=Decimal(potongan)),
    )
    for st in ("dikerjakan", "diambil"):
        await osvc.ubah_status_order(c.s, o.id, OrderStatusIn(status=st, tanggal=tgl_kirim - timedelta(days=2)))
    if tgl_kirim:
        await osvc.ubah_status_order(c.s, o.id, OrderStatusIn(status="dikirim", tanggal=tgl_kirim))
    return o


async def _order_tukang(c, pemasok, tgl_diambil, no):
    o = await osvc.create_order(
        c.s, OrderIn(no_order=no, saluran_id=c.shopee.id, produk_id=c.partisi.id, pemasok_id=pemasok.id, butuh_cat=False),
    )
    for st in ("dikerjakan", "diambil"):
        await osvc.ubah_status_order(c.s, o.id, OrderStatusIn(status=st, tanggal=tgl_diambil))
    return o


# --- 1.2 kategori ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kategori_sistem_dan_batas_staf_ditolak_server(c):
    await _modal(c)
    await services.catat_pengisian_kas_kecil(c.s, c.owner)
    for nama in ("Penjualan marketplace", "Penjualan reseller", "Biaya produksi / pembelian barang", "Gaji karyawan",
                 "Bagi hasil", "Langganan & utilitas", "Biaya marketplace", "Kerugian retur"):
        jenis = "masuk" if c.kat[nama].jenis == "pemasukan" else "keluar"
        assert await _kode(c, _trx, c, c.admin, "KAS_UTAMA", nama, jenis, "1000") == 422
    # staf: hanya 4 kategori, hanya pengeluaran kas kecil
    assert await _kode(c, _trx, c, c.staf, "KAS_KECIL", "Prive", "keluar", "1000") == 403
    assert await _kode(c, _trx, c, c.staf, "KAS_KECIL", "Biaya iklan", "keluar", "1000") == 403
    for nama in ("Transport", "Packing", "Operasional", "Pengeluaran lain"):
        t = await _trx(c, c.staf, "KAS_KECIL", nama, "keluar", "1000")
        assert t.status_kirim == "draf"
    # Prive & setoran modal khusus admin; biaya iklan hanya dari kas iklan
    assert await _kode(c, _trx, c, c.owner, "KAS_UTAMA", "Prive", "keluar", "1000") == 403
    assert await _kode(c, _trx, c, c.admin, "KAS_UTAMA", "Biaya iklan", "keluar", "1000") == 422
    assert (await _trx(c, c.admin, "KAS_UTAMA", "Prive", "keluar", "1000")).status_kirim == "terkirim"
    # flag di GET /kategori
    out = {k.nama: KategoriOut.model_validate(k) for k in await services.list_kategori(c.s)}
    assert out["Penjualan marketplace"].sistem and not out["Penjualan marketplace"].untuk_staf
    assert out["Transport"].untuk_staf and out["Transport"].masuk_laba
    assert not out["Setoran modal"].masuk_laba and out["Setoran modal"].khusus_admin
    assert [k for k, v in out.items() if v.untuk_staf] == ["Operasional", "Packing", "Pengeluaran lain", "Transport"]


# --- 1.9 setoran modal -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setoran_modal_sekali_dan_tidak_masuk_laba(c):
    await _seed_setoran_modal(c.s, c.owner)
    await _seed_setoran_modal(c.s, c.owner)  # idempoten
    rows = (await c.s.execute(select(BlTransaksi).where(BlTransaksi.kategori_id == c.kat["Setoran modal"].id))).scalars().all()
    assert len(rows) == 1 and rows[0].tanggal == date(2026, 9, 1) and rows[0].jumlah == Decimal("20000000")
    assert await services.saldo_akun(c.s, c.akun["KAS_UTAMA"]) == Decimal("20000000")
    assert await _kode(c, _trx, c, c.admin, "KAS_UTAMA", "Setoran modal", "masuk", "5000000") == 409
    assert await _kode(c, _trx, c, c.owner, "KAS_UTAMA", "Setoran modal", "masuk", "5000000", konfirmasi_setoran_modal_kedua=True) == 403
    await _trx(c, c.admin, "KAS_UTAMA", "Setoran modal", "masuk", "5000000", date(2026, 9, 2), konfirmasi_setoran_modal_kedua=True)
    laba = await ringkasan_laba(c.s, date(2026, 9, 1), date(2026, 9, 30))
    assert laba.total_pemasukan == 0 and laba.laba == 0
    aksi = (await c.s.execute(select(BlAuditLog.aksi))).scalars().all()
    assert aksi.count("setoran_modal") == 1


# --- 1.1 batal otomatis & sisihan --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batal_transaksi_otomatis_dan_sisihan_ditolak_dari_endpoint_umum(c):
    await _modal(c)
    o = await _order_reseller(c, date(2026, 9, 22))
    pen = await pembayaran.buat_penerimaan_reseller(c.s, c.admin, PenerimaanResellerIn(pelanggan_id=c.rina.id, order_ids=[o.id]))
    trx = (await c.s.execute(select(BlTransaksi).where(BlTransaksi.ref_id == pen.id))).scalar_one()
    assert await _kode(c, services.batalkan_transaksi, c.s, trx.id, "salah", c.admin) == 409

    await pembayaran.create_karyawan(c.s, KaryawanIn(nama="Sari", gaji_bulanan=Decimal("2000000")))
    sis = await ps.catat_sisihan(c.s, c.admin, date(2026, 9, 1))
    assert await _kode(c, services.batalkan_transfer, c.s, sis.transfer_id, "salah", c.admin) == 409
    await ps.batalkan_sisihan(c.s, sis.id, "salah")  # dari halaman asal: boleh

    # pengisian kas kecil/iklan dengan tanggal pilihan
    t = await services.catat_pengisian_kas_kecil(c.s, c.owner, date(2026, 9, 29))
    assert t.tanggal == date(2026, 9, 29)
    t2 = await services.catat_pengisian(c.s, c.admin, "kas_iklan", date(2026, 9, 28))
    assert t2.tanggal == date(2026, 9, 28)


# --- 1.13 kirim ke laporan keuangan -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kirim_kas_kecil_kunci_batal_kiriman_dan_kirim_ulang(c):
    await _modal(c)
    await services.catat_pengisian_kas_kecil(c.s, c.owner, date(2026, 9, 1))
    kk = c.akun["KAS_KECIL"]
    t1 = await _trx(c, c.staf, "KAS_KECIL", "Transport", "keluar", "50000", date(2026, 9, 3))
    t2 = await _trx(c, c.staf, "KAS_KECIL", "Packing", "keluar", "70000", date(2026, 9, 10))

    # draf: tidak memengaruhi laba, saldo resmi, maupun buku besar; uang fisik sudah berkurang
    assert (await ringkasan_laba(c.s, date(2026, 9, 1), date(2026, 9, 30))).total_biaya == 0
    assert await services.saldo_akun(c.s, kk) == Decimal("3000000")
    assert await services.saldo_akun(c.s, kk, termasuk_draf=True) == Decimal("2880000")
    assert [t.id for t in await services.list_transaksi(c.s, c.owner, akun_id=kk.id)] == []
    (d,) = await kirim.ringkasan_draf(c.s, c.owner, "kas_kecil")
    assert (d.jumlah_entri, d.total_keluar, d.tanggal_tertua) == (2, Decimal("120000"), date(2026, 9, 3))
    dash = await lap.dashboard(c.s, c.owner, "2026-09")
    assert [(x.sumber, x.jumlah_entri) for x in dash.draf_belum_dikirim] == [("kas_kecil", 2)]

    # kirim sebagian (sampai tanggal), lalu sisanya
    k1 = await kirim.kirim(c.s, c.owner, "kas_kecil", date(2026, 9, 5))
    assert (k1.jumlah_entri, k1.total) == (1, Decimal("50000")) and k1.nomor.startswith("KRM-")
    assert t1.status_kirim == "terkirim" and t1.kiriman_id == k1.id and t1.tanggal == date(2026, 9, 3)
    assert t2.status_kirim == "draf"
    k2 = await kirim.kirim(c.s, c.owner, "kas_kecil")
    assert k2.nomor != k1.nomor
    assert await _kode(c, kirim.kirim, c.s, c.owner, "kas_kecil") == 400  # tidak ada draf lagi
    laba = await ringkasan_laba(c.s, date(2026, 9, 1), date(2026, 9, 30))
    assert laba.total_biaya == Decimal("120000")
    assert await services.saldo_akun(c.s, kk) == Decimal("2880000")

    # terkunci: tidak bisa dibatalkan satu per satu
    assert await _kode(c, services.batalkan_transaksi, c.s, t2.id, "salah", c.admin) == 409
    # batal kiriman -> kembali draf, kirim ulang = kiriman baru
    await kirim.batal_kiriman(c.s, c.admin, k2.id, "nominal salah")
    assert t2.status_kirim == "draf" and t2.kiriman_id is None
    assert await _kode(c, kirim.batal_kiriman, c.s, c.admin, k2.id, "lagi") == 409
    await services.batalkan_transaksi(c.s, t2.id, "nominal salah", c.admin)  # draf boleh dibatalkan
    await _trx(c, c.staf, "KAS_KECIL", "Packing", "keluar", "75000", date(2026, 9, 10))
    k3 = await kirim.kirim(c.s, c.owner, "kas_kecil")
    riwayat = await kirim.list_kiriman(c.s, c.owner, "kas_kecil")
    assert {k.id: k.status for k in riwayat} == {k1.id: "terkirim", k2.id: "dibatalkan", k3.id: "terkirim"}
    _, items = await kirim.detail_kiriman(c.s, c.owner, k3.id)
    assert [i.jumlah for i in items] == [Decimal("75000")]
    aksi = (await c.s.execute(select(BlAuditLog.aksi))).scalars().all()
    assert aksi.count("kirim") == 3 and aksi.count("batal_kiriman") == 1


@pytest.mark.asyncio
async def test_kirim_semua_sumber_order_dan_owner_tanpa_kas_iklan(c):
    await _modal(c)
    await services.catat_pengisian(c.s, c.admin, "kas_iklan", date(2026, 9, 1))
    await _trx(c, c.admin, "KAS_IKLAN", "Biaya iklan", "keluar", "300000", date(2026, 9, 2))
    o = await _order_reseller(c, date(2026, 9, 22))
    pen = await pembayaran.buat_penerimaan_reseller(c.s, c.admin, PenerimaanResellerIn(pelanggan_id=c.rina.id, order_ids=[o.id], tanggal=date(2026, 9, 29)))
    assert pen.status_kirim == "draf"
    await _order_tukang(c, c.tukang, date(2026, 9, 22), "SHP-1")
    bayar = await pembayaran.buat_pembayaran_pemasok(c.s, c.admin, PembayaranPemasokIn(tanggal=SELASA))
    assert bayar.status_kirim == "draf"

    assert [d.sumber for d in await kirim.ringkasan_draf(c.s, c.owner)] == ["kas_kecil", "penerimaan_reseller", "pembayaran_pemasok", "pencairan"]
    assert await _kode(c, kirim.ringkasan_draf, c.s, c.owner, "kas_iklan") == 403
    hasil = await kirim.kirim_semua(c.s, c.admin, tutup_kas_mingguan_id="tkm-2026-09-29")
    assert sorted(k.sumber for k in hasil) == ["kas_iklan", "pembayaran_pemasok", "penerimaan_reseller"]
    assert all(k.tutup_kas_mingguan_id == "tkm-2026-09-29" for k in hasil)
    assert pen.status_kirim == bayar.status_kirim == "terkirim"
    assert await _kode(c, pembayaran.batalkan_penerimaan_reseller, c.s, pen.id, "salah") == 409
    laba = await ringkasan_laba(c.s, date(2026, 9, 1), date(2026, 9, 30))
    assert {b.kategori: b.jumlah for b in laba.biaya} == {
        "Biaya iklan": Decimal("300000"),
        "Biaya produksi / pembelian barang": Decimal("1000000"),  # order reseller + order Shopee, 500rb per order
    }
    assert laba.total_pemasukan == Decimal("50000000") + Decimal("610000")
    assert await _kode(c, kirim.kirim_semua, c.s, c.admin) == 400


# --- 1.4 tagihan penjual lain ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_piutang_dan_invoice_satu_rumus_cutoff_tanggal_kirim(c):
    a = await _order_reseller(c, date(2026, 9, 21), potongan="99000")  # Senin minggu lalu
    b = await _order_reseller(c, date(2026, 9, 26), qty=2)  # Sabtu
    telat = await _order_reseller(c, date(2026, 9, 15))  # minggu sebelumnya, belum dibayar
    minggu_ini = await _order_reseller(c, date(2026, 9, 27))  # Minggu -> invoice berikutnya

    (inv,) = await dok.invoice_reseller(c.s, SELASA)
    ids = [i.order_id for i in inv.items]
    assert set(ids) == {a.id, b.id, telat.id} and minggu_ini.id not in ids
    assert {i.order_id: i.terlambat for i in inv.items}[telat.id] is True
    tagihan = {o.id: pembayaran.tagihan_order(o) for o in (a, b, telat)}
    assert tagihan[a.id] == Decimal("610000")  # potongan marketplace tidak mengurangi tagihan penjual lain
    assert inv.grand_total == sum(tagihan.values())
    (piutang,) = await pembayaran.list_piutang_reseller(c.s)
    assert {i.order_id for i in piutang.items} == {a.id, b.id, telat.id, minggu_ini.id}
    assert {i.order_id: i.jumlah for i in piutang.items if i.order_id in tagihan} == tagihan


# --- 1.3 kunci order, 1.12 nomor pesanan -------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_terkunci_setelah_dibayar(c):
    await _modal(c)
    o = await _order_reseller(c, date(2026, 9, 22))
    pen = await pembayaran.buat_penerimaan_reseller(c.s, c.admin, PenerimaanResellerIn(pelanggan_id=c.rina.id))
    o = await osvc.get_order(c.s, o.id)
    out = OrderOut.model_validate(o)
    assert out.dibayar_penjual_lain and out.terkunci and not out.dibayar_tukang
    assert await _kode(c, osvc.update_order, c.s, o.id, OrderPatch(harga_satuan=Decimal("1"))) == 409
    assert await _kode(c, osvc.ubah_status_order, c.s, o.id, OrderStatusIn(status="batal")) == 409
    await osvc.update_order(c.s, o.id, OrderPatch(catatan="bungkus rapi", harga_satuan=Decimal("600000")))  # tidak berubah: boleh
    await osvc.ubah_status_order(c.s, o.id, OrderStatusIn(status="selesai"))  # maju status: boleh
    lst = [OrderOut.model_validate(x) for x in await osvc.list_order(c.s)]
    assert lst[0].terkunci
    await pembayaran.batalkan_penerimaan_reseller(c.s, pen.id, "keluarkan dari draf")
    assert not OrderOut.model_validate(await osvc.get_order(c.s, o.id)).terkunci


@pytest.mark.asyncio
async def test_nomor_pesanan_marketplace_wajib_dan_unik(c):
    base = dict(saluran_id=c.shopee.id, produk_id=c.partisi.id, tanggal_order=date(2026, 9, 1))
    assert await _kode(c, osvc.create_order, c.s, OrderIn(**base)) == 422
    await osvc.create_order(c.s, OrderIn(no_order="260901AB", **base))
    await osvc.create_order(c.s, OrderIn(no_order="260901AB", **base))  # barang kedua dari pesanan yang sama
    lain_hari = {**base, "tanggal_order": date(2026, 9, 2)}
    assert await _kode(c, osvc.create_order, c.s, OrderIn(no_order="260901AB", **lain_hari)) == 409
    o = await osvc.create_order(c.s, OrderIn(no_order="260902CD", **lain_hari))
    assert await _kode(c, osvc.update_order, c.s, o.id, OrderPatch(no_order="260901AB")) == 409
    # saluran non-marketplace boleh tanpa nomor
    await osvc.create_order(c.s, OrderIn(saluran_id=c.res.id, pelanggan_id=c.rina.id, produk_id=c.partisi.id))


# --- 1.6 beberapa pembayaran tukang per minggu ---------------------------------------------------------


@pytest.mark.asyncio
async def test_beberapa_pembayaran_tukang_per_minggu(c):
    await _modal(c)
    a = await _order_tukang(c, c.tukang, date(2026, 9, 22), "SHP-A")
    b = await _order_tukang(c, c.tukang2, date(2026, 9, 23), "SHP-B")
    p1 = await pembayaran.buat_pembayaran_pemasok(c.s, c.admin, PembayaranPemasokIn(tanggal=SELASA, pemasok_id=c.tukang.id))
    siap = await pembayaran.siap_bayar_pemasok(c.s, SELASA)
    assert {i.order_id for g in siap.pemasok for i in g.items} == {b.id} and siap.sudah_dicatat_id == p1.id
    p2 = await pembayaran.buat_pembayaran_pemasok(c.s, c.admin, PembayaranPemasokIn(tanggal=SELASA))
    siap = await pembayaran.siap_bayar_pemasok(c.s, SELASA)
    assert siap.pembayaran_ids == [p1.id, p2.id] and siap.total == 0
    assert OrderOut.model_validate(await osvc.get_order(c.s, a.id)).dibayar_tukang


# --- 1.7 dashboard, 1.8 bagi hasil ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_bulanan_dan_bagi_hasil_hanya_bulan_berakhir(c):
    await osvc.create_order(c.s, OrderIn(no_order="X1", saluran_id=c.shopee.id, produk_id=c.partisi.id, tanggal_order=date(2026, 8, 30)))
    await osvc.create_order(c.s, OrderIn(no_order="X2", saluran_id=c.shopee.id, produk_id=c.partisi.id, tanggal_order=date(2026, 9, 2)))
    await _order_reseller(c, date(2026, 9, 22))
    dash = await lap.dashboard(c.s, c.admin, "2026-09")
    assert dash.order_per_status == {"dipesan": 1, "dikirim": 1}
    assert dash.order_aktif_per_status == {"dipesan": 2, "dikirim": 1}

    hari_ini = services._hari_ini()
    bulan_ini = f"{hari_ini.year}-{hari_ini.month:02d}"
    assert await _kode(c, pembayaran.simpan_bagi_hasil, c.s, c.admin, bulan_ini) == 409
    assert await _kode(c, pembayaran.simpan_bagi_hasil, c.s, c.admin, "2020-01") == 409  # belum tutup buku


# --- keamanan ------------------------------------------------------------------------------------------


def _req(token: str) -> Request:
    return Request({"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]})


@pytest.mark.asyncio
async def test_token_dicabut_saat_versi_sesi_naik(c, monkeypatch):
    monkeypatch.setenv("JWT_SECRET_BUMI_LESTARI", "test-secret-bumi-lestari")
    token = issue_bumi_lestari_token(c.staf)
    assert (await get_current_user_bumi_lestari(_req(token), c.s)).id == "s"
    await services.update_user(c.s, c.admin, "s", UserPatchIn(nama="Staf baru"))  # tidak mencabut sesi
    assert (await get_current_user_bumi_lestari(_req(token), c.s)).id == "s"
    await services.update_user(c.s, c.admin, "s", UserPatchIn(role="owner"))
    assert await _kode(c, get_current_user_bumi_lestari, _req(token), c.s) == 401
    baru = issue_bumi_lestari_token(c.staf)
    assert (await get_current_user_bumi_lestari(_req(baru), c.s)).id == "s"
    services.naikkan_versi_sesi(c.staf)  # logout semua perangkat
    assert await _kode(c, get_current_user_bumi_lestari, _req(baru), c.s) == 401


def test_seed_hanya_post():
    from tenants.bumi_lestari.adapters.api.v1.bumi_lestari_router import bumi_lestari_router

    routes = [r for r in bumi_lestari_router.routes if getattr(r, "path", None) == "/seed-now"]
    assert routes and all(r.methods == {"POST"} for r in routes)


@pytest.mark.asyncio
async def test_error_500_bumi_lestari_tanpa_detail_exception():
    from main import CsrfOriginMiddleware

    mw = CsrfOriginMiddleware(app=None)

    async def meledak(_request):
        raise RuntimeError("password=rahasia")

    async def panggil(path):
        scope = {"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b""}
        r = await mw.dispatch(Request(scope), meledak)
        return r.status_code, json.loads(r.body)

    kode, isi = await panggil("/api/bumi-lestari/transaksi")
    assert kode == 500 and isi == {"detail": "Terjadi kesalahan internal pada server"}
    kode, isi = await panggil("/api/lain/x")
    assert kode == 500 and "error_message" in isi
