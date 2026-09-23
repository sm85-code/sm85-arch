"""Accounting-period string parsing shared by period-close and the
recording-lock check.

Both BUMDes (Pusat) and unit usaha (UU01..UU06) close monthly, using the
single "YYYY-MM" shape.
"""
from __future__ import annotations

import re
from calendar import monthrange
from datetime import date

_PERIOD_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

UNIT_GROUP_CODES = {"UU01", "UU02", "UU03", "UU04", "UU05", "UU06"}


def period_kind(period: str) -> str:
    """Classify a period string. Only 'month' ("YYYY-MM") is supported;
    raises ValueError otherwise."""
    if _PERIOD_MONTH_RE.match(period or ""):
        return "month"
    raise ValueError("Format period harus YYYY-MM")


def period_range(period: str) -> tuple[date, date]:
    """Return the (start, end) dates covered by a "YYYY-MM" period string."""
    period_kind(period)
    year, month = int(period[:4]), int(period[5:7])
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def assert_period_kind_matches_group(period: str, group_code: str) -> str:
    """Both BUMDES and unit usaha close monthly. Returns the period's kind
    on success ("month"); raises ValueError with a user-facing message
    otherwise."""
    return period_kind(period)
