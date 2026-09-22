"""format_money() is what fixes 'nominal tidak terformat' in PDF/Word exports
(Excel already formats real numeric cells via its own number_format)."""
from __future__ import annotations

from decimal import Decimal

from adapters.external.report_formatting import format_money


def test_format_money_formats_float_as_indonesian_rupiah():
    assert format_money(1000000.0) == "Rp 1.000.000"


def test_format_money_rounds_and_uses_thousands_separator():
    assert format_money(1234567.89) == "Rp 1.234.568"


def test_format_money_handles_decimal_and_negative():
    assert format_money(Decimal("50000.00")) == "Rp 50.000"
    assert format_money(-2500) == "-Rp 2.500"


def test_format_money_passes_through_non_numeric_and_empty():
    assert format_money(None) == ""
    assert format_money("") == ""
    assert format_money("Total Pendapatan") == "Total Pendapatan"
