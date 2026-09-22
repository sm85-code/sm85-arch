"""Shared helpers for the PDF/Excel/Word report renderers.

Excel keeps real numeric cells with a number_format (so it stays sortable),
but PDF and Word only ever render text -- so money columns there must be
pre-formatted into Rupiah strings, matching the frontend's fmtRp().
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any


def is_money_header(h: str) -> bool:
    h = (h or "").lower()
    return any(
        k in h
        for k in (
            "nominal", "jumlah", "debit", "kredit", "saldo", "rp",
            "amount", "nilai", "laba", "beban", "pendapatan",
        )
    )


def row_is_total(row: list[Any]) -> bool:
    joined = " ".join(str(c or "").lower() for c in row)
    return any(k in joined for k in ("total", "jumlah", "laba bersih", "saldo akhir", "saldo awal"))


def format_money(value: Any) -> str:
    """Format a numeric amount as Rupiah text, e.g. -> 'Rp 1.000.000'.

    Non-numeric values (None, empty string, already-formatted text) pass
    through unchanged so section labels sharing a money column still render.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        stripped = value.strip()
        try:
            n = float(stripped)
        except ValueError:
            return value
    elif isinstance(value, (int, float, Decimal)):
        n = float(value)
    else:
        return str(value)
    sign = "-" if n < 0 else ""
    return f"{sign}Rp {abs(round(n)):,}".replace(",", ".")


def format_money_row(headers: list[str], row: list[Any]) -> list[Any]:
    """Return a copy of `row` with money columns formatted as Rupiah text."""
    money_cols = {i for i, h in enumerate(headers) if is_money_header(h)}
    return [format_money(c) if i in money_cols else c for i, c in enumerate(row)]
