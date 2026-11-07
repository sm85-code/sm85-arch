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


DISPLAY_FIELDS = {'payment_method', 'city', 'order_total', 'buyer_paid', 'currency',
                  'recipient_name', 'recipient_phone', 'recipient_address', 'source_status', 'status',
                  'carrier', 'tracking_number', 'buyer_note'}
LINE_FIELDS = {'unit_price', 'line_total', 'image_url'}


def merge_display_snapshot(original, latest):
    if not isinstance(latest, dict) or not all(latest.get(k) == original.get(k) for k in ('platform', 'shop_id', 'order_id')):
        return original
    result = {**original, **{k: latest[k] for k in DISPLAY_FIELDS if k in latest}}
    lines = latest.get('items') or []
    by_id = {line.get('item_id'): line for line in lines}
    if len(by_id) != len(lines):
        return result
    result['items'] = []
    for line in original.get('items') or []:
        fresh = by_id.get(line.get('item_id')) or {}
        same = all(fresh.get(k) == line.get(k) for k in ('product', 'variant', 'quantity'))
        result['items'].append({**line, **({k: fresh[k] for k in LINE_FIELDS if k in fresh} if same else {})})
    return result


def source_status_label(source):
    if source.get('source_status') == 'AWAITING_COLLECTION' and source.get('status') == 'ready':
        return 'Menunggu Penyerahan ke Kurir'
    return {'shipped': 'Dikirim', 'completed': 'Selesai', 'cancelled': 'Dibatalkan',
            'unpaid': 'Belum Bayar', 'hold': 'Ditahan'}.get(source.get('status'),
            'Status EasyBoss: ' + source.get('source_status', 'Belum tersedia'))


def bridge_order_number(order):
    """Original marketplace display number; the internal order key stays unchanged."""
    if order is None or order.platform != 'manual':
        return None
    try:
        detail = json.loads(order.detail_json or '{}')
    except (ValueError, TypeError):
        return None
    source = detail.get('easyboss') if isinstance(detail, dict) else None
    if not isinstance(source, dict) or source.get('platform') not in {'tiktokshop', 'lazada'}:
        return None
    number = source.get('order_id')
    return number if isinstance(number, str) and number else None
