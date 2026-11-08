"""Source boundaries for the two inboxes; operational order IDs remain shared."""
from fastapi import HTTPException
from sqlalchemy import exists, select
from ..infrastructure.models import BridgeOrder, Pesanan, TokoPenjualan


def easyboss_condition():
    return exists().where(BridgeOrder.pesanan_id == Pesanan.id,
                          BridgeOrder.platform.in_(["lazada", "tiktokshop"]))


def source_condition(source):
    if source == "easyboss":
        return easyboss_condition()
    if source == "shopee":
        return (Pesanan.platform == "shopee") & ~easyboss_condition()
    raise HTTPException(422, "Sumber pesanan tidak valid")


async def page_for_order(session, order):
    bridge = await session.scalar(select(BridgeOrder.id).where(
        BridgeOrder.pesanan_id == order.id, BridgeOrder.platform.in_(["lazada", "tiktokshop"])))
    return "pesanan_easyboss" if bridge else "pesanan"


async def filter_shops(session, shops, source):
    if source == "shopee":
        return [s for s in shops if s['jenis'] == 'marketplace' and s['platform'] == 'shopee']
    ids = set((await session.scalars(select(TokoPenjualan.id).where(
        TokoPenjualan.sumber == 'easyboss', TokoPenjualan.platform.in_(['lazada', 'tiktokshop'])))).all())
    return [s for s in shops if s['id'].startswith('internal:') and s['id'][9:] in ids]
