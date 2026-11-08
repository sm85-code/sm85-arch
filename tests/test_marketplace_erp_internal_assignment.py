"""Internal assignments share order scopes without changing legacy data."""

import json
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_marketplace_erp_pesanan_dashboard import session, data  # noqa: F401
from tenants.marketplace_erp.modules.marketplace_erp.application import services, shop_scope
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import (
    akun_ids_diizinkan,
    pastikan_akses_pesanan,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    UserMarketplaceErp,
    TokoPenjualan,
    Pesanan,
    PesananTokoPenjualan,
)


@pytest.mark.asyncio
async def test_mixed_assignments_backfill_scope_and_revoke(session, data):  # noqa: F811
    a, _ = data
    user = UserMarketplaceErp(nama="Staff", username="internalstaff", password_hash="unused", role="staff")
    one = TokoPenjualan(nama="TikTok A", nama_key="tiktok a", jenis="internal", platform="tiktok", kode="TA", aktif=True)
    two = TokoPenjualan(nama="Lazada B", nama_key="lazada b", jenis="internal", platform="lazada", kode="LB", aktif=True)
    session.add_all([user, one, two])
    await session.flush()
    orders = [
        Pesanan(
            platform="manual",
            id_eksternal="EB-" + s.id,
            status="to_ship",
            detail_json=json.dumps({"manual": {"toko_id": s.id}}),
        )
        for s in [one, two]
    ]
    session.add_all(orders)
    await session.flush()
    before = [o.detail_json for o in orders]
    await shop_scope.backfill_links(session)
    await shop_scope.backfill_links(session)
    assert len((await session.scalars(select(PesananTokoPenjualan))).all()) == 2
    assigned = await services.assign_staff_banyak(session, user.id, [a.id, "internal:" + one.id, "internal:" + one.id])
    assert len(assigned) == 2
    assert [r.id for r in await services.assign_staff_banyak(session, user.id, [a.id, "internal:" + one.id])] == [
        r.id for r in assigned
    ]
    allowed = await akun_ids_diizinkan(user, session)
    assert set(allowed) == {a.id, "internal:" + one.id}
    assert {x["id"] for x in await shop_scope.options(session, allowed)} == set(allowed)
    from tenants.marketplace_erp.adapters.api.v1 import marketplace_erp_router as api
    visible = await api.list_pesanan(platform=None, akun_id='internal:' + one.id, status_filter=None, session=session, user=user)
    assert [row.id for row in visible] == [orders[0].id]
    await pastikan_akses_pesanan(user, session, orders[0])
    with pytest.raises(HTTPException) as err:
        await pastikan_akses_pesanan(user, session, orders[1])
    assert err.value.status_code == 403
    with pytest.raises(HTTPException):
        await services.assign_staff_banyak(session, user.id, ["internal:" + two.id, "internal:missing"])
    assert set(await akun_ids_diizinkan(user, session)) == set(allowed)
    await services.remove_staff_akun(session, assigned[1].id)
    assert await akun_ids_diizinkan(user, session) == [a.id]
    with pytest.raises(HTTPException):
        await pastikan_akses_pesanan(user, session, orders[0])
    assert [o.detail_json for o in orders] == before
