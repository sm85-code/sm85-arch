"""Regression test: LoginRequest accepts short usernames (e.g. "admin"),
not just phone-number-length identifiers. UserMadrasah.no_hp is a plain
unique string column with no phone-format validation anywhere in the
codebase -- the old min_length=8 on LoginRequest was the only thing
blocking a friendly username like "admin" (5 chars) from being used to
log in.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from tenants.madrasah.modules.madrasah.application.schemas import LoginRequest


def test_login_request_accepts_short_username():
    req = LoginRequest(no_hp="admin", password="password123")
    assert req.no_hp == "admin"


def test_login_request_still_rejects_too_short():
    with pytest.raises(ValidationError):
        LoginRequest(no_hp="ab", password="password123")


def test_login_request_still_accepts_phone_numbers():
    req = LoginRequest(no_hp="081200000001", password="password123")
    assert req.no_hp == "081200000001"
