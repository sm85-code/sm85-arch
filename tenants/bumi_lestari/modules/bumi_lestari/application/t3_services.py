"""Business logic -- bumi_lestari T3/T4: pembayaran Selasa, penerimaan reseller, gaji, bagi hasil.

Semua uang masuk/keluar di sini menghasilkan BlTransaksi (ref_jenis/ref_id) di keuangan. Pembayaran
pemasok adalah SATU transaksi bertotal; rinciannya (per order/barang) ada di tabel item.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application.provisi_core import (
    batalkan_provisi_sumber,
    beban_provisi,
    sesuaikan,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_order import OrderOut
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_t3 import (
    BagiHasilOut,
    ItemRincianOut,
    ItemSiapBayarOut,
    KaryawanIn,
    KaryawanPatch,
    PembayaranPemasokDetailOut,
    PembayaranPemasokIn,
    PemasokSiapBayarOut,
    PenerimaanResellerIn,
    PiutangItemOut,
    PiutangPelangganOut,
    SiapBayarOut,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.services import (
    _akun_by_kode,
    _batalkan,
    _hari_ini,
    get_proporsi,
    saldo_akun,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import (
    KODE_DANA_CADANGAN,
    KODE_KAS_UTAMA,
    BlAkunKas,
    BlKategori,
    BlTransaksi,
    BlUser,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import (
    BlOrder,
    BlPelanggan,
    BlPemasok,
    BlProduk,
    BlSaluran,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_t3 import (
    PERAN_KARYAWAN,
    REF_BAGI_HASIL,
    REF_GAJI,
    REF_PEMBAYARAN_PEMASOK,
    REF_PENERIMAAN_RESELLER,
    REF_TAGIHAN,
    BlBagiHasil,
    BlGaji,
    BlKaryawan,
    BlPembayaranPemasok,
    BlPembayaranPemasokItem,
    BlPenerimaanReseller,
    BlPenerimaanResellerItem,
)

KATEGORI_PRODUKSI = "Biaya produksi / pembelian barang"
KATEGORI_RESELLER = "Penjualan reseller"
KATEGORI_GAJI = "Gaji karyawan"
KATEGORI_TAGIHAN = "Langganan & utilitas"
KATEGORI_BAGI_HASIL = "Bagi hasil"
# Bukan biaya usaha: tidak mengurangi laba bersih yang dibagi.
KATEGORI_BUKAN_BIAYA = ("Prive", KATEGORI_BAGI_HASIL)


def _bad(detail: str, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


# --- helper bersama -------------------------------------------------------------------


async def _kategori(session: AsyncSession, nama: str) -> BlKategori:
    kat = (await session.execute(select(BlKategori).where(BlKategori.nama == nama))).scalar_one_or_none()
    if kat is None:
        raise _bad(f"Kategori '{nama}' belum ada (jalankan seed-now)", status.HTTP_409_CONFLICT)
    return kat


async def _akun_bayar(session: AsyncSession, akun_id: str | None) -> BlAkunKas:
    if akun_id is None:
        return await _akun_by_kode(session, KODE_KAS_UTAMA)
    akun = await session.get(BlAkunKas, akun_id)
    if akun is None or not akun.aktif:
        raise _bad("Akun kas tidak ditemukan", status.HTTP_404_NOT_FOUND)
    return akun


async def _transaksi_otomatis(
    session: AsyncSession, user: BlUser, *, tanggal: date, akun: BlAkunKas, kategori: str, jenis: str,
    jumlah: Decimal, keterangan: str, ref_jenis: str, ref_id: str,
) -> BlTransaksi:
    kat = await _kategori(session, kategori)
    trx = BlTransaksi(
        tanggal=tanggal, akun_id=akun.id, kategori_id=kat.id, jenis=jenis, jumlah=jumlah,
        keterangan=keterangan, dibuat_oleh=user.id, ref_jenis=ref_jenis, ref_id=ref_id,
    )
    session.add(trx)
    await session.flush()
    return trx


async def _pastikan_saldo(session: AsyncSession, akun: BlAkunKas, jumlah: Decimal) -> None:
    if await saldo_akun(session, akun) < jumlah:
        raise _bad(f"Saldo {akun.nama} tidak cukup untuk Rp {jumlah:,.0f}")


async def _batalkan_transaksi_ref(session: AsyncSession, ref_jenis: str, ref_id: str, alasan: str) -> None:
    rows = (
        await session.execute(
            select(BlTransaksi).where(
                BlTransaksi.ref_jenis == ref_jenis, BlTransaksi.ref_id == ref_id, BlTransaksi.dibatalkan.is_(False)
            )
        )
    ).scalars()
    for trx in rows:
        _batalkan(trx, alasan)


def selasa_acuan(tanggal: date) -> date:
    """Selasa terakhir pada atau sebelum `tanggal` (weekday: Senin=0, Selasa=1)."""
    return tanggal - timedelta(days=(tanggal.weekday() - 1) % 7)


def total_order(order: BlOrder) -> Decimal:
    out = OrderOut.model_validate(order)
    return out.total_penjualan - Decimal(order.potongan_marketplace)


# --- Pembayaran pemasok (tukang kayu + supplier), tiap Selasa -------------------------------


async def order_sudah_dibayar_pemasok(session: AsyncSession) -> set[str]:
    stmt = (
        select(BlPembayaranPemasokItem.order_id)
        .join(BlPembayaranPemasok, BlPembayaranPemasok.id == BlPembayaranPemasokItem.pembayaran_id)
        .where(BlPembayaranPemasok.dibatalkan.is_(False))
    )
    return set((await session.execute(stmt)).scalars())


async def _pembayaran_selasa_aktif(session: AsyncSession, selasa: date) -> BlPembayaranPemasok | None:
    stmt = select(BlPembayaranPemasok).where(
        BlPembayaranPemasok.selasa == selasa, BlPembayaranPemasok.dibatalkan.is_(False)
    )
    return (await session.execute(stmt)).scalars().first()


async def siap_bayar_pemasok(session: AsyncSession, tanggal: date | None = None) -> SiapBayarOut:
    """Order yang sudah diambil/diterima s.d. Sabtu sebelum Selasa acuan dan belum dibayar.

    Aturan: pesanan minggu sebelumnya (Senin-Sabtu) yang selesai dikerjakan dan diambil dibayar Selasa
    ini. Yang diambil Minggu atau Senin-Selasa ini ikut Selasa berikutnya. Order yang terlewat dari
    minggu-minggu sebelumnya tetap muncul, ditandai `terlambat`.
    """
    selasa = selasa_acuan(tanggal or _hari_ini())
    batas = selasa - timedelta(days=3)  # Sabtu
    awal = selasa - timedelta(days=8)  # Senin minggu sebelumnya
    dibayar = await order_sudah_dibayar_pemasok(session)
    orders = (
        await session.execute(
            select(BlOrder, BlPemasok, BlProduk)
            .join(BlPemasok, BlPemasok.id == BlOrder.pemasok_id)
            .join(BlProduk, BlProduk.id == BlOrder.produk_id)
            .where(
                BlOrder.tgl_diambil.is_not(None),
                BlOrder.tgl_diambil <= batas,
                BlOrder.status != "batal",
                BlOrder.biaya_pokok > 0,
            )
            .order_by(BlPemasok.nama, BlOrder.tgl_diambil)
        )
    ).all()
    per_pemasok: dict[str, PemasokSiapBayarOut] = {}
    total = Decimal("0")
    for order, pemasok, produk in orders:
        if order.id in dibayar:
            continue
        grup = per_pemasok.setdefault(
            pemasok.id,
            PemasokSiapBayarOut(
                pemasok_id=pemasok.id, nama=pemasok.nama, jenis=pemasok.jenis, subtotal=Decimal("0"), items=[]
            ),
        )
        jumlah = Decimal(order.biaya_pokok)
        grup.items.append(
            ItemSiapBayarOut(
                order_id=order.id, no_order=order.no_order, produk_id=produk.id, qty=order.qty,
                tgl_diambil=order.tgl_diambil, jumlah=jumlah, terlambat=order.tgl_diambil < awal,
            )
        )
        grup.subtotal += jumlah
        total += jumlah
    existing = await _pembayaran_selasa_aktif(session, selasa)
    return SiapBayarOut(
        selasa=selasa, batas_diambil=batas, sudah_dicatat_id=existing.id if existing else None,
        total=total, pemasok=list(per_pemasok.values()),
    )


async def buat_pembayaran_pemasok(
    session: AsyncSession, user: BlUser, payload: PembayaranPemasokIn
) -> BlPembayaranPemasok:
    """Tombol "Kirim ke laporan": satu pembayaran per Selasa -> SATU transaksi keluar bertotal."""
    tanggal = payload.tanggal or _hari_ini()
    siap = await siap_bayar_pemasok(session, tanggal)
    if siap.sudah_dicatat_id:
        raise _bad("Pembayaran untuk Selasa ini sudah dicatat (batalkan dulu bila ingin mengulang)", 409)
    semua = {item.order_id: item for grup in siap.pemasok for item in grup.items}
    if payload.order_ids is not None:
        tidak_siap = [oid for oid in payload.order_ids if oid not in semua]
        if tidak_siap:
            raise _bad(f"Order tidak siap dibayar atau sudah dibayar: {', '.join(tidak_siap)}")
        semua = {oid: semua[oid] for oid in payload.order_ids}
    if not semua:
        raise _bad("Tidak ada order yang perlu dibayar")
    total = sum((i.jumlah for i in semua.values()), Decimal("0"))
    akun = await _akun_bayar(session, payload.akun_id)
    await _pastikan_saldo(session, akun, total)

    pembayaran = BlPembayaranPemasok(
        selasa=siap.selasa, tanggal=tanggal, akun_id=akun.id, total=total, dibuat_oleh=user.id
    )
    session.add(pembayaran)
    await session.flush()
    pemasok_per_order = {i.order_id: g.pemasok_id for g in siap.pemasok for i in g.items}
    for oid, item in semua.items():
        session.add(
            BlPembayaranPemasokItem(
                pembayaran_id=pembayaran.id, order_id=oid, pemasok_id=pemasok_per_order[oid], jumlah=item.jumlah
            )
        )
    await _transaksi_otomatis(
        session, user, tanggal=tanggal, akun=akun, kategori=KATEGORI_PRODUKSI, jenis="keluar", jumlah=total,
        keterangan=f"Pembayaran pemasok Selasa {siap.selasa:%d-%m-%Y} ({len(semua)} order)",
        ref_jenis=REF_PEMBAYARAN_PEMASOK, ref_id=pembayaran.id,
    )
    await session.flush()
    return pembayaran


async def list_pembayaran_pemasok(session: AsyncSession) -> list[BlPembayaranPemasok]:
    stmt = select(BlPembayaranPemasok).order_by(BlPembayaranPemasok.selasa.desc())
    return list((await session.execute(stmt)).scalars())


async def detail_pembayaran_pemasok(session: AsyncSession, pembayaran_id: str) -> PembayaranPemasokDetailOut:
    p = await session.get(BlPembayaranPemasok, pembayaran_id)
    if p is None:
        raise _bad("Pembayaran tidak ditemukan", status.HTTP_404_NOT_FOUND)
    rows = (
        await session.execute(
            select(BlPembayaranPemasokItem, BlOrder, BlProduk, BlPemasok)
            .join(BlOrder, BlOrder.id == BlPembayaranPemasokItem.order_id)
            .join(BlProduk, BlProduk.id == BlOrder.produk_id)
            .join(BlPemasok, BlPemasok.id == BlPembayaranPemasokItem.pemasok_id)
            .where(BlPembayaranPemasokItem.pembayaran_id == p.id)
            .order_by(BlPemasok.nama, BlOrder.tgl_diambil)
        )
    ).all()
    items = [
        ItemRincianOut(
            order_id=o.id, no_order=o.no_order, produk_sku=pr.sku, produk_nama=pr.nama, qty=o.qty,
            pemasok_nama=pm.nama, jumlah=it.jumlah,
        )
        for it, o, pr, pm in rows
    ]
    trx_id = (
        await session.execute(
            select(BlTransaksi.id).where(BlTransaksi.ref_jenis == REF_PEMBAYARAN_PEMASOK, BlTransaksi.ref_id == p.id)
        )
    ).scalars().first()
    return PembayaranPemasokDetailOut(
        id=p.id, selasa=p.selasa, tanggal=p.tanggal, akun_id=p.akun_id, total=p.total, dibatalkan=p.dibatalkan,
        transaksi_id=trx_id, total_qty=sum(i.qty for i in items), items=items,
    )


async def batalkan_pembayaran_pemasok(session: AsyncSession, pembayaran_id: str, alasan: str) -> BlPembayaranPemasok:
    p = await session.get(BlPembayaranPemasok, pembayaran_id)
    if p is None:
        raise _bad("Pembayaran tidak ditemukan", status.HTTP_404_NOT_FOUND)
    _batalkan(p, alasan)
    await _batalkan_transaksi_ref(session, REF_PEMBAYARAN_PEMASOK, p.id, alasan)
    await session.flush()
    return p


# --- Penerimaan dari penjual lain (reseller) -----------------------------------------------


async def order_sudah_dibayar_reseller(session: AsyncSession) -> set[str]:
    stmt = (
        select(BlPenerimaanResellerItem.order_id)
        .join(BlPenerimaanReseller, BlPenerimaanReseller.id == BlPenerimaanResellerItem.penerimaan_id)
        .where(BlPenerimaanReseller.dibatalkan.is_(False))
    )
    return set((await session.execute(stmt)).scalars())


async def list_piutang_reseller(session: AsyncSession, pelanggan_id: str | None = None) -> list[PiutangPelangganOut]:
    """Order reseller yang barangnya sudah jadi dan diambil dari tukang (tgl_diambil) dan belum dibayar."""
    dibayar = await order_sudah_dibayar_reseller(session)
    stmt = (
        select(BlOrder, BlPelanggan)
        .join(BlSaluran, BlSaluran.id == BlOrder.saluran_id)
        .join(BlPelanggan, BlPelanggan.id == BlOrder.pelanggan_id)
        .where(BlSaluran.jenis == "reseller", BlOrder.status != "batal", BlOrder.tgl_diambil.is_not(None))
        .order_by(BlPelanggan.nama, BlOrder.tanggal_order)
    )
    if pelanggan_id:
        stmt = stmt.where(BlOrder.pelanggan_id == pelanggan_id)
    hasil: dict[str, PiutangPelangganOut] = {}
    for order, pelanggan in (await session.execute(stmt)).all():
        if order.id in dibayar:
            continue
        grup = hasil.setdefault(
            pelanggan.id, PiutangPelangganOut(pelanggan_id=pelanggan.id, nama=pelanggan.nama, subtotal=Decimal("0"), items=[])
        )
        jumlah = total_order(order)
        grup.items.append(
            PiutangItemOut(order_id=order.id, no_order=order.no_order, tanggal_order=order.tanggal_order, jumlah=jumlah)
        )
        grup.subtotal += jumlah
    return list(hasil.values())


async def buat_penerimaan_reseller(
    session: AsyncSession, user: BlUser, payload: PenerimaanResellerIn
) -> BlPenerimaanReseller:
    if await session.get(BlPelanggan, payload.pelanggan_id) is None:
        raise _bad("Pelanggan tidak ditemukan", status.HTTP_404_NOT_FOUND)
    piutang = await list_piutang_reseller(session, payload.pelanggan_id)
    semua = {i.order_id: i for g in piutang for i in g.items}
    if payload.order_ids is not None:
        tidak = [oid for oid in payload.order_ids if oid not in semua]
        if tidak:
            raise _bad(f"Order bukan piutang pelanggan ini atau sudah dibayar: {', '.join(tidak)}")
        semua = {oid: semua[oid] for oid in payload.order_ids}
    if not semua:
        raise _bad("Tidak ada piutang yang dibayar")
    total = sum((i.jumlah for i in semua.values()), Decimal("0"))
    akun = await _akun_bayar(session, payload.akun_id)
    tanggal = payload.tanggal or _hari_ini()
    penerimaan = BlPenerimaanReseller(
        tanggal=tanggal, pelanggan_id=payload.pelanggan_id, akun_id=akun.id, total=total, dibuat_oleh=user.id
    )
    session.add(penerimaan)
    await session.flush()
    for oid, item in semua.items():
        session.add(BlPenerimaanResellerItem(penerimaan_id=penerimaan.id, order_id=oid, jumlah=item.jumlah))
    pelanggan = await session.get(BlPelanggan, payload.pelanggan_id)
    await _transaksi_otomatis(
        session, user, tanggal=tanggal, akun=akun, kategori=KATEGORI_RESELLER, jenis="masuk", jumlah=total,
        keterangan=f"Pembayaran {pelanggan.nama} ({len(semua)} order)",
        ref_jenis=REF_PENERIMAAN_RESELLER, ref_id=penerimaan.id,
    )
    await session.flush()
    return penerimaan


async def list_penerimaan_reseller(session: AsyncSession) -> list[BlPenerimaanReseller]:
    stmt = select(BlPenerimaanReseller).order_by(BlPenerimaanReseller.tanggal.desc())
    return list((await session.execute(stmt)).scalars())


async def batalkan_penerimaan_reseller(session: AsyncSession, penerimaan_id: str, alasan: str) -> BlPenerimaanReseller:
    p = await session.get(BlPenerimaanReseller, penerimaan_id)
    if p is None:
        raise _bad("Penerimaan tidak ditemukan", status.HTTP_404_NOT_FOUND)
    _batalkan(p, alasan)
    await _batalkan_transaksi_ref(session, REF_PENERIMAAN_RESELLER, p.id, alasan)
    await session.flush()
    return p


# --- Karyawan tetap & gaji bulanan -------------------------------------------------------------


def _cek_peran(peran: str) -> None:
    if peran not in PERAN_KARYAWAN:
        raise _bad(f"Peran harus salah satu dari: {', '.join(PERAN_KARYAWAN)}")


async def list_karyawan(session: AsyncSession) -> list[BlKaryawan]:
    stmt = select(BlKaryawan).where(BlKaryawan.aktif.is_(True)).order_by(BlKaryawan.nama)
    return list((await session.execute(stmt)).scalars())


async def create_karyawan(session: AsyncSession, payload: KaryawanIn) -> BlKaryawan:
    _cek_peran(payload.peran)
    if payload.user_id and await session.get(BlUser, payload.user_id) is None:
        raise _bad("Pengguna tidak ditemukan", status.HTTP_404_NOT_FOUND)
    k = BlKaryawan(
        nama=payload.nama.strip(), peran=payload.peran, gaji_bulanan=payload.gaji_bulanan, user_id=payload.user_id
    )
    session.add(k)
    await session.flush()
    return k


async def update_karyawan(session: AsyncSession, karyawan_id: str, payload: KaryawanPatch) -> BlKaryawan:
    k = await session.get(BlKaryawan, karyawan_id)
    if k is None:
        raise _bad("Karyawan tidak ditemukan", status.HTTP_404_NOT_FOUND)
    data = payload.model_dump(exclude_unset=True)
    if "peran" in data:
        _cek_peran(data["peran"])
    for kolom, nilai in data.items():
        if nilai is not None or kolom == "user_id":
            setattr(k, kolom, nilai.strip() if isinstance(nilai, str) else nilai)
    await session.flush()
    return k


async def siapkan_gaji(session: AsyncSession, periode: str) -> list[BlGaji]:
    """Buat baris gaji periode untuk semua karyawan aktif yang belum punya (gaji disalin saat ini)."""
    ada = set((await session.execute(select(BlGaji.karyawan_id).where(BlGaji.periode == periode))).scalars())
    for k in await list_karyawan(session):
        if k.id not in ada:
            session.add(BlGaji(periode=periode, karyawan_id=k.id, jumlah=k.gaji_bulanan))
    await session.flush()
    return await list_gaji(session, periode)


async def list_gaji(session: AsyncSession, periode: str) -> list[BlGaji]:
    stmt = select(BlGaji).where(BlGaji.periode == periode).order_by(BlGaji.id)
    return list((await session.execute(stmt)).scalars())


async def bayar_gaji(session: AsyncSession, user: BlUser, periode: str, tanggal: date | None) -> list[BlGaji]:
    belum = [g for g in await list_gaji(session, periode) if g.tanggal_bayar is None]
    if not belum:
        raise _bad("Tidak ada gaji yang belum dibayar untuk periode ini (jalankan 'siapkan gaji' dulu)")
    total = sum((Decimal(g.jumlah) for g in belum), Decimal("0"))
    # Gaji dibayar dari Dana cadangan (disisihkan mingguan); beban sudah diakui lewat cicilan, jadi transaksi
    # pembayaran tidak dihitung lagi sebagai biaya (lihat KATEGORI_TIDAK_DIHITUNG di bagi hasil).
    akun = await _akun_by_kode(session, KODE_DANA_CADANGAN)
    await _pastikan_saldo(session, akun, total)
    tanggal = tanggal or _hari_ini()
    nama = {k.id: k.nama for k in (await session.execute(select(BlKaryawan))).scalars()}
    for g in belum:
        await sesuaikan(
            session, tanggal=tanggal, periode=g.periode, jenis="gaji", target=Decimal(g.jumlah),
            karyawan_id=g.karyawan_id, sumber_jenis=REF_GAJI, sumber_id=g.id,
        )
        await _transaksi_otomatis(
            session, user, tanggal=tanggal, akun=akun, kategori=KATEGORI_GAJI, jenis="keluar",
            jumlah=Decimal(g.jumlah), keterangan=f"Gaji {nama[g.karyawan_id]} {periode}",
            ref_jenis=REF_GAJI, ref_id=g.id,
        )
        g.tanggal_bayar = tanggal
        g.dibayar_oleh = user.id
    await session.flush()
    return belum


async def batalkan_bayar_gaji(session: AsyncSession, gaji_id: str, alasan: str) -> BlGaji:
    g = await session.get(BlGaji, gaji_id)
    if g is None:
        raise _bad("Gaji tidak ditemukan", status.HTTP_404_NOT_FOUND)
    if g.tanggal_bayar is None:
        raise _bad("Gaji ini belum dibayar", status.HTTP_409_CONFLICT)
    await _batalkan_transaksi_ref(session, REF_GAJI, g.id, alasan)
    await batalkan_provisi_sumber(session, REF_GAJI, g.id, alasan)
    g.tanggal_bayar = None
    g.dibayar_oleh = None
    await session.flush()
    return g


# --- Bagi hasil bulanan admin & owner --------------------------------------------------------


def _rentang_periode(periode: str) -> tuple[date, date]:
    tahun, bulan = int(periode[:4]), int(periode[5:])
    awal = date(tahun, bulan, 1)
    berikut = date(tahun + (bulan == 12), bulan % 12 + 1, 1)
    return awal, berikut - timedelta(days=1)


async def hitung_bagi_hasil(session: AsyncSession, periode: str) -> BagiHasilOut:
    """Laba bersih = pemasukan - pengeluaran periode (basis kas). Transfer, Prive, Bagi hasil tidak dihitung."""
    awal, akhir = _rentang_periode(periode)

    async def jumlah(jenis: str, *, kecuali: tuple[str, ...] = (), kecuali_ref: tuple[str, ...] = ()) -> Decimal:
        stmt = (
            select(func.coalesce(func.sum(BlTransaksi.jumlah), 0))
            .join(BlKategori, BlKategori.id == BlTransaksi.kategori_id)
            .where(
                BlTransaksi.jenis == jenis, BlTransaksi.dibatalkan.is_(False),
                BlTransaksi.tanggal >= awal, BlTransaksi.tanggal <= akhir,
            )
        )
        if kecuali:
            stmt = stmt.where(BlKategori.nama.not_in(kecuali))
        if kecuali_ref:
            stmt = stmt.where(or_(BlTransaksi.ref_jenis.is_(None), BlTransaksi.ref_jenis.not_in(kecuali_ref)))
        return Decimal(str((await session.execute(stmt)).scalar_one() or 0))

    pemasukan = await jumlah("masuk")
    # Gaji & tagihan langganan dibayar dari Dana cadangan: bebannya sudah diakui lewat cicilan mingguan
    # (BlProvisi), jadi transaksi pembayarannya dikecualikan agar tidak dihitung dua kali.
    pengeluaran = await jumlah("keluar", kecuali=KATEGORI_BUKAN_BIAYA, kecuali_ref=(REF_GAJI, REF_TAGIHAN))
    pengeluaran += await beban_provisi(session, awal, akhir)
    laba = pemasukan - pengeluaran
    proporsi = {p.penerima: Decimal(p.persen) for p in await get_proporsi(session)}
    if set(proporsi) != {"admin", "owner"}:
        raise _bad("Proporsi bagi hasil belum diatur (jalankan seed-now atau atur di profil UMKM)", 409)
    if laba > 0:
        admin = (laba * proporsi["admin"] / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        owner = laba - admin
    else:
        admin = owner = Decimal("0")
    return BagiHasilOut(
        periode=periode, pemasukan=pemasukan, pengeluaran=pengeluaran, laba_bersih=laba,
        persen_admin=proporsi["admin"], persen_owner=proporsi["owner"], bagian_admin=admin, bagian_owner=owner,
    )


async def _bagi_hasil_aktif(session: AsyncSession, periode: str) -> BlBagiHasil | None:
    stmt = select(BlBagiHasil).where(BlBagiHasil.periode == periode, BlBagiHasil.dibatalkan.is_(False))
    return (await session.execute(stmt)).scalars().first()


async def simpan_bagi_hasil(session: AsyncSession, user: BlUser, periode: str) -> BlBagiHasil:
    if await _bagi_hasil_aktif(session, periode):
        raise _bad("Bagi hasil periode ini sudah disimpan (batalkan dulu bila ingin menghitung ulang)", 409)
    h = await hitung_bagi_hasil(session, periode)
    row = BlBagiHasil(
        periode=periode, laba_bersih=h.laba_bersih, persen_admin=h.persen_admin, persen_owner=h.persen_owner,
        bagian_admin=h.bagian_admin, bagian_owner=h.bagian_owner, dibuat_oleh=user.id,
    )
    session.add(row)
    await session.flush()
    return row


async def list_bagi_hasil(session: AsyncSession) -> list[BlBagiHasil]:
    return list((await session.execute(select(BlBagiHasil).order_by(BlBagiHasil.periode.desc()))).scalars())


async def bayar_bagi_hasil(session: AsyncSession, user: BlUser, bagi_hasil_id: str, tanggal: date | None) -> BlBagiHasil:
    row = await session.get(BlBagiHasil, bagi_hasil_id)
    if row is None or row.dibatalkan:
        raise _bad("Bagi hasil tidak ditemukan", status.HTTP_404_NOT_FOUND)
    if row.tanggal_bayar is not None:
        raise _bad("Bagi hasil ini sudah dibayar", status.HTTP_409_CONFLICT)
    total = Decimal(row.bagian_admin) + Decimal(row.bagian_owner)
    if total <= 0:
        raise _bad("Tidak ada bagi hasil yang dibayarkan (laba nol atau rugi)")
    akun = await _akun_by_kode(session, KODE_KAS_UTAMA)
    await _pastikan_saldo(session, akun, total)
    tanggal = tanggal or _hari_ini()
    for peran, jumlah in (("admin", row.bagian_admin), ("owner", row.bagian_owner)):
        if Decimal(jumlah) > 0:
            await _transaksi_otomatis(
                session, user, tanggal=tanggal, akun=akun, kategori=KATEGORI_BAGI_HASIL, jenis="keluar",
                jumlah=Decimal(jumlah), keterangan=f"Bagi hasil {peran} {row.periode}",
                ref_jenis=REF_BAGI_HASIL, ref_id=row.id,
            )
    row.tanggal_bayar = tanggal
    await session.flush()
    return row


async def batalkan_bagi_hasil(session: AsyncSession, bagi_hasil_id: str, alasan: str) -> BlBagiHasil:
    row = await session.get(BlBagiHasil, bagi_hasil_id)
    if row is None:
        raise _bad("Bagi hasil tidak ditemukan", status.HTTP_404_NOT_FOUND)
    _batalkan(row, alasan)
    await _batalkan_transaksi_ref(session, REF_BAGI_HASIL, row.id, alasan)
    await session.flush()
    return row
