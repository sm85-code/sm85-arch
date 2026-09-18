"""PDF document generator (kwitansi, invoice, statements)."""
from __future__ import annotations

import io
from typing import Any, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "DocTitle", parent=base["Heading1"], alignment=TA_CENTER, fontSize=14, spaceAfter=8
        ),
        "subtitle": ParagraphStyle(
            "DocSub", parent=base["Normal"], alignment=TA_CENTER, fontSize=10, textColor=colors.grey
        ),
        "body": ParagraphStyle("DocBody", parent=base["Normal"], fontSize=10, leading=14),
        "right": ParagraphStyle("DocRight", parent=base["Normal"], alignment=TA_RIGHT, fontSize=10),
        "left": ParagraphStyle("DocLeft", parent=base["Normal"], alignment=TA_LEFT, fontSize=10),
    }


def generate_pdf_report(
    *,
    title: str,
    subtitle: str = "",
    lines: Optional[list[str]] = None,
    table_headers: Optional[list[str]] = None,
    table_rows: Optional[list[list[Any]]] = None,
    footer: str = "",
) -> bytes:
    """Generic structured PDF (balance sheet / statement style)."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=1.5 * cm, rightMargin=1.5 * cm,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm,
    )
    s = _styles()
    story: list[Any] = [Paragraph(title, s["title"])]
    if subtitle:
        story.append(Paragraph(subtitle, s["subtitle"]))
    story.append(Spacer(1, 8 * mm))

    for line in lines or []:
        story.append(Paragraph(line, s["body"]))
        story.append(Spacer(1, 2 * mm))

    if table_headers and table_rows is not None:
        data = [table_headers] + [[str(c) if c is not None else "" for c in r] for r in table_rows]
        tbl = Table(data, repeatRows=1)
        tbl.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E79")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F7FA")]),
                    ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        story.append(tbl)

    if footer:
        story.append(Spacer(1, 10 * mm))
        story.append(Paragraph(footer, s["subtitle"]))

    doc.build(story)
    return buf.getvalue()


def generate_kwitansi_pdf(
    *,
    no: str,
    tanggal: str,
    diterima_dari: str,
    jumlah: str,
    untuk: str,
    penerima: str = "",
) -> bytes:
    """Receipt (kwitansi) PDF."""
    lines = [
        f"No: <b>{no}</b>",
        f"Tanggal: {tanggal}",
        f"Telah diterima dari: <b>{diterima_dari}</b>",
        f"Uang sejumlah: <b>{jumlah}</b>",
        f"Untuk pembayaran: {untuk}",
    ]
    if penerima:
        lines.append(f"Penerima: {penerima}")
    return generate_pdf_report(title="KWITANSI", lines=lines)


def generate_invoice_pdf(
    *,
    invoice_no: str,
    date: str,
    customer: str,
    items: list[list[Any]],
    total: str,
) -> bytes:
    """Simple invoice PDF. items = [[desc, qty, price, subtotal], ...]."""
    return generate_pdf_report(
        title="INVOICE",
        subtitle=f"No. {invoice_no} | {date} | {customer}",
        table_headers=["Uraian", "Qty", "Harga", "Subtotal"],
        table_rows=items,
        footer=f"Total: {total}",
    )
