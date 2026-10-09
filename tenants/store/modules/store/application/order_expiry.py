"""Release abandoned reservations that never started an external payment."""
import os
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from . import services
from ..infrastructure.models import PesananStore


async def expire_idle(session):
    try:
        hours = max(1, int(os.getenv("STORE_UNPAID_EXPIRY_HOURS", "24")))
    except ValueError:
        hours = 24
    ids = list((await session.execute(select(PesananStore.id).where(
        PesananStore.status == "menunggu_pembayaran", PesananStore.payment_state == "idle",
        PesananStore.gateway_ref.is_(None), PesananStore.created_at < datetime.now(timezone.utc) - timedelta(hours=hours)
    ).limit(50).with_for_update(skip_locked=True))).scalars())
    for order_id in ids:
        order = await services.get_pesanan(session, order_id, lock=True)
        if order.status == "menunggu_pembayaran" and order.payment_state == "idle" and not order.gateway_ref:
            await services.ubah_status_pesanan(session, order_id, "dibatalkan")
    return len(ids)
