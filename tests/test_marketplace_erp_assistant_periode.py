from datetime import date

import pytest

from tenants.marketplace_erp.modules.marketplace_erp.application.assistant import tools

TODAY = date(2026, 10, 24)  # Sabtu


@pytest.mark.parametrize("name,expected", [
    ("hari ini", (date(2026, 10, 24), date(2026, 10, 24))),
    ("kemarin", (date(2026, 10, 23), date(2026, 10, 23))),
    ("7 hari terakhir", (date(2026, 10, 18), date(2026, 10, 24))),
    ("7 hari sebelumnya", (date(2026, 10, 11), date(2026, 10, 17))),
    ("30 hari terakhir", (date(2026, 9, 25), date(2026, 10, 24))),
    ("minggu ini", (date(2026, 10, 19), date(2026, 10, 24))),
    ("minggu lalu", (date(2026, 10, 12), date(2026, 10, 18))),
    ("bulan ini", (date(2026, 10, 1), date(2026, 10, 24))),
    ("bulan lalu", (date(2026, 9, 1), date(2026, 9, 30))),
])
def test_named_period(name, expected):
    assert tools.named_period(name, TODAY) == expected


def test_summary_period_requires_dates_or_name():
    assert tools.SummaryPeriod.model_validate({"periode": "kemarin"}).mulai is not None
    assert tools.SummaryPeriod.model_validate({"mulai": "2026-10-01", "selesai": "2026-10-02"}).selesai == date(2026, 10, 2)
    with pytest.raises(ValueError):
        tools.SummaryPeriod.model_validate({})
