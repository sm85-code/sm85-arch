"""Exercise the actual dependencies registered on finance endpoints."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tenants.marketplace_erp.adapters.api.v1 import marketplace_erp_router, workflow_router


@pytest.mark.asyncio
async def test_finance_routes_reject_non_admin_roles():
    routes = [
        route
        for router in (marketplace_erp_router.marketplace_erp_router, workflow_router.router)
        for route in router.routes
        if any(part in getattr(route, 'path', '') for part in ('settlement', 'transaksi-dana', '/iklan'))
    ]
    assert len(routes) >= 10
    for route in routes:
        guards = [dep.call for dep in route.dependant.dependencies if getattr(dep.call, '__name__', '') == '_inner']
        assert len(guards) == 1, route.path
        guard = guards[0]
        assert (await guard(user=SimpleNamespace(role='admin'))).role == 'admin'
        for role in ('owner', 'staff'):
            with pytest.raises(HTTPException) as exc:
                await guard(user=SimpleNamespace(role=role))
            assert exc.value.status_code == 403, (route.path, role)
