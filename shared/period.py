"""Accounting-period string parsing shared by period-close and the
recording-lock check.

BUMDes (Pusat) closes per triwulan/tahun; unit usaha (UU01..UU06) close
monthly -- so a "period" string can be one of three shapes:
  - "YYYY-MM"   month   (unit usaha)
  - "YYYY-Q1".."YYYY-Q4"  quarter (BUMDES)
  - "YYYY"      year    (BUMDES)
"""
from __future__ import annotations

import re
from calendar import monthrange
from datetime import date

_PERIOD_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_PERIOD_QUARTER_RE = re.compile(r"^\d{4}-Q[1-4]$")
_PERIOD_YEAR_RE = re.compile(r"^\d{4}$")

UNIT_GROUP_CODES = {"UU01", "UU02", "UU03", "UU04", "UU05", "UU06"}


def period_kind(period: str) -> str:
    """Classify a period string as 'month', 'quarter', or 'year'.
    Raises ValueError if it matches none of those shapes."""
    if _PERIOD_YEAR_RE.match(period or ""):
        return "year"
    if _PERIOD_QUARTER_RE.match(period or ""):
        return "quarter"
    if _PERIOD_MONTH_RE.match(period or ""):
        return "month"
    raise ValueError(
        "Format period harus YYYY-MM (bulanan), YYYY-Q1..Q4 (triwulan), atau YYYY (tahunan)"
    )


def period_range(period: str) -> tuple[date, date]:
    """Return the (start, end) dates covered by a period string."""
    kind = period_kind(period)
    if kind == "year":
        year = int(period)
        return date(year, 1, 1), date(year, 12, 31)
    if kind == "quarter":
        year, quarter = int(period[:4]), int(period[-1])
        start_month = (quarter - 1) * 3 + 1
        end_month = start_month + 2
        return date(year, start_month, 1), date(year, end_month, monthrange(year, end_month)[1])
    year, month = int(period[:4]), int(period[5:7])
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def period_contains(period: str, day: date) -> bool:
    """True if `day` falls inside the date range represented by `period`,
    whichever of the three shapes it is. Used to check whether a
    transaction date falls inside an already-closed period, since BUMDES
    closes are quarter/year strings, not just YYYY-MM."""
    try:
        start, end = period_range(period)
    except ValueError:
        return False
    return start <= day <= end


def assert_period_kind_matches_group(period: str, group_code: str) -> str:
    """Enforce BUMDES = quarter/year, unit usaha = month. Returns the
    period's kind on success; raises ValueError with a user-facing message
    otherwise."""
    kind = period_kind(period)
    if group_code == "BUMDES":
        if kind == "month":
            raise ValueError(
                "BUMDES (Pusat) hanya bisa ditutup per triwulan (YYYY-Q1..Q4) atau tahunan (YYYY), bukan bulanan"
            )
    elif kind != "month":
        raise ValueError(f"Unit usaha ({group_code}) hanya bisa ditutup bulanan (YYYY-MM)")
    return kind
