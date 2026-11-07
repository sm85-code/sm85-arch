"""Read-only bridge display projection; never changes Pesanan or its identifiers."""
import json
from sqlalchemy import select
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import BridgeOrder


async def enrich_orders(session, orders):
    by_id = {p.id: p for p in orders if p.platform == 'manual'}
    if not by_id:
        return
    rows = (await session.scalars(select(BridgeOrder).where(BridgeOrder.pesanan_id.in_(by_id)))).all()
    for row in rows:
        try:
            snapshot = json.loads(row.snapshot_json)
        except (ValueError, TypeError):
            continue
        if isinstance(snapshot, dict):
            by_id[row.pesanan_id]._bridge_snapshot = snapshot
