"""Laporan keuangan Fase 2.13 (spesifikasi 9.1-9.3, 9.7).

Laba rugi memakai angka yang sama dengan bagi hasil dan snapshot tutup buku (`ringkasan_laba`: entri terkirim, HPP =
pembayaran tukang/supplier, retur direklas ke Kerugian retur, gaji lewat cicilan), lalu dirinci per saluran. HPP yang
dicocokkan per order ditampilkan sebagai info dan dirinci di laporan HPP & margin."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application.kategori_core import (
    KATEGORI_BAGI_HASIL,
    KATEGORI_BIAYA_IKLAN,
    KATEGORI_BIAYA_MARKETPLACE,
    KATEGORI_PENJUALAN_MARKETPLACE,
    KATEGORI_PENJUALAN_WEB,
    KATEGORI_PRIVE,
    KATEGORI_PRODUKSI,
    KATEGORI_RESELLER,
    KATEGORI_SETORAN_MODAL,
    KATEGORI_STAF,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.laba_core import ringkasan_laba
from tenants.bumi_lestari.modules.bumi_lestari.application.laporan_services import belum_cair
from tenants.bumi_lestari.modules.bumi_lestari.application.pembayaran_services import _rentang_periode, tagihan_order
from tenants.bumi_lestari.modules.bumi_lestari.application.provisi_core import beban_provisi
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_keuangan import (
    BagiHasilOwner,
    BarisNilai,
    CatatanBelumCair,
    HppMarginOut,
    LabaRugiOut,
    MarginBaris,
    NeracaOut,
    RingkasanOwnerOut,
    TrenBulan,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_order import OrderOut
from tenants.bumi_lestari.modules.bumi_lestari.application.services import _hari_ini, saldo_akun
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import (
    JENIS_KEWAJIBAN,
    STATUS_DITUTUP,
    STATUS_TERKIRIM,
    BlAkunKas,
    BlKategori,
    BlTransaksi,
    BlTutupBuku,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import (
    JENIS_SALURAN_CAIR,
    STATUS_BATAL,
    STATUS_CAIR_CAIR,
    STATUS_RETUR,
    BlOrder,
    BlPemasok,
    BlProduk,
    BlSaluran,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_pembayaran import (
    REF_GAJI,
    BlBagiHasil,
    BlPembayaranPemasok,
    BlPembayaranPemasokItem,
    BlPenerimaanReseller,
    BlPenerimaanResellerItem,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_pencairan import BlPencairanBaris, BlPencairanUnggahan
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_talangan import BlTalangan, BlTalanganBayar

_NOL = Decimal("0")
AWAL_BUKU = date(2000, 1, 1)
KATEGORI_PENJUALAN = (KATEGORI_PENJUALAN_MARKETPLACE, KATEGORI_PENJUALAN_WEB, KATEGORI_RESELLER)
LABEL_PENJUAL_LAIN = "Penjual lain"
LABEL_BIAYA_PROSES = "Pendapatan biaya proses"
LABEL_KAS_KECIL = "Kas kecil (transport, packing, operasional, lainnya)"


def _margin(laba: Decimal, jual: Decimal) -> Decimal | None:
    return (laba / jual * 100).quantize(Decimal("0.1")) if jual else None


async def _sementara(session: AsyncSession, periode: str) -> bool:
    tb = (await session.execute(select(BlTutupBuku).where(BlTutupBuku.periode == periode))).scalar_one_or_none()
    return not (tb and tb.status == STATUS_DITUTUP)


async def _per_akun(session: AsyncSession, awal: date, akhir: date, kategori: tuple[str, ...]):
    """(akun_id, kategori, jenis trx, jumlah) untuk entri terkirim -- filter sama dengan ringkasan_laba."""
    return (
        await session.execute(
            select(BlTransaksi.akun_id, BlKategori.nama, BlTransaksi.jenis, func.sum(BlTransaksi.jumlah))
            .join(BlKategori, BlKategori.id == BlTransaksi.kategori_id)
            .where(
                BlKategori.nama.in_(kategori), BlTransaksi.dibatalkan.is_(False), BlTransaksi.status_kirim == STATUS_TERKIRIM,
                BlTransaksi.tanggal >= awal, BlTransaksi.tanggal <= akhir,
            )
            .group_by(BlTransaksi.akun_id, BlKategori.nama, BlTransaksi.jenis)
        )
    ).all()


async def _saluran_per_akun(session: AsyncSession) -> dict[str, str]:
    rows = (await session.execute(select(BlSaluran.akun_id, BlSaluran.nama).where(BlSaluran.akun_id.is_not(None)))).all()
    hasil: dict[str, str] = {}
    for akun_id, nama in rows:
        hasil.setdefault(akun_id, nama)
    return hasil


def _urut(d: dict[str, Decimal]) -> list[BarisNilai]:
    return [BarisNilai(label=k, jumlah=v) for k, v in sorted(d.items()) if v]


async def _rincian_potongan(session: AsyncSession, awal: date, akhir: date) -> dict[str, dict[str, Decimal]]:
    """Rincian biaya marketplace per saluran per jenis potongan dari tabel standar pencairan (unggahan terkirim)."""
    rows = (
        await session.execute(
            select(BlSaluran.nama, BlPencairanBaris.rincian_biaya)
            .join(BlPencairanUnggahan, BlPencairanUnggahan.id == BlPencairanBaris.unggahan_id)
            .join(BlSaluran, BlSaluran.id == BlPencairanBaris.saluran_id)
            .where(
                BlPencairanUnggahan.status_kirim == STATUS_TERKIRIM, BlPencairanUnggahan.dibatalkan.is_(False),
                BlPencairanBaris.dibatalkan.is_(False), BlPencairanBaris.tanggal_cair >= awal, BlPencairanBaris.tanggal_cair <= akhir,
            )
        )
    ).all()
    hasil: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: _NOL))
    for sal, rincian in rows:
        for nama, nilai in (rincian or {}).items():
            hasil[sal][nama] += Decimal(str(nilai))
    return hasil


async def laba_rugi(session: AsyncSession, periode: str) -> LabaRugiOut:
    awal, akhir = _rentang_periode(periode)
    r = await ringkasan_laba(session, awal, akhir)
    sal_akun = await _saluran_per_akun(session)

    jual: dict[str, Decimal] = defaultdict(lambda: _NOL)
    biaya_mp: dict[str, Decimal] = defaultdict(lambda: _NOL)
    for akun_id, kat, jenis, jml in await _per_akun(session, awal, akhir, (*KATEGORI_PENJUALAN, KATEGORI_BIAYA_MARKETPLACE)):
        jml = Decimal(jml)
        if kat == KATEGORI_BIAYA_MARKETPLACE:
            biaya_mp[sal_akun.get(akun_id, "Lainnya")] += jml if jenis == "keluar" else -jml
            continue
        if kat == KATEGORI_RESELLER:
            label = LABEL_PENJUAL_LAIN
        else:
            label = sal_akun.get(akun_id) or ("Toko web" if kat == KATEGORI_PENJUALAN_WEB else "Marketplace lain")
        jual[label] += jml if jenis == "masuk" else -jml
    rincian = await _rincian_potongan(session, awal, akhir)
    biaya_rows = [
        BarisNilai(label=k, jumlah=v, rincian=_urut(rincian.get(k, {}))) for k, v in sorted(biaya_mp.items()) if v
    ]

    pendapatan_lain = [BarisNilai(label=b.kategori, jumlah=b.jumlah) for b in r.pemasukan if b.kategori not in (*KATEGORI_PENJUALAN, KATEGORI_BIAYA_MARKETPLACE) and b.jumlah]
    total_jual = sum(jual.values(), _NOL)
    total_mp = sum(biaya_mp.values(), _NOL)
    hpp = sum((b.jumlah for b in r.biaya if b.kategori == KATEGORI_PRODUKSI), _NOL)
    kas_kecil = sum((b.jumlah for b in r.biaya if b.kategori in KATEGORI_STAF), _NOL)
    operasional = [BarisNilai(label=LABEL_KAS_KECIL, jumlah=kas_kecil, rincian=[
        BarisNilai(label=b.kategori, jumlah=b.jumlah) for b in r.biaya if b.kategori in KATEGORI_STAF and b.jumlah
    ])] if kas_kecil else []
    lain = [b for b in r.biaya if b.kategori not in (*KATEGORI_STAF, KATEGORI_PRODUKSI, KATEGORI_BIAYA_MARKETPLACE) and b.jumlah]
    lain.sort(key=lambda b: (b.kategori != KATEGORI_BIAYA_IKLAN, b.kategori))  # Biaya iklan satu baris, paling atas
    operasional += [BarisNilai(label=b.kategori, jumlah=b.jumlah) for b in lain]
    total_op = sum((b.jumlah for b in operasional), _NOL)
    laba_kotor = total_jual - total_mp - hpp
    laba_bersih = laba_kotor - total_op + sum((b.jumlah for b in pendapatan_lain), _NOL)

    bc = await belum_cair(session, akhir)
    margin, proses = await _hpp_margin(session, periode)
    # Biaya proses penjual lain: tetap bagian total penjualan, tetapi tampil sebagai baris tersendiri.
    proses = min(proses, max(jual.get(LABEL_PENJUAL_LAIN, _NOL), _NOL))
    if proses:
        jual[LABEL_PENJUAL_LAIN] -= proses
    baris_jual = _urut(jual) + ([BarisNilai(label=LABEL_BIAYA_PROSES, jumlah=proses)] if proses else [])
    return LabaRugiOut(
        periode=periode, sementara=await _sementara(session, periode),
        penjualan=baris_jual, total_penjualan=total_jual, pendapatan_biaya_proses=proses, biaya_marketplace=biaya_rows, total_biaya_marketplace=total_mp,
        penjualan_bersih=total_jual - total_mp, hpp=hpp, laba_kotor=laba_kotor, margin_persen=_margin(laba_kotor, total_jual),
        biaya_operasional=operasional, total_biaya_operasional=total_op, pendapatan_lain=pendapatan_lain,
        laba_bersih=laba_bersih, hpp_dicocokkan=margin.total.hpp,
        belum_cair=CatatanBelumCair(
            total_penjualan=bc.total_penjualan, total_perkiraan_cair=bc.total_perkiraan_cair, jumlah_order=bc.jumlah_order,
            tgl_kirim_tertua=bc.tgl_kirim_tertua, perkiraan_laba_jika_cair=laba_bersih + bc.total_perkiraan_cair,
        ),
        di_luar_laba=[BarisNilai(label=b.kategori, jumlah=b.jumlah) for b in r.di_luar_laba if b.jumlah],
    )


# --- HPP & margin (9.3): per order yang penjualannya diakui di bulan itu ---

async def _order_diakui(session: AsyncSession, awal: date, akhir: date) -> list[tuple[BlOrder, BlSaluran, BlProduk, bool]]:
    """Marketplace/Toko web: cair di bulan itu (bukan retur). Penjual lain: diterima (penerimaan terkirim) di bulan itu."""
    cair = (
        await session.execute(
            select(BlOrder, BlSaluran, BlProduk)
            .join(BlSaluran, BlSaluran.id == BlOrder.saluran_id)
            .join(BlProduk, BlProduk.id == BlOrder.produk_id)
            .where(
                BlSaluran.jenis.in_(JENIS_SALURAN_CAIR), BlOrder.status.not_in((STATUS_BATAL, STATUS_RETUR)),
                BlOrder.status_cair == STATUS_CAIR_CAIR, BlOrder.tgl_cair >= awal, BlOrder.tgl_cair <= akhir,
            )
        )
    ).all()
    diterima = (
        await session.execute(
            select(BlOrder, BlSaluran, BlProduk)
            .join(BlSaluran, BlSaluran.id == BlOrder.saluran_id)
            .join(BlProduk, BlProduk.id == BlOrder.produk_id)
            .where(
                BlOrder.status != STATUS_BATAL,
                BlOrder.id.in_(
                    select(BlPenerimaanResellerItem.order_id)
                    .join(BlPenerimaanReseller, BlPenerimaanReseller.id == BlPenerimaanResellerItem.penerimaan_id)
                    .where(
                        BlPenerimaanReseller.dibatalkan.is_(False), BlPenerimaanReseller.status_kirim == STATUS_TERKIRIM,
                        BlPenerimaanReseller.tanggal >= awal, BlPenerimaanReseller.tanggal <= akhir,
                    )
                ),
            )
        )
    ).all()
    return [(o, s, p, True) for o, s, p in cair] + [(o, s, p, False) for o, s, p in diterima]


async def _harga_dari_file(session: AsyncSession, orders: list[BlOrder]) -> dict[str, Decimal]:
    """Pendapatan margin order marketplace/Toko web dari harga jual di baris pencairan (file atau entri manual).

    Satu baris bisa mencakup beberapa order (nomor pesanan sama): dibagi menurut pendapatan produk tiap order, atau
    rata bila semuanya 0. Baris tanpa harga jual: hanya dipakai untuk order berharga 0 (jumlah cair + potongan)."""
    per_baris: dict[str, list[BlOrder]] = defaultdict(list)
    for o in orders:
        if o.pencairan_baris_id:
            per_baris[o.pencairan_baris_id].append(o)
    if not per_baris:
        return {}
    rows = (await session.execute(select(BlPencairanBaris).where(BlPencairanBaris.id.in_(list(per_baris))))).scalars()
    hasil: dict[str, Decimal] = {}
    for b in rows:
        grup = per_baris[b.id]
        dasar = [OrderOut.model_validate(o).pendapatan_produk for o in grup]
        if b.harga_jual is not None:
            nilai = Decimal(b.harga_jual)
        elif not any(dasar):
            nilai = Decimal(b.jumlah_cair) + Decimal(b.potongan_biaya or 0)
        else:
            continue  # file tanpa harga jual: pakai harga order
        total_dasar = sum(dasar, _NOL)
        sisa = nilai
        for i, (o, d) in enumerate(zip(grup, dasar)):
            if i == len(grup) - 1:
                bagian = sisa
            elif total_dasar:
                bagian = (nilai * d / total_dasar).quantize(Decimal("0.01"))
            else:
                bagian = (nilai / len(grup)).quantize(Decimal("0.01"))
            sisa -= bagian
            hasil[o.id] = bagian
    return hasil


def _baris_margin(label: str, d: dict) -> MarginBaris:
    kotor = d["penjualan"] - d["hpp"]  # margin kotor
    laba = kotor - d["potongan"]  # margin bersih saluran
    return MarginBaris(
        label=label, qty=d["qty"], jumlah_order=d["order"], penjualan=d["penjualan"], potongan=d["potongan"], hpp=d["hpp"],
        laba_kotor=laba, margin_persen=_margin(laba, d["penjualan"]), margin_kotor=kotor,
        margin_kotor_persen=_margin(kotor, d["penjualan"]), biaya_proses=d["proses"],
    )


async def _hpp_margin(session: AsyncSession, periode: str) -> tuple[HppMarginOut, Decimal]:
    """Margin produk = (barang x qty + cat/jasa + packing) − potongan − biaya tukang & supplier. Biaya proses penjual
    lain dicatat terpisah (bukan margin produk). Marketplace/Toko web: pendapatan dari harga jual file pencairan bila ada.
    Mengembalikan juga biaya proses order penjual lain (untuk memecah baris penjualan di laba rugi)."""
    awal, akhir = _rentang_periode(periode)

    def kosong():
        return {"qty": 0, "order": 0, "penjualan": _NOL, "potongan": _NOL, "hpp": _NOL, "proses": _NOL}

    produk: dict[str, dict] = defaultdict(kosong)
    saluran: dict[str, dict] = defaultdict(kosong)
    total = kosong()
    proses_penjual_lain = _NOL
    diakui = await _order_diakui(session, awal, akhir)
    file_jual = await _harga_dari_file(session, [o for o, _s, _p, m in diakui if m])
    for o, sal, prod, marketplace in diakui:
        out = OrderOut.model_validate(o)
        proses = Decimal(o.biaya_proses or 0)
        if marketplace and o.id in file_jual:
            jual, proses = file_jual[o.id], _NOL  # harga jual file sudah mencakup semuanya
        else:
            jual = out.pendapatan_produk
        if not marketplace:
            proses_penjual_lain += proses
        potong = Decimal(o.potongan_aktual if o.potongan_aktual is not None else o.potongan_marketplace) if marketplace else _NOL
        for d in (produk[prod.nama], saluran[sal.nama], total):
            d["qty"] += o.qty
            d["order"] += 1
            d["penjualan"] += jual
            d["potongan"] += potong
            d["hpp"] += Decimal(o.biaya_pokok)
            d["proses"] += proses
    out = HppMarginOut(
        periode=periode, sementara=await _sementara(session, periode),
        per_produk=sorted((_baris_margin(k, v) for k, v in produk.items()), key=lambda b: -b.laba_kotor),
        per_saluran=[_baris_margin(k, v) for k, v in sorted(saluran.items())],
        total=_baris_margin("Total", total), pendapatan_biaya_proses=total["proses"],
    )
    return out, proses_penjual_lain


async def hpp_margin(session: AsyncSession, periode: str) -> HppMarginOut:
    return (await _hpp_margin(session, periode))[0]


# --- Neraca sederhana (9.2) ---

async def _jumlah_kategori(session: AsyncSession, nama: str, jenis: str, sampai: date) -> Decimal:
    v = await session.scalar(
        select(func.coalesce(func.sum(BlTransaksi.jumlah), 0))
        .join(BlKategori, BlKategori.id == BlTransaksi.kategori_id)
        .where(
            BlKategori.nama == nama, BlTransaksi.jenis == jenis, BlTransaksi.dibatalkan.is_(False),
            BlTransaksi.status_kirim == STATUS_TERKIRIM, BlTransaksi.tanggal <= sampai,
        )
    )
    return Decimal(str(v or 0))


async def _piutang_reseller(session: AsyncSession, per: date) -> Decimal:
    dibayar = select(BlPenerimaanResellerItem.order_id).join(
        BlPenerimaanReseller, BlPenerimaanReseller.id == BlPenerimaanResellerItem.penerimaan_id
    ).where(BlPenerimaanReseller.dibatalkan.is_(False), BlPenerimaanReseller.tanggal <= per)
    orders = (
        await session.execute(
            select(BlOrder).join(BlSaluran, BlSaluran.id == BlOrder.saluran_id).where(
                BlSaluran.jenis == "reseller", BlOrder.status != STATUS_BATAL, BlOrder.tgl_dikirim.is_not(None),
                BlOrder.tgl_dikirim <= per, BlOrder.id.not_in(dibayar),
            )
        )
    ).scalars().all()
    return sum((tagihan_order(o) for o in orders), _NOL)


LABEL_JENIS_PEMASOK = {"tukang_kayu": "Tukang", "supplier": "Supplier"}


async def _utang_pemasok_rinci(session: AsyncSession, per: date) -> list[tuple[str, str, Decimal]]:
    """(jenis, nama, utang) per tukang/supplier yang barangnya sudah diambil/diterima dan belum dibayar."""
    dibayar = select(BlPembayaranPemasokItem.order_id).join(
        BlPembayaranPemasok, BlPembayaranPemasok.id == BlPembayaranPemasokItem.pembayaran_id
    ).where(BlPembayaranPemasok.dibatalkan.is_(False), BlPembayaranPemasok.tanggal <= per)
    rows = (
        await session.execute(
            select(BlPemasok.jenis, BlPemasok.nama, func.sum(BlOrder.biaya_pokok)).join(BlPemasok, BlPemasok.id == BlOrder.pemasok_id).where(
                BlOrder.status != STATUS_BATAL, BlOrder.tgl_diambil.is_not(None), BlOrder.tgl_diambil <= per,
                BlOrder.biaya_pokok > 0, BlOrder.id.not_in(dibayar),
            ).group_by(BlPemasok.jenis, BlPemasok.nama)
        )
    ).all()
    return [(jenis, n, Decimal(str(j))) for jenis, n, j in rows]


async def _utang_pemasok(session: AsyncSession, per: date) -> dict[str, Decimal]:
    hasil: dict[str, Decimal] = defaultdict(lambda: _NOL)
    for _jenis, nama, jml in await _utang_pemasok_rinci(session, per):
        hasil[nama] += jml
    return dict(hasil)


def _utang_per_jenis(rinci: list[tuple[str, str, Decimal]]) -> list[BarisNilai]:
    """Utang dikelompokkan Tukang / Supplier (rincian per nama). Harga beli tetap satu angka per order."""
    grup: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: _NOL))
    for jenis, nama, jml in rinci:
        grup[LABEL_JENIS_PEMASOK.get(jenis, jenis)][nama] += jml
    return [
        BarisNilai(label=label, jumlah=sum(d.values(), _NOL), rincian=_urut(d))
        for label in ("Tukang", "Supplier", *sorted(set(grup) - {"Tukang", "Supplier"}))
        if (d := grup.get(label)) and sum(d.values(), _NOL)
    ]


async def _talangan_per_orang(session: AsyncSession, per: date) -> dict[str, Decimal]:
    bayar = (
        select(BlTalanganBayar.talangan_id, func.sum(BlTalanganBayar.jumlah).label("dibayar"))
        .where(BlTalanganBayar.dibatalkan.is_(False), BlTalanganBayar.tanggal <= per)
        .group_by(BlTalanganBayar.talangan_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(BlTalangan.nama, func.sum(BlTalangan.jumlah - func.coalesce(bayar.c.dibayar, 0)))
            .outerjoin(bayar, bayar.c.talangan_id == BlTalangan.id)
            .where(BlTalangan.dibatalkan.is_(False), BlTalangan.tanggal <= per)
            .group_by(BlTalangan.nama)
        )
    ).all()
    return {n: Decimal(str(j)) for n, j in rows if j and Decimal(str(j)) > 0}


async def _dana_gaji_belum_dibayar(session: AsyncSession, per: date) -> Decimal:
    dibayar = await session.scalar(
        select(func.coalesce(func.sum(BlTransaksi.jumlah), 0)).where(
            BlTransaksi.ref_jenis == REF_GAJI, BlTransaksi.jenis == "keluar", BlTransaksi.dibatalkan.is_(False),
            BlTransaksi.status_kirim == STATUS_TERKIRIM, BlTransaksi.tanggal <= per,
        )
    )
    return await beban_provisi(session, AWAL_BUKU, per) - Decimal(str(dibayar or 0))


async def neraca(session: AsyncSession, per_tanggal: date | None = None) -> NeracaOut:
    """Aset - (kewajiban + modal) = selisih. Laba ditahan berbasis kas (sama dengan laba rugi), jadi piutang, belum cair
    dan utang pemasok yang belum masuk laba dicatat di modal sebagai "Laba belum terealisasi" agar pemeriksaan seimbang."""
    per = per_tanggal or _hari_ini()
    akun = (
        await session.execute(select(BlAkunKas).where(BlAkunKas.jenis != JENIS_KEWAJIBAN).order_by(BlAkunKas.created_at))
    ).scalars().all()
    aset_kas, saldo_awal = [], _NOL
    for a in akun:
        s = await saldo_akun(session, a, per)
        saldo_awal += Decimal(a.saldo_awal)
        if a.aktif or s:
            aset_kas.append(BarisNilai(label=a.nama, jumlah=s))
    piutang = await _piutang_reseller(session, per)
    bc = await belum_cair(session, per)
    belum = [BarisNilai(label=g.nama, jumlah=g.total_perkiraan_cair) for g in bc.per_saluran]
    total_aset = sum((b.jumlah for b in aset_kas), _NOL) + piutang + bc.total_perkiraan_cair

    rinci_utang = await _utang_pemasok_rinci(session, per)
    utang: dict[str, Decimal] = defaultdict(lambda: _NOL)
    for _jenis, nama, jml in rinci_utang:
        utang[nama] += jml
    gaji = await _dana_gaji_belum_dibayar(session, per)
    talangan = await _talangan_per_orang(session, per)
    total_kewajiban = sum(utang.values(), _NOL) + gaji + sum(talangan.values(), _NOL)

    r = await ringkasan_laba(session, AWAL_BUKU, per)
    bagi = next((b.jumlah for b in r.di_luar_laba if b.kategori == KATEGORI_BAGI_HASIL), _NOL)
    prive = next((b.jumlah for b in r.di_luar_laba if b.kategori == KATEGORI_PRIVE), _NOL)
    modal = [
        BarisNilai(label="Setoran modal", jumlah=await _jumlah_kategori(session, KATEGORI_SETORAN_MODAL, "masuk", per)),
        BarisNilai(label="Saldo awal akun", jumlah=saldo_awal),
        BarisNilai(label="Laba ditahan", jumlah=r.laba),
        BarisNilai(label="Bagi hasil dibayar", jumlah=-bagi),
        BarisNilai(label="Prive", jumlah=-prive),
        BarisNilai(label="Laba belum terealisasi (piutang + belum cair - utang pemasok)",
                   jumlah=piutang + bc.total_perkiraan_cair - sum(utang.values(), _NOL)),
    ]
    modal = [m for m in modal if m.jumlah or m.label in ("Setoran modal", "Laba ditahan")]
    total_modal = sum((m.jumlah for m in modal), _NOL)
    return NeracaOut(
        per_tanggal=per, aset_kas=aset_kas, piutang_penjual_lain=piutang, belum_cair=belum, total_aset=total_aset,
        utang_pemasok=_urut(utang), utang_per_jenis=_utang_per_jenis(rinci_utang), dana_gaji_belum_dibayar=gaji, talangan=_urut(talangan), total_kewajiban=total_kewajiban,
        modal=modal, total_modal=total_modal, selisih=total_aset - total_kewajiban - total_modal,
    )


# --- Ringkasan Owner (9.7) ---

def _mundur(periode: str, n: int) -> str:
    y, m = map(int, periode.split("-"))
    m -= n
    while m <= 0:
        m += 12
        y -= 1
    return f"{y}-{m:02d}"


async def ringkasan_owner(session: AsyncSession, bulan_tren: int = 6) -> RingkasanOwnerOut:
    hari_ini = _hari_ini()
    tutup = (
        await session.execute(
            select(BlTutupBuku.periode).where(BlTutupBuku.status == STATUS_DITUTUP).order_by(BlTutupBuku.periode.desc()).limit(1)
        )
    ).scalar_one_or_none()
    periode = tutup or f"{hari_ini.year}-{hari_ini.month:02d}"
    lr = await laba_rugi(session, periode)
    iklan = sum((b.jumlah for b in lr.biaya_operasional if b.label == KATEGORI_BIAYA_IKLAN), _NOL)
    tren = []
    for i in range(bulan_tren - 1, -1, -1):
        p = _mundur(periode, i)
        lr_p = lr if p == periode else await laba_rugi(session, p)
        tren.append(TrenBulan(periode=p, penjualan=lr_p.total_penjualan, laba_bersih=lr_p.laba_bersih, sementara=lr_p.sementara))
    nr = await neraca(session, hari_ini)
    nilai = {m.label: m.jumlah for m in nr.modal}
    bagi_hasil = (
        await session.execute(select(BlBagiHasil).where(BlBagiHasil.dibatalkan.is_(False)).order_by(BlBagiHasil.periode.desc()))
    ).scalars().all()
    return RingkasanOwnerOut(
        periode=periode, sementara=lr.sementara, penjualan=lr.total_penjualan, hpp=lr.hpp, biaya_iklan=iklan,
        biaya_operasional_lain=lr.total_biaya_marketplace + lr.total_biaya_operasional - iklan, laba_bersih=lr.laba_bersih,
        tren=tren, total_kas=sum((b.jumlah for b in nr.aset_kas), _NOL),
        piutang=nr.piutang_penjual_lain + sum((b.jumlah for b in nr.belum_cair), _NOL), utang=nr.total_kewajiban,
        modal=nr.total_modal, setoran_modal=nilai.get("Setoran modal", _NOL), laba_ditahan=nilai.get("Laba ditahan", _NOL),
        bagi_hasil_dibayar=-nilai.get("Bagi hasil dibayar", _NOL),
        bagi_hasil=[
            BagiHasilOwner(
                periode=b.periode, laba_bersih=b.laba_bersih, persen_owner=b.persen_owner, bagian_owner=b.bagian_owner,
                dibayar=b.tanggal_bayar is not None, tanggal_bayar=b.tanggal_bayar,
            )
            for b in bagi_hasil
        ],
    )
