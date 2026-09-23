"""shared.period: BUMDES (Pusat) closes per triwulan/tahun, unit usaha
(UU01..UU06) close monthly. Deliberately DB-independent (no import of
modules.identity/siabumdes models, which require DATABASE_URL) so it runs
in CI same as everything else in this file's suite."""
from __future__ import annotations

from datetime import date

import pytest

from shared.period import (
    assert_period_kind_matches_group,
    period_contains,
    period_kind,
    period_range,
)


def test_period_kind_classifies_the_three_shapes():
    assert period_kind("2026") == "year"
    assert period_kind("2026-Q1") == "quarter"
    assert period_kind("2026-Q4") == "quarter"
    assert period_kind("2026-09") == "month"


def test_period_kind_rejects_garbage():
    with pytest.raises(ValueError):
        period_kind("2026-Q5")
    with pytest.raises(ValueError):
        period_kind("not-a-period")
    with pytest.raises(ValueError):
        period_kind("")


def test_period_range_year():
    assert period_range("2026") == (date(2026, 1, 1), date(2026, 12, 31))


def test_period_range_quarter():
    assert period_range("2026-Q1") == (date(2026, 1, 1), date(2026, 3, 31))
    assert period_range("2026-Q4") == (date(2026, 10, 1), date(2026, 12, 31))


def test_period_range_month_handles_leap_february():
    assert period_range("2024-02") == (date(2024, 2, 1), date(2024, 2, 29))


def test_period_contains():
    assert period_contains("2026-Q1", date(2026, 2, 15)) is True
    assert period_contains("2026-Q1", date(2026, 4, 1)) is False
    assert period_contains("2026", date(2026, 11, 1)) is True
    assert period_contains("2026-09", date(2026, 9, 30)) is True
    assert period_contains("garbage", date(2026, 1, 1)) is False


def test_bumdes_must_close_quarterly_or_yearly_not_monthly():
    assert assert_period_kind_matches_group("2026-Q1", "BUMDES") == "quarter"
    assert assert_period_kind_matches_group("2026", "BUMDES") == "year"
    with pytest.raises(ValueError, match="triwulan"):
        assert_period_kind_matches_group("2026-09", "BUMDES")


def test_unit_usaha_must_close_monthly_not_quarterly_or_yearly():
    assert assert_period_kind_matches_group("2026-09", "UU01") == "month"
    with pytest.raises(ValueError, match="bulanan"):
        assert_period_kind_matches_group("2026-Q1", "UU01")
    with pytest.raises(ValueError, match="bulanan"):
        assert_period_kind_matches_group("2026", "UU05")
