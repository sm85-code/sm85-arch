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
from adapters.external.word_generator import generate_word_report

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


def test_generate_word_report_is_a_valid_docx_with_letterhead_and_table():
    blob = generate_word_report(title="Laba Rugi", subtitle="Jan 2024", table_headers=SAMPLE_HEADERS, table_rows=SAMPLE_ROWS)
    assert blob.startswith(b"PK")  # docx is a zip archive

    from docx import Document
    import io

    doc = Document(io.BytesIO(blob))
    body_text = "\n".join(p.text for p in doc.paragraphs)
    assert "LABA RUGI" in body_text
    assert "Jan 2024" in body_text
    assert len(doc.tables) == 2  # data table + signature table
    header_cells = [c.text for c in doc.tables[0].rows[0].cells]
    assert header_cells == SAMPLE_HEADERS


@pytest.mark.asyncio
async def test_report_router_pdf_excel_word_helpers_run_off_the_event_loop():
    """adapters.api.v1.reports_router._pdf/_xlsx/_docx wrap the blocking
    generate_*_report calls in starlette's run_in_threadpool. This smoke
    test exercises them the same way the report_pdf/report_excel/report_word
    route handlers do, confirming they still produce valid output once
    awaited from a thread pool instead of being called directly."""
    from adapters.api.v1.reports_router import _docx, _pdf, _xlsx

    pdf_response = await _pdf("Laba Rugi", SAMPLE_HEADERS, SAMPLE_ROWS, "Jan 2024")
    assert pdf_response.media_type == "application/pdf"
    assert bytes(pdf_response.body).startswith(b"%PDF")

    xlsx_response = await _xlsx("Laba Rugi", SAMPLE_HEADERS, SAMPLE_ROWS, "Jan 2024")
    assert xlsx_response.media_type == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert bytes(xlsx_response.body).startswith(b"PK")

    docx_response = await _docx("Laba Rugi", SAMPLE_HEADERS, SAMPLE_ROWS, "Jan 2024")
    assert docx_response.media_type == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert bytes(docx_response.body).startswith(b"PK")
