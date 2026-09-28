"""Hardening: ensure_schema _WIDEN/_ADD_COLUMNS stay coherent without a live DB.

Prod boots via ensure_schema(); Alembic is the reviewable history. These checks
guard against typos / duplicate column entries that would make boot-time DDL
noisily fail (and skip later steps) even though _safe_exec isolates each step.
"""
from __future__ import annotations

import os

# Importing schema pulls shared.database, which requires DATABASE_URL at import.
# CI may inject a real URL; for local/unit collection provide a harmless placeholder
# (no connection is opened by these list-shape tests).
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@127.0.0.1:5432/siabumdes_test")

from modules.siabumdes.schema import _ADD_COLUMNS, _WIDEN


def test_widen_entries_are_unique_and_shaped():
    seen: set[tuple[str, str]] = set()
    for table, column, new_type in _WIDEN:
        assert table and column and new_type, (table, column, new_type)
        assert new_type.upper().startswith("VARCHAR"), new_type
        key = (table, column)
        assert key not in seen, f"duplicate _WIDEN entry: {key}"
        seen.add(key)


def test_add_columns_unique_and_shaped():
    seen: set[tuple[str, str]] = set()
    for table, column, col_def in _ADD_COLUMNS:
        assert table and column and col_def, (table, column, col_def)
        key = (table, column)
        assert key not in seen, f"duplicate _ADD_COLUMNS entry: {key}"
        seen.add(key)


def test_add_columns_cover_known_bootstrap_fields():
    """Columns that exist in recent alembic revisions must also be boot-ensured."""
    keys = {(t, c) for t, c, _ in _ADD_COLUMNS}
    required = {
        ("org_profiles", "share_pengurus"),
        ("org_profiles", "share_pades"),
        ("users", "photo_url"),
        ("stock_cards", "movement_kind"),
        ("unit_usaha", "business_type"),
    }
    missing = required - keys
    assert not missing, f"_ADD_COLUMNS missing boot-critical columns: {missing}"


def test_widen_covers_identity_varchar_hotspots():
    keys = {(t, c) for t, c, _ in _WIDEN}
    required = {
        ("users", "password_hash"),
        ("users", "id"),
        ("accounts", "code"),
    }
    missing = required - keys
    assert not missing, f"_WIDEN missing expected identity/account columns: {missing}"
