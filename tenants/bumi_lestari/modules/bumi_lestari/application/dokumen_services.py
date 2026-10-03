"""Penyusun data Purchase Order (ke tukang/supplier) dan Invoice (ke penjual lain) mingguan."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_dokumen import (
    InvoiceItemOut,
    InvoiceKepadaOut,
    InvoiceOut,
    MingguOut,
    PerusahaanOut,
    PoItemOut,
    PoKepadaOut,
    PurchaseOrderOut,
)
from tenants.bumi_lestari.modules.bumi_lestari.application.services import _hari_ini, get_profil, nama_usaha_pada
from tenants.bumi_lestari.modules.bumi_lestari.application.t3_services import (
    order_sudah_dibayar_reseller,
    selasa_acuan,
    siap_bayar_pemasok,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import (
    BlOrder,
    BlPelanggan,
    BlPemasok,
    BlProduk,
    BlSaluran,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_t3 import (
    BlPembayaranPemasok,
    BlPembayaranPemasokItem,
)

HARI = ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu")
BULAN = (
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
)
ROMAWI = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII")
SYARAT_INVOICE = (
    "Pembayaran dilakukan setelah barang / jasa diterima.",
    "Pembayaran dilakukan paling lambat saat tanggal jatuh tempo melalui transfer ke :",
)


def info_minggu(selasa: date) -> tuple[MingguOut, int, int]:
    """Periode Senin-Sabtu sebelum `selasa`. Minggu ke-N dihitung dari minggu yang memuat tanggal 1 bulan
    dari hari Sabtu periode (21-26 September 2026 = Minggu ke-4 September). Mengembalikan (minggu, no, bulan, tahun)."""
    awal = selasa - timedelta(days=8)
    akhir = selasa - timedelta(days=3)
    pertama = date(akhir.year, akhir.month, 1)
    senin_minggu_1 = pertama - timedelta(days=pertama.weekday())
    nomor_minggu = (awal - senin_minggu_1).days // 7 + 1
    minggu = MingguOut(label=f"Minggu ke-{nomor_minggu} {BULAN[akhir.month - 1]}", periode_awal=awal, periode_akhir=akhir)
    return minggu, nomor_minggu, akhir.month * 10000 + akhir.year


def _nomor(prefix: str, selasa: date, kode: str) -> str:
    _, no_minggu, bulan_tahun = info_minggu(selasa)
    bulan, tahun = divmod(bulan_tahun, 10000)
    return f"{prefix}/MG.{no_minggu}-{kode}/{ROMAWI[bulan - 1]}/{tahun}"


async def _perusahaan(session: AsyncSession, tanggal_dokumen: date) -> PerusahaanOut:
    profil = await get_profil(session)
    return PerusahaanOut(nama=nama_usaha_pada(profil, tanggal_dokumen), alamat=profil.alamat)


# --- Purchase Order -----------------------------------------------------------------------


async def _susun_po(
    session: AsyncSession, selasa: date, per_pemasok: dict[str, list[tuple[BlOrder, BlProduk, Decimal]]]
) -> list[PurchaseOrderOut]:
    minggu, _, _ = info_minggu(selasa)
    perusahaan = await _perusahaan(session, selasa)  # tanggal PO = tanggal pembayaran
    hasil = []
    for pemasok_id, baris in per_pemasok.items():
        pemasok = await session.get(BlPemasok, pemasok_id)
        items = [
            PoItemOut(
                tanggal_selesai=o.tgl_diambil, hari=HARI[o.tgl_diambil.weekday()], kode_pesanan=o.no_order,
                nama_barang=pr.nama, ukuran=pr.ukuran, qty=o.qty,
                harga_barang=(jumlah / o.qty).quantize(Decimal("0.01")), total=jumlah,
                terlambat=o.tgl_diambil < minggu.periode_awal,
            )
            for o, pr, jumlah in sorted(baris, key=lambda b: (b[0].tgl_diambil, b[0].no_order))
        ]
        hasil.append(
            PurchaseOrderOut(
                nomor=_nomor("PO", selasa, pemasok.kode), perusahaan=perusahaan, minggu=minggu, tgl_pembayaran=selasa,
                kepada=PoKepadaOut(
                    pemasok_id=pemasok.id, nama=pemasok.nama, nama_bank=pemasok.nama_bank,
                    no_rekening=pemasok.no_rekening, atas_nama=pemasok.atas_nama,
                ),
                items=items, total_qty=sum(i.qty for i in items), grand_total=sum((i.total for i in items), Decimal("0")),
            )
        )
    return sorted(hasil, key=lambda p: p.kepada.nama)


async def _order_produk(session: AsyncSession, order_id: str) -> tuple[BlOrder, BlProduk]:
    order = await session.get(BlOrder, order_id)
    return order, await session.get(BlProduk, order.produk_id)


async def po_dari_siap(session: AsyncSession, tanggal: date | None = None) -> list[PurchaseOrderOut]:
    """PO (satu per pemasok) dari order yang siap dibayar -- bisa dicetak sebelum pembayaran dicatat."""
    siap = await siap_bayar_pemasok(session, tanggal)
    per_pemasok: dict[str, list] = {}
    for grup in siap.pemasok:
        for item in grup.items:
            order, produk = await _order_produk(session, item.order_id)
            per_pemasok.setdefault(grup.pemasok_id, []).append((order, produk, item.jumlah))
    return await _susun_po(session, siap.selasa, per_pemasok)


async def po_dari_pembayaran(session: AsyncSession, pembayaran_id: str) -> list[PurchaseOrderOut]:
    p = await session.get(BlPembayaranPemasok, pembayaran_id)
    if p is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pembayaran tidak ditemukan")
    rows = (
        await session.execute(select(BlPembayaranPemasokItem).where(BlPembayaranPemasokItem.pembayaran_id == p.id))
    ).scalars()
    per_pemasok: dict[str, list] = {}
    for item in rows:
        order, produk = await _order_produk(session, item.order_id)
        per_pemasok.setdefault(item.pemasok_id, []).append((order, produk, Decimal(item.jumlah)))
    return await _susun_po(session, p.selasa, per_pemasok)


# --- Invoice ke penjual lain ------------------------------------------------------------------


async def invoice_reseller(
    session: AsyncSession, tanggal: date | None = None, pelanggan_id: str | None = None
) -> list[InvoiceOut]:
    """Invoice mingguan per penjual lain: order reseller yang diserahkan (dikirim) s.d. Sabtu sebelum Selasa
    acuan dan belum dibayar. Tgl. invoice = Senin setelah periode, jatuh tempo = Selasa."""
    selasa = selasa_acuan(tanggal or _hari_ini())
    minggu, _, _ = info_minggu(selasa)
    dibayar = await order_sudah_dibayar_reseller(session)
    stmt = (
        select(BlOrder, BlProduk, BlPelanggan)
        .join(BlSaluran, BlSaluran.id == BlOrder.saluran_id)
        .join(BlProduk, BlProduk.id == BlOrder.produk_id)
        .join(BlPelanggan, BlPelanggan.id == BlOrder.pelanggan_id)
        .where(
            BlSaluran.jenis == "reseller",
            BlOrder.status.in_(("dikirim", "selesai")),
            BlOrder.tgl_dikirim.is_not(None),
            BlOrder.tgl_dikirim <= minggu.periode_akhir,
        )
        .order_by(BlPelanggan.nama, BlOrder.tgl_dikirim, BlOrder.no_order)
    )
    if pelanggan_id:
        stmt = stmt.where(BlOrder.pelanggan_id == pelanggan_id)
    per_pelanggan: dict[str, tuple[BlPelanggan, list[InvoiceItemOut]]] = {}
    for order, produk, pelanggan in (await session.execute(stmt)).all():
        if order.id in dibayar:
            continue
        qty = order.qty
        barang = Decimal(order.harga_satuan) * qty
        jasa = (Decimal(order.harga_cat_jasa) + Decimal(order.harga_packing)) * qty
        proses = Decimal(order.biaya_proses)
        per_pelanggan.setdefault(pelanggan.id, (pelanggan, []))[1].append(
            InvoiceItemOut(
                order_id=order.id, tanggal=order.tgl_dikirim, hari=HARI[order.tgl_dikirim.weekday()],
                nama_barang=produk.nama, ukuran=produk.ukuran, qty=qty, harga_barang=barang,
                biaya_jasa_pengecatan=jasa, biaya_proses=proses, total=barang + jasa + proses,
                terlambat=order.tgl_dikirim < minggu.periode_awal,
            )
        )
    profil = await get_profil(session)
    perusahaan = await _perusahaan(session, selasa - timedelta(days=1))  # tanggal invoice
    hasil = []
    for pelanggan, items in per_pelanggan.values():
        total_barang = sum((i.harga_barang for i in items), Decimal("0"))
        total_jasa = sum((i.biaya_jasa_pengecatan for i in items), Decimal("0"))
        total_proses = sum((i.biaya_proses for i in items), Decimal("0"))
        hasil.append(
            InvoiceOut(
                nomor=_nomor("INV", selasa, pelanggan.kode), perusahaan=perusahaan, minggu=minggu,
                tgl_invoice=selasa - timedelta(days=1), jatuh_tempo=selasa,
                kepada=InvoiceKepadaOut(pelanggan_id=pelanggan.id, nama=pelanggan.nama, alamat=pelanggan.alamat),
                items=items, total_barang=total_barang, total_jasa_pengecatan=total_jasa,
                total_biaya_proses=total_proses, grand_total=total_barang + total_jasa + total_proses,
                syarat=list(SYARAT_INVOICE), info_pembayaran=profil.info_pembayaran or None,
            )
        )
    return hasil
