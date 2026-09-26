"""Regression tests for the P0 security audit findings on the madrasah module:

1. POST /api/madrasah/reset-now must require an authenticated admin/kepala
   sekolah -- it used to be callable by anyone and would drop every
   madrasah_* table.
2. POST /api/madrasah/auth/login must throttle repeated failed attempts for
   the same ip+no_hp pair, to slow down brute forcing (e.g. against the
   default seeded password).

No HTTP test client is available in this environment (httpx isn't in
requirements.txt and nothing else in this repo's test suite uses one), so
these exercise the dependency wiring and the throttle helpers directly,
the same way the rest of tests/ drives services/seeder functions directly.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tenants.madrasah.adapters.api.v1.madrasah_router import ADMIN_ROLES, madrasah_router
from tenants.madrasah.modules.madrasah.infrastructure import auth as auth_module
from tenants.madrasah.modules.madrasah.infrastructure.auth import (
    check_login_rate_limit,
    record_failed_login,
    require_roles_madrasah,
    reset_login_attempts,
)


def _fake_request(ip: str = "127.0.0.1") -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host=ip))


@pytest.fixture(autouse=True)
def _clear_throttle_state():
    auth_module._login_attempts.clear()
    yield
    auth_module._login_attempts.clear()


def test_reset_now_route_requires_admin_role_dependency():
    """The /reset-now route must carry a require_roles_madrasah(*ADMIN_ROLES)
    dependency -- this is exactly what was missing before the fix, which let
    anyone drop every madrasah_* table without logging in."""
    reset_route = next(r for r in madrasah_router.routes if r.path == "/reset-now")

    dependant_role_deps = [
        dep for dep in reset_route.dependant.dependencies if getattr(dep.call, "__name__", "") == "_inner"
    ]
    assert dependant_role_deps, "reset-now no longer has a role-based auth dependency"


@pytest.mark.asyncio
async def test_require_roles_madrasah_rejects_wrong_role():
    checker = require_roles_madrasah(*ADMIN_ROLES)
    wrong_role_user = SimpleNamespace(role="wali_santri")
    with pytest.raises(HTTPException) as exc:
        await checker(user=wrong_role_user)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_require_roles_madrasah_accepts_admin_role():
    checker = require_roles_madrasah(*ADMIN_ROLES)
    admin_user = SimpleNamespace(role="admin")
    result = await checker(user=admin_user)
    assert result is admin_user


def test_login_rate_limit_blocks_after_max_attempts():
    request = _fake_request()
    no_hp = "081200000099"

    for _ in range(auth_module.MAX_LOGIN_ATTEMPTS):
        check_login_rate_limit(request, no_hp)
        record_failed_login(request, no_hp)

    with pytest.raises(HTTPException) as exc:
        check_login_rate_limit(request, no_hp)
    assert exc.value.status_code == 429


def test_login_rate_limit_is_scoped_per_ip_and_phone():
    no_hp = "081200000099"
    request_a = _fake_request("10.0.0.1")
    request_b = _fake_request("10.0.0.2")

    for _ in range(auth_module.MAX_LOGIN_ATTEMPTS):
        check_login_rate_limit(request_a, no_hp)
        record_failed_login(request_a, no_hp)

    with pytest.raises(HTTPException):
        check_login_rate_limit(request_a, no_hp)

    # A different source IP against the same phone number is not blocked.
    check_login_rate_limit(request_b, no_hp)


def test_successful_login_resets_attempt_counter():
    request = _fake_request()
    no_hp = "081200000099"

    for _ in range(auth_module.MAX_LOGIN_ATTEMPTS - 1):
        check_login_rate_limit(request, no_hp)
        record_failed_login(request, no_hp)

    reset_login_attempts(request, no_hp)

    # Counter was cleared, so a fresh burst of attempts is allowed again.
    for _ in range(auth_module.MAX_LOGIN_ATTEMPTS):
        check_login_rate_limit(request, no_hp)
