"""OpenPyXL inbound parse + outbound report generator."""
from __future__ import annotations

import io
from decimal import Decimal
from typing import Any, Optional

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, NamedStyle, PatternFill, Side
from openpyxl.utils import get_column_letter


def parse_excel_rows(
    file_bytes: bytes,
    *,
    sheet_name: Optional[str] = None,
    header_row: int = 1,
) -> tuple[list[str], list[list[Any]]]:
    """Return (headers, data rows) from .xlsx bytes."""
    wb = load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    ws = wb[sheet_name] if sheet_name else wb.active
    rows_iter = ws.iter_rows(values_only=True)
    headers: list[str] = []
    data: list[list[Any]] = []
    for idx, row in enumerate(rows_iter, start=1):
        values = list(row)
        if idx < header_row:
            continue
        if idx == header_row:
            headers = [str(c).strip() if c is not None else "" for c in values]
            continue
        if all(v is None or str(v).strip() == "" for v in values):
            continue
        data.append(list(values))
    wb.close()
    return headers, data


def generate_excel_report(
    headers: list[str],
    rows: list[list[Any]],
    sheet_title: str = "Report",
) -> bytes:
    """Build formatted .xlsx (header style + currency-aware cells)."""
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title[:31] or "Report"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F4E79")
    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    currency_format = '"Rp"#,##0.00'

    for col, title in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=title)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border

    for r_idx, row in enumerate(rows, start=2):
        for c_idx, value in enumerate(row, start=1):
            cell = ws.cell(row=r_idx, column=c_idx, value=value)
            cell.border = border
            if isinstance(value, (int, float, Decimal)) and c_idx > 1:
                cell.number_format = currency_format
                cell.alignment = Alignment(horizontal="right")

    for col in range(1, len(headers) + 1):
        letter = get_column_letter(col)
        max_len = len(str(headers[col - 1])) if headers else 10
        for row in rows:
            if col - 1 < len(row) and row[col - 1] is not None:
                max_len = max(max_len, min(len(str(row[col - 1])), 40))
        ws.column_dimensions[letter].width = max_len + 4

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
