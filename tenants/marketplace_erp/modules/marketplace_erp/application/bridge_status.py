"""Shared native status mapping. No marketplace mutation or financial posting.

https://partner.tiktokshop.com/docv2/page/get-order-list-202309
Lazada ready_to_ship/shipped verified from live read evidence on 2026-11-07.
"""
STATES = {
 'tiktokshop': {
  'UNPAID': ('unpaid', 'belum_bayar', 'Belum Bayar'),
  'ON_HOLD': ('hold', 'perlu_diproses', 'Ditahan'),
  'AWAITING_SHIPMENT': ('ready', 'perlu_diproses', 'Perlu Diproses'),
  'PARTIALLY_SHIPPING': ('hold', 'dikirim', 'Dikirim Sebagian'),
  'AWAITING_COLLECTION': ('ready', 'menunggu_kurir', 'Menunggu Penyerahan ke Kurir'),
  'IN_TRANSIT': ('shipped', 'dikirim', 'Dikirim'),
  'DELIVERED': ('completed', 'selesai', 'Diterima Pembeli'),
  'COMPLETED': ('completed', 'selesai', 'Selesai'),
  'CANCELLED': ('cancelled', 'dibatalkan', 'Dibatalkan'),
 },
 'lazada': {
  'pending': ('ready', 'perlu_diproses', 'Perlu Diproses'),
  'ready_to_ship': ('ready', 'menunggu_kurir', 'Menunggu Penyerahan ke Kurir'),
  'shipped': ('shipped', 'dikirim', 'Dikirim'),
  'delivered': ('completed', 'selesai', 'Diterima Pembeli'),
  'canceled': ('cancelled', 'dibatalkan', 'Dibatalkan'),
  'cancelled': ('cancelled', 'dibatalkan', 'Dibatalkan'),
 },
}
STATUS_BY_PHASE = {'belum_bayar':'unpaid', 'perlu_diproses':'to_ship', 'menunggu_kurir':'to_ship',
                   'dikirim':'shipped', 'selesai':'completed', 'dibatalkan':'cancelled'}


def native_state(platform, status):
    return STATES.get(platform, {}).get(status)


def projected_status(source, fallback):
    state = native_state(source.get('platform'), source.get('source_status'))
    return STATUS_BY_PHASE[state[1]] if state else fallback


def phase_expression():
    from sqlalchemy import and_, case, select
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import BridgeOrder, Pesanan
    mapping = [(and_(BridgeOrder.platform == platform, BridgeOrder.source_status == native), values[1])
               for platform, states in STATES.items() for native, values in states.items()]
    return select(case(*mapping, else_=None)).where(BridgeOrder.pesanan_id == Pesanan.id).scalar_subquery()
