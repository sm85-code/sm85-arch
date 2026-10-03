"""bumi_lestari T3/T4: pembayaran Selasa, penerimaan reseller, gaji, bagi hasil, kelola pengguna."""
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tenants.bumi_lestari.modules.bumi_lestari.application import order_services as osvc
from tenants.bumi_lestari.modules.bumi_lestari.application import services, t3_services as t3
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas import (
    ResetPasswordIn,
    TransaksiIn,
    TransferIn,
    UserCreateIn,
    UserPatchIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_order import (
    HargaGrosirIn,
    OrderIn,
    OrderStatusIn,
    PelangganIn,
    PemasokIn,
    ProdukIn,
    SaluranIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_t3 import (
    KaryawanIn,
    KaryawanPatch,
    PembayaranPemasokIn,
    PenerimaanResellerIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_t3  # noqa: F401
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import (
    BlAkunKas,
    BlKategori,
    BlProporsiBagiHasil,
    BlTransaksi,
    BlUser,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.seeder import DEFAULT_AKUN, DEFAULT_KATEGORI

# 2025-06-10 adalah hari Selasa. Minggu sebelumnya: Senin 2-Jun .. Sabtu 7-Jun.
SELASA = date(2025, 6, 10)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(BumiLestariBase.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        for kode, nama, jenis, plafon in DEFAULT_AKUN:
            s.add(BlAkunKas(kode=kode, nama=nama, jenis=jenis, plafon=plafon))
        for nama, jenis in DEFAULT_KATEGORI:
            s.add(BlKategori(nama=nama, jenis=jenis))
        s.add(BlProporsiBagiHasil(penerima="admin", persen=40))
        s.add(BlProporsiBagiHasil(penerima="owner", persen=60))
        await s.flush()
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def ctx(session):
    class C:
        pass

    c = C()
    c.admin = BlUser(id="admin-1", nama="A", email="a@t.com", password_hash="x", role="admin")
    session.add(c.admin)
    await session.flush()
    c.kas = (await session.execute(select(BlAkunKas).where(BlAkunKas.kode == "KAS_UTAMA"))).scalar_one()
    c.partisi = await osvc.create_produk(
        session, ProdukIn(sku="P1", nama="Partisi", harga_jual=Decimal("850000"), biaya_pokok_default=Decimal("500000"))
    )
    c.lampu = await osvc.create_produk(
        session, ProdukIn(sku="L1", nama="Lampu", jenis_produk="non_kayu", harga_jual=Decimal("100000"), biaya_pokok_default=Decimal("60000"))
    )
    c.tukang = await osvc.create_pemasok(session, PemasokIn(nama="Pak Budi", jenis="tukang_kayu"))
    c.supplier = await osvc.create_pemasok(session, PemasokIn(nama="Toko Lampu", jenis="supplier"))
    c.shopee = await osvc.create_saluran(session, SaluranIn(nama="Shopee", jenis="marketplace"))
    c.res_saluran = await osvc.create_saluran(session, SaluranIn(nama="Reseller", jenis="reseller"))
    c.rina = await osvc.create_pelanggan(session, PelangganIn(nama="Toko Rina"))
    return c


async def _kas_masuk(session, ctx, jumlah, kategori="Penjualan marketplace", tanggal=None):
    kat = (await session.execute(select(BlKategori).where(BlKategori.nama == kategori))).scalar_one()
    return await services.create_transaksi(
        session, ctx.admin,
        TransaksiIn(akun_id=ctx.kas.id, kategori_id=kat.id, jenis="masuk", jumlah=Decimal(jumlah), tanggal=tanggal),
    )


async def _order_diambil(session, produk, pemasok, tgl, *, qty=1, biaya=None, saluran=None, no=""):
    o = await osvc.create_order(
        session,
        OrderIn(saluran_id=(saluran or await _shopee(session)).id, produk_id=produk.id, qty=qty, pemasok_id=pemasok.id, butuh_cat=False, no_order=no, biaya_pokok=biaya),
    )
    langkah = ("dikerjakan", "diambil") if produk.jenis_produk == "kayu" else ("diterima",)
    for s in langkah:
        await osvc.ubah_status_order(session, o.id, OrderStatusIn(status=s, tanggal=tgl))
    return o


async def _shopee(session):
    from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import BlSaluran

    return (await session.execute(select(BlSaluran).where(BlSaluran.nama == "Shopee"))).scalar_one()


@pytest.mark.asyncio
async def test_selasa_acuan_and_window():
    assert t3.selasa_acuan(date(2025, 6, 10)) == SELASA  # Selasa
    assert t3.selasa_acuan(date(2025, 6, 12)) == SELASA  # Kamis -> Selasa sebelumnya
    assert t3.selasa_acuan(date(2025, 6, 16)) == date(2025, 6, 10)  # Senin -> Selasa sebelumnya


@pytest.mark.asyncio
async def test_pembayaran_pemasok_is_one_transaction_with_item_detail(session, ctx):
    await _kas_masuk(session, ctx, "5000000")
    a = await _order_diambil(session, ctx.partisi, ctx.tukang, date(2025, 6, 3), qty=2, no="A")  # Selasa lalu: ikut
    b = await _order_diambil(session, ctx.partisi, ctx.tukang, date(2025, 6, 7), no="B")  # Sabtu: ikut
    c = await _order_diambil(session, ctx.lampu, ctx.supplier, date(2025, 6, 5), qty=3, no="C")  # supplier: ikut
    d = await _order_diambil(session, ctx.partisi, ctx.tukang, date(2025, 6, 8), no="D")  # Minggu: Selasa depan
    e = await _order_diambil(session, ctx.partisi, ctx.tukang, date(2025, 6, 9), no="E")  # Senin: Selasa depan

    siap = await t3.siap_bayar_pemasok(session, SELASA)
    assert siap.batas_diambil == date(2025, 6, 7)
    ids = {i.order_id for g in siap.pemasok for i in g.items}
    assert ids == {a.id, b.id, c.id} and d.id not in ids and e.id not in ids
    assert siap.total == Decimal("1000000") + Decimal("500000") + Decimal("180000")

    p = await t3.buat_pembayaran_pemasok(session, ctx.admin, PembayaranPemasokIn(tanggal=SELASA))
    trxs = (await session.execute(select(BlTransaksi).where(BlTransaksi.ref_id == p.id))).scalars().all()
    assert len(trxs) == 1 and trxs[0].jumlah == Decimal("1680000") and trxs[0].jenis == "keluar"  # angkanya 1
    detail = await t3.detail_pembayaran_pemasok(session, p.id)
    assert detail.total_qty == 6 and len(detail.items) == 3  # isinya: daftar barang
    assert detail.transaksi_id == trxs[0].id
    assert await services.saldo_akun(session, ctx.kas) == Decimal("5000000") - Decimal("1680000")

    with pytest.raises(HTTPException) as exc:  # 1 kali tiap Selasa
        await t3.buat_pembayaran_pemasok(session, ctx.admin, PembayaranPemasokIn(tanggal=SELASA))
    assert exc.value.status_code == 409

    depan = await t3.siap_bayar_pemasok(session, date(2025, 6, 17))
    assert {i.order_id for g in depan.pemasok for i in g.items} == {d.id, e.id}

    await t3.batalkan_pembayaran_pemasok(session, p.id, "salah")
    assert await services.saldo_akun(session, ctx.kas) == Decimal("5000000")
    assert (await t3.siap_bayar_pemasok(session, SELASA)).total == Decimal("1680000")  # bisa diulang


@pytest.mark.asyncio
async def test_pembayaran_needs_balance_and_late_orders_are_flagged(session, ctx):
    lama = await _order_diambil(session, ctx.partisi, ctx.tukang, date(2025, 5, 20), no="LAMA")
    siap = await t3.siap_bayar_pemasok(session, SELASA)
    assert siap.pemasok[0].items[0].terlambat is True
    with pytest.raises(HTTPException) as exc:  # kas kosong
        await t3.buat_pembayaran_pemasok(session, ctx.admin, PembayaranPemasokIn(tanggal=SELASA))
    assert exc.value.status_code == 400
    await _kas_masuk(session, ctx, "600000")
    with pytest.raises(HTTPException):  # order tidak siap
        await t3.buat_pembayaran_pemasok(session, ctx.admin, PembayaranPemasokIn(tanggal=SELASA, order_ids=["x"]))
    p = await t3.buat_pembayaran_pemasok(session, ctx.admin, PembayaranPemasokIn(tanggal=SELASA, order_ids=[lama.id]))
    assert p.total == Decimal("500000")


@pytest.mark.asyncio
async def test_reseller_piutang_and_payment(session, ctx):
    await osvc.set_harga_grosir(
        session, HargaGrosirIn(produk_id=ctx.partisi.id, pelanggan_id=ctx.rina.id, harga=Decimal("600000"), harga_packing_biasa=Decimal("20000"))
    )
    o = await osvc.create_order(
        session, OrderIn(saluran_id=ctx.res_saluran.id, pelanggan_id=ctx.rina.id, produk_id=ctx.partisi.id, qty=2, pemasok_id=ctx.tukang.id, butuh_cat=False)
    )
    assert await t3.list_piutang_reseller(session) == []  # barang belum jadi/diambil dari tukang
    for s in ("dikerjakan", "diambil"):  # diambil = barang jadi -> sudah bisa ditagih, walau belum dicat/dikirim
        await osvc.ubah_status_order(session, o.id, OrderStatusIn(status=s))
    piutang = await t3.list_piutang_reseller(session)
    assert piutang[0].subtotal == Decimal("1250000")  # (600rb + 20rb) x 2 + 10rb

    pen = await t3.buat_penerimaan_reseller(session, ctx.admin, PenerimaanResellerIn(pelanggan_id=ctx.rina.id))
    assert pen.total == Decimal("1250000")
    assert await t3.list_piutang_reseller(session) == []
    assert await services.saldo_akun(session, ctx.kas) == Decimal("1250000")
    with pytest.raises(HTTPException):
        await t3.buat_penerimaan_reseller(session, ctx.admin, PenerimaanResellerIn(pelanggan_id=ctx.rina.id))
    await t3.batalkan_penerimaan_reseller(session, pen.id, "salah catat")
    assert await services.saldo_akun(session, ctx.kas) == 0 and len(await t3.list_piutang_reseller(session)) == 1


async def _akun(session, kode):
    return (await session.execute(select(BlAkunKas).where(BlAkunKas.kode == kode))).scalar_one()


@pytest.mark.asyncio
async def test_gaji_monthly_paid_next_month_first_from_dana_cadangan(session, ctx):
    await _kas_masuk(session, ctx, "10000000")
    await t3.create_karyawan(session, KaryawanIn(nama="Sari", peran="kas_kecil_packing", gaji_bulanan=Decimal("2000000")))
    await t3.create_karyawan(session, KaryawanIn(nama="Tono", peran="tukang_cat", gaji_bulanan=Decimal("2500000")))
    with pytest.raises(HTTPException):
        await t3.create_karyawan(session, KaryawanIn(nama="X", peran="bos", gaji_bulanan=Decimal("1")))
    gaji = await t3.siapkan_gaji(session, "2025-12")
    assert len(gaji) == 2 and len(await t3.siapkan_gaji(session, "2025-12")) == 2  # idempoten
    from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_t3 import GajiOut

    assert GajiOut.model_validate(gaji[0]).jatuh_tempo == date(2026, 1, 1)
    with pytest.raises(HTTPException):  # Dana cadangan masih kosong
        await t3.bayar_gaji(session, ctx.admin, "2025-12", date(2026, 1, 1))

    dana = await _akun(session, "DANA_CADANGAN")  # tanpa cicilan: isi manual dari kas utama
    await services.create_transfer(
        session, ctx.admin, TransferIn(dari_akun_id=ctx.kas.id, ke_akun_id=dana.id, jumlah=Decimal("4500000"))
    )
    dibayar = await t3.bayar_gaji(session, ctx.admin, "2025-12", date(2026, 1, 1))
    assert sum(g.jumlah for g in dibayar) == Decimal("4500000")
    assert await services.saldo_akun(session, dana) == 0
    assert await services.saldo_akun(session, ctx.kas) == Decimal("5500000")
    with pytest.raises(HTTPException):
        await t3.bayar_gaji(session, ctx.admin, "2025-12", None)  # sudah semua
    await t3.batalkan_bayar_gaji(session, dibayar[0].id, "salah")
    assert dibayar[0].tanggal_bayar is None


@pytest.mark.asyncio
async def test_bagi_hasil_profit_loss_and_payment(session, ctx):
    await _kas_masuk(session, ctx, "10000000", tanggal=date(2025, 6, 5))
    await _kas_masuk(session, ctx, "3000000", tanggal=date(2025, 5, 1))  # kas dari bulan lain (di luar laba Juni)
    biaya = (await session.execute(select(BlKategori).where(BlKategori.nama == "Operasional"))).scalar_one()
    prive = (await session.execute(select(BlKategori).where(BlKategori.nama == "Prive"))).scalar_one()
    for kat, jumlah in ((biaya, "4000000"), (prive, "999999")):  # prive tidak mengurangi laba
        await services.create_transaksi(
            session, ctx.admin, TransaksiIn(akun_id=ctx.kas.id, kategori_id=kat.id, jenis="keluar", jumlah=Decimal(jumlah), tanggal=date(2025, 6, 6))
        )
    h = await t3.hitung_bagi_hasil(session, "2025-06")
    assert (h.laba_bersih, h.bagian_admin, h.bagian_owner) == (Decimal("6000000"), Decimal("2400000"), Decimal("3600000"))

    row = await t3.simpan_bagi_hasil(session, ctx.admin, "2025-06")
    with pytest.raises(HTTPException) as exc:
        await t3.simpan_bagi_hasil(session, ctx.admin, "2025-06")
    assert exc.value.status_code == 409
    saldo_sebelum = await services.saldo_akun(session, ctx.kas)
    await t3.bayar_bagi_hasil(session, ctx.admin, row.id, date(2025, 7, 1))
    assert await services.saldo_akun(session, ctx.kas) == saldo_sebelum - Decimal("6000000")
    assert (await t3.hitung_bagi_hasil(session, "2025-06")).laba_bersih == Decimal("6000000")  # 'Bagi hasil' tak dihitung
    with pytest.raises(HTTPException):
        await t3.bayar_bagi_hasil(session, ctx.admin, row.id, None)

    rugi = await t3.simpan_bagi_hasil(session, ctx.admin, "2025-07")  # bulan kosong: laba 0
    assert (rugi.bagian_admin, rugi.bagian_owner) == (0, 0)
    with pytest.raises(HTTPException):
        await t3.bayar_bagi_hasil(session, ctx.admin, rugi.id, None)

    await t3.batalkan_bagi_hasil(session, row.id, "hitung ulang")
    assert await services.saldo_akun(session, ctx.kas) == saldo_sebelum


@pytest.mark.asyncio
async def test_admin_manages_users_and_resets_password(session, ctx):
    owner = await services.create_user(session, ctx.admin, UserCreateIn(nama="O", email="o@t.com", password="rahasia123", role="owner"))
    updated = await services.update_user(session, ctx.admin, owner.id, UserPatchIn(nama="Owner Baru", role="staff"))
    assert (updated.nama, updated.role) == ("Owner Baru", "staff")
    with pytest.raises(HTTPException):  # tidak boleh mengubah role / menonaktifkan diri sendiri
        await services.update_user(session, ctx.admin, ctx.admin.id, UserPatchIn(role="owner"))
    with pytest.raises(HTTPException):
        await services.update_user(session, ctx.admin, ctx.admin.id, UserPatchIn(aktif=False))
    user = await services.reset_password(session, owner.id, ResetPasswordIn(new_password="passwordbaru1"))
    assert user.must_change_password is True
    await services.update_user(session, ctx.admin, owner.id, UserPatchIn(aktif=False))
    from tenants.bumi_lestari.modules.bumi_lestari.application.schemas import LoginIn

    with pytest.raises(HTTPException) as exc:
        await services.authenticate_user(session, LoginIn(email="o@t.com", password="passwordbaru1"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_gaji_installments_then_payment_do_not_double_count(session, ctx):
    from tenants.bumi_lestari.modules.bumi_lestari.application import provisi_services as ps

    await _kas_masuk(session, ctx, "20000000", tanggal=date(2026, 9, 1))
    sari = await t3.create_karyawan(session, KaryawanIn(nama="Sari", peran="kas_kecil_packing", gaji_bulanan=Decimal("2000001")))
    dana = await _akun(session, "DANA_CADANGAN")

    # 4 Selasa di September 2026: 1, 8, 15, 22 (tgl 29 = Selasa ke-5, tidak ada cicilan)
    prev = await ps.hitung_sisihan(session, date(2026, 9, 1))
    assert (prev.periode, prev.minggu_ke, prev.total) == ("2026-09", 1, Decimal("500000.25"))
    for hari in (1, 8, 15, 22):
        await ps.catat_sisihan(session, ctx.admin, date(2026, 9, hari))
    with pytest.raises(HTTPException) as exc:  # sudah dicatat
        await ps.catat_sisihan(session, ctx.admin, date(2026, 9, 22))
    assert exc.value.status_code == 409
    assert (await ps.hitung_sisihan(session, date(2026, 9, 29))).items == []
    with pytest.raises(HTTPException):
        await ps.catat_sisihan(session, ctx.admin, date(2026, 9, 29))

    assert await services.saldo_akun(session, dana) == Decimal("2000001")  # 4 cicilan = tepat sebulan
    assert await services.saldo_akun(session, ctx.kas) == Decimal("20000000") - Decimal("2000001")

    # Laporan: beban gaji diakui mingguan (September), bukan saat dibayar di Oktober.
    h = await t3.hitung_bagi_hasil(session, "2026-09")
    assert (h.pengeluaran, h.laba_bersih) == (Decimal("2000001"), Decimal("20000000") - Decimal("2000001"))

    # Awal Oktober: gaji naik jadi 2,1 juta -> kurang 99.999 di Dana cadangan; top-up dulu dari kas utama.
    await t3.update_karyawan(session, sari.id, KaryawanPatch(gaji_bulanan=Decimal("2100000")))
    await t3.siapkan_gaji(session, "2026-09")
    with pytest.raises(HTTPException) as exc:
        await t3.bayar_gaji(session, ctx.admin, "2026-09", date(2026, 10, 1))
    assert exc.value.status_code == 400
    await services.create_transfer(session, ctx.admin, TransferIn(dari_akun_id=ctx.kas.id, ke_akun_id=dana.id, jumlah=Decimal("99999")))
    (g,) = await t3.bayar_gaji(session, ctx.admin, "2026-09", date(2026, 10, 1))
    assert await services.saldo_akun(session, dana) == 0
    assert (await t3.hitung_bagi_hasil(session, "2026-09")).pengeluaran == Decimal("2000001")  # tidak dobel
    assert (await t3.hitung_bagi_hasil(session, "2026-10")).pengeluaran == Decimal("99999")  # selisih saat dibayar

    await t3.batalkan_bayar_gaji(session, g.id, "salah")  # transaksi & penyesuaian ikut batal
    assert (await t3.hitung_bagi_hasil(session, "2026-10")).pengeluaran == 0


@pytest.mark.asyncio
async def test_langganan_paid_directly_expense_recognized_when_paid(session, ctx):
    from tenants.bumi_lestari.modules.bumi_lestari.application import provisi_services as ps
    from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_t3 import (
        LanggananIn,
        TagihanBayarIn,
        TagihanItemIn,
    )

    await _kas_masuk(session, ctx, "5000000", tanggal=date(2026, 9, 1))
    listrik = await ps.create_langganan(session, LanggananIn(nama="Listrik A", jumlah_bulanan=Decimal("400000")))
    wifi = await ps.create_langganan(session, LanggananIn(nama="Wifi A", jumlah_bulanan=Decimal("350000")))
    assert (await ps.hitung_sisihan(session, date(2026, 9, 1))).items == []  # langganan tidak dicicil

    bayar = TagihanBayarIn(
        periode="2026-09", tanggal=date(2026, 9, 24),  # minggu ke-4: tagihan datang
        items=[TagihanItemIn(langganan_id=listrik.id, jumlah=Decimal("450000")), TagihanItemIn(langganan_id=wifi.id, jumlah=Decimal("350000"))],
    )
    dibayar = await ps.bayar_tagihan(session, ctx.admin, bayar)
    assert await services.saldo_akun(session, ctx.kas) == Decimal("4200000")  # dibayar langsung dari kas utama
    assert (await t3.hitung_bagi_hasil(session, "2026-09")).pengeluaran == Decimal("800000")  # diakui saat dibayar
    with pytest.raises(HTTPException) as exc:
        await ps.bayar_tagihan(session, ctx.admin, bayar)
    assert exc.value.status_code == 409

    await ps.batalkan_tagihan(session, dibayar[0].id, "salah angka")
    assert (await t3.hitung_bagi_hasil(session, "2026-09")).pengeluaran == Decimal("350000")
