"""Pembuat PDF Purchase Order & Invoice (A4 landscape), meniru contoh dokumen asli."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_dokumen import InvoiceOut, PurchaseOrderOut

LOGO = Path(__file__).resolve().parents[3] / "assets" / "logo.png"
HARI = ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu")
TEKS = colors.HexColor("#1F2937")
HEADER_KIRI = colors.HexColor("#DFE3EC")
HEADER_KANAN = colors.HexColor("#8396B4")
BARIS = colors.HexColor("#F3F4F7")


def rp(nilai: Decimal | int) -> str:
    return "Rp" + f"{Decimal(nilai):,.0f}".replace(",", ".")


def tgl(d: date) -> str:
    return f"{d:%d/%m/%Y}"


def tgl_hari(d: date) -> str:
    return f"{d:%d/%m/%Y} - {HARI[d.weekday()]}"


_normal = ParagraphStyle("n", fontName="Helvetica", fontSize=9, leading=12, textColor=TEKS)
_bold = ParagraphStyle("b", parent=_normal, fontName="Helvetica-Bold")
_nama = ParagraphStyle("nama", parent=_bold, fontSize=12, leading=15)
_judul = ParagraphStyle("j", parent=_bold, fontSize=15, leading=19, alignment=1)
_tengah = ParagraphStyle("t", parent=_normal, alignment=1)
_tengah_bold = ParagraphStyle("tb", parent=_bold, alignment=1)
_kanan = ParagraphStyle("k", parent=_normal, alignment=2)
_th = ParagraphStyle("th", parent=_bold, fontName="Helvetica-BoldOblique", alignment=1)
_th_putih = ParagraphStyle("thp", parent=_th, textColor=colors.white)


def _kepala(judul: str, perusahaan, minggu, kiri_baris: list[tuple[str, str]], kanan: list[Paragraph]) -> list:
    logo = Image(str(LOGO), width=24 * mm, height=24 * mm, mask="auto") if LOGO.exists() else Spacer(24 * mm, 24 * mm)
    alamat = [Paragraph(baris, _normal) for baris in perusahaan.alamat.split(", Kec.")]
    if len(alamat) > 1:
        alamat[1] = Paragraph("Kec." + perusahaan.alamat.split(", Kec.")[1], _normal)
    identitas = [Paragraph(perusahaan.nama.upper(), _nama), *alamat]
    judul_box = Table([[Paragraph(judul, _judul)]], colWidths=[75 * mm], rowHeights=[11 * mm])
    judul_box.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, TEKS), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    periode = [
        judul_box,
        Paragraph(f"Periode : {tgl(minggu.periode_awal)}  s.d.  {tgl(minggu.periode_akhir)}", _tengah),
        Paragraph(minggu.label, _tengah_bold),
    ]
    atas = Table([[logo, identitas, periode]], colWidths=[28 * mm, 150 * mm, 80 * mm])
    atas.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (0, 0), 0)]))
    info = Table(
        [[Paragraph(k, _normal), Paragraph(f": {v}", _normal)] for k, v in kiri_baris],
        colWidths=[32 * mm, 70 * mm],
    )
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (0, -1), 0)]))
    bawah = Table([[info, kanan]], colWidths=[180 * mm, 78 * mm])
    bawah.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (0, 0), 0)]))
    return [atas, Spacer(1, 6 * mm), bawah, Spacer(1, 6 * mm)]


def _tabel(header: list[str], baris: list[list[str]], total: list[str], kiri: int, lebar: list[float]) -> Table:
    """`kiri` = jumlah kolom awal berwarna abu muda; sisanya berwarna biru-abu (angka)."""
    data = [[Paragraph(h, _th if i < kiri else _th_putih) for i, h in enumerate(header)]]
    data += [[Paragraph(c, _kanan if _angka(c) else _normal) for c in r] for r in baris]
    data.append([Paragraph(c, _bold if not _angka(c) else ParagraphStyle("tk", parent=_bold, alignment=2)) for c in total])
    tabel = Table(data, colWidths=[w * mm for w in lebar], repeatRows=1)
    gaya = [
        ("BACKGROUND", (0, 0), (kiri - 1, 0), HEADER_KIRI),
        ("BACKGROUND", (kiri, 0), (-1, 0), HEADER_KANAN),
        ("LINEBELOW", (0, 0), (-1, 0), 1.2, colors.HexColor("#6B7A99")),
        ("BACKGROUND", (0, 1), (-1, -2), BARIS),
        ("BACKGROUND", (0, -1), (-1, -1), HEADER_KIRI),
        ("LINEABOVE", (0, -1), (-1, -1), 1.2, TEKS),
        ("LINEBELOW", (0, -1), (-1, -1), 1.2, TEKS),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    tabel.setStyle(TableStyle(gaya))
    return tabel


def _angka(teks: str) -> bool:
    return teks.startswith("Rp") or teks.isdigit()


def _bangun(story: list, judul_dok: str) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4), leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
        title=judul_dok, author="Bumi Lestari",
    )
    doc.build(story)
    return buf.getvalue()


def render_invoice(inv: InvoiceOut) -> bytes:
    kanan = [Paragraph("Kepada Yth.", _normal), Paragraph(inv.kepada.nama, _bold)]
    if inv.kepada.alamat:
        kanan.append(Paragraph(inv.kepada.alamat, _normal))
    story = _kepala(
        "INVOICE", inv.perusahaan, inv.minggu,
        [("Nomor Invoice", inv.nomor), ("Tgl. Invoice", tgl(inv.tgl_invoice)), ("Tgl. Jatuh Tempo", tgl(inv.jatuh_tempo))],
        kanan,
    )
    story.append(Paragraph("Deskripsi Barang / Jasa", _bold))
    story.append(Spacer(1, 2 * mm))
    baris = [
        [tgl_hari(i.tanggal), i.nama_barang, i.ukuran, rp(i.harga_barang), rp(i.biaya_jasa_pengecatan), rp(i.biaya_proses), rp(i.total)]
        for i in inv.items
    ]
    total = ["Grand Total", "", "", rp(inv.total_barang), rp(inv.total_jasa_pengecatan), rp(inv.total_biaya_proses), rp(inv.grand_total)]
    story.append(
        _tabel(
            ["Tanggal", "Nama Barang", "Ukuran", "Harga Barang", "Biaya Jasa<br/>Pengecatan", "Biaya Proses<br/>Pesanan", "Total"],
            baris, total, 3, [42, 80, 28, 30, 30, 30, 30],
        )
    )
    story.append(Spacer(1, 12 * mm))
    story.append(Paragraph("Informasi Pembayaran :", _bold))
    story.append(Spacer(1, 1.5 * mm))
    for no, teks in enumerate(inv.syarat, 1):
        tambahan = f" <b>{inv.info_pembayaran}</b>" if no == len(inv.syarat) and inv.info_pembayaran else ""
        story.append(Paragraph(f"{no}. {teks}{tambahan}", _normal))
    return _bangun(story, inv.nomor)


def render_po(po: PurchaseOrderOut) -> bytes:
    k = po.kepada
    kanan = [Paragraph("Kepada Yth.", _normal), Paragraph(f"Sdr. {k.nama}", _bold)]
    if k.no_rekening:
        atas_nama = f" a.n. {k.atas_nama}" if k.atas_nama else ""
        kanan.append(Paragraph(f"No. Rekening : {k.no_rekening}{atas_nama}", _normal))
    if k.nama_bank:
        kanan.append(Paragraph(k.nama_bank, _normal))
    story = _kepala(
        "PURCHASE ORDER", po.perusahaan, po.minggu,
        [
            ("Nomor", po.nomor),
            ("Tgl. Order", f"{tgl(po.minggu.periode_awal)}  s.d.  {tgl(po.minggu.periode_akhir)}"),
            ("Tgl. Pembayaran", tgl(po.tgl_pembayaran)),
        ],
        kanan,
    )
    story.append(Paragraph("Deskripsi Barang", _bold))
    story.append(Spacer(1, 2 * mm))
    baris = [
        [tgl_hari(i.tanggal_selesai), i.kode_pesanan, i.nama_barang, i.ukuran, str(i.qty), rp(i.harga_barang), rp(i.total)]
        for i in po.items
    ]
    total = ["Grand Total", "", "", "", str(po.total_qty), rp(po.grand_total), rp(po.grand_total)]
    story.append(
        _tabel(
            ["Tanggal Selesai", "Kode Pesanan", "Nama Barang", "Ukuran", "Qty", "Harga Barang", "Total"],
            baris, total, 4, [42, 42, 66, 30, 14, 32, 32],
        )
    )
    return _bangun(story, po.nomor)
