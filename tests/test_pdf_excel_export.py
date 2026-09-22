"""Regresi untuk export PDF/Excel laporan keuangan.

Latar belakang: weasyprint==62.3 mendeklarasikan dependency "pydyf>=0.10.0",
tapi Stream.transform() di weasyprint memanggil super().transform(...), API
yang sudah dihapus di pydyf 0.12. Tanpa pin versi pydyf, pip menginstal
0.12.x terbaru dan setiap render PDF diam-diam gagal lalu jatuh ke fallback
ReportLab (lihat generate_pdf_report -> html_pdf.render_pdf_from_html yang
menelan exception-nya jadi cuma warning log). requirements.txt sekarang
menambahkan pydyf==0.11.0. Test ini memastikan jalur WeasyPrint benar-benar
jalan (bukan diam-diam fallback) di lingkungan test/CI.
"""
from __future__ import annotations

import pytest

from adapters.external.excel_adapter import generate_excel_report
from adapters.external.html_pdf import render_pdf_from_html, build_report_html
from adapters.external.pdf_generator import generate_pdf_report_platypus

SAMPLE_HEADERS = ["Kode", "Nama", "Nominal"]
SAMPLE_ROWS = [
    ["4.1.01", "Pendapatan Jasa", 15000000],
    ["Total Pendapatan", "", 15000000],
]


def test_weasyprint_pdf_path_does_not_silently_fall_back():
    """Guards against the pydyf-version regression: render_pdf_from_html
    must return real bytes, not None (which would mean it swallowed an
    exception and the caller fell back to the ReportLab renderer)."""
    html_doc = build_report_html(title="Laba Rugi", table_headers=SAMPLE_HEADERS, table_rows=SAMPLE_ROWS)
    blob = render_pdf_from_html(html_doc)
    assert blob is not None, "WeasyPrint gagal render -- cek kecocokan versi pydyf di requirements.txt"
    assert blob.startswith(b"%PDF")


def test_reportlab_fallback_pdf_still_works_standalone():
    blob = generate_pdf_report_platypus(title="Laba Rugi", table_headers=SAMPLE_HEADERS, table_rows=SAMPLE_ROWS)
    assert blob.startswith(b"%PDF")


def test_generate_excel_report_has_letterhead_headers_and_currency_format():
    blob = generate_excel_report(SAMPLE_HEADERS, SAMPLE_ROWS, "Laba Rugi")
    from openpyxl import load_workbook
    import io

    wb = load_workbook(io.BytesIO(blob))
    ws = wb.active
    header_row_values = [c.value for c in ws[8]]
    assert header_row_values == SAMPLE_HEADERS
    nominal_cell = ws.cell(row=9, column=3)
    assert nominal_cell.value == 15000000
    assert "Rp" in nominal_cell.number_format
