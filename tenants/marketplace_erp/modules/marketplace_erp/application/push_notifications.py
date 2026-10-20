"""Web Push for new orders to process (and cancel requests) and new buyer chats.

A round runs every PUSH_INTERVAL_DETIK (default 60) and only when VAPID keys are set and someone subscribed. Orders come
from the database (the background order sync keeps it fresh); chats are read from Shopee, only for shops that have a
subscriber who wants chat. Everything already waiting when a source is seen the first time is recorded without a push,
so switching this on never floods anyone with the backlog.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import services, webpush
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee_chat
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    AkunMarketplace,
    Pesanan,
    PushSubscription,
    PushTerkirim,
    StaffAkunMarketplace,
    UserMarketplaceErp,
)

logger = logging.getLogger(__name__)
DEFAULT_INTERVAL = 60.0
KEEP_DAYS = 30


def interval_seconds() -> float:
    """PUSH_INTERVAL_DETIK (at least 30); 0 for off."""
    try:
        value = float(os.getenv("PUSH_INTERVAL_DETIK", str(DEFAULT_INTERVAL)))
    except ValueError:
        return 0.0
    return 0.0 if value <= 0 else max(value, 30.0)


def _insert(session):
    return pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert


async def _remember(session: AsyncSession, keys: list[str]) -> None:
    for i in range(0, len(keys), 200):
        chunk = keys[i : i + 200]
        await session.execute(_insert(session)(PushTerkirim).values([{"key": k, "created_at": datetime.now(timezone.utc)} for k in chunk]).on_conflict_do_nothing(index_elements=["key"]))
    await session.commit()


async def _unseen(session: AsyncSession, keys: list[str]) -> list[str]:
    seen: set[str] = set()
    for i in range(0, len(keys), 500):
        seen.update((await session.scalars(select(PushTerkirim.key).where(PushTerkirim.key.in_(keys[i : i + 500])))).all())
    return [k for k in keys if k not in seen]


async def _recipients(session: AsyncSession) -> list[tuple[PushSubscription, set[str] | None]]:
    """Every subscription with the shops its user may see; None = every shop (admin and owner)."""
    rows = (await session.execute(select(PushSubscription, UserMarketplaceErp.role).join(UserMarketplaceErp, UserMarketplaceErp.id == PushSubscription.user_id))).all()
    staff_ids = [sub.user_id for sub, role in rows if (role or "").strip().lower() == "staff"]
    shops: dict[str, set[str]] = {}
    if staff_ids:
        for user_id, akun_id in (await session.execute(select(StaffAkunMarketplace.user_id, StaffAkunMarketplace.akun_id).where(StaffAkunMarketplace.user_id.in_(staff_ids)))).all():
            shops.setdefault(user_id, set()).add(akun_id)
    out = []
    for sub, role in rows:
        role = (role or "").strip().lower()
        out.append((sub, shops.get(sub.user_id, set()) if role == "staff" else None if role in ("admin", "owner") else set()))
    return out


def _sees(allowed: set[str] | None, akun_id: str) -> bool:
    return allowed is None or akun_id in allowed


def _subscription(sub: PushSubscription) -> dict:
    return {"endpoint": sub.endpoint, "p256dh": sub.p256dh, "auth": sub.auth}


async def _deliver(session: AsyncSession, sub: PushSubscription, payload: dict) -> None:
    status = await webpush.send(_subscription(sub), payload)
    if status in (404, 410):  # the device removed the subscription
        await session.execute(delete(PushSubscription).where(PushSubscription.id == sub.id))
        await session.commit()
    elif status not in (200, 201, 202):
        logger.warning("push gagal (%s) untuk langganan %s", status, sub.id)


def _summary(items: list[dict], single: str, many: str, url_many: str) -> dict:
    if len(items) == 1:
        return {"title": single, "body": items[0]["body"], "url": items[0]["url"], "tag": items[0]["tag"]}
    shops = sorted({i["toko"] for i in items})
    return {"title": f"{len(items)} {many}", "body": ", ".join(shops[:3]) + (" dan lainnya" if len(shops) > 3 else ""), "url": url_many, "tag": url_many}


async def _orders(session: AsyncSession, recipients) -> None:
    perlu = services._kondisi_tahap("perlu_diproses")
    batal = (Pesanan.platform == "shopee") & (Pesanan.status_marketplace == "IN_CANCEL") & Pesanan.status.in_(["unpaid", "to_ship"])
    rows = (
        await session.execute(
            select(Pesanan.id, Pesanan.id_eksternal, Pesanan.akun_id, AkunMarketplace.nama_toko, batal.label("batal"))
            .outerjoin(AkunMarketplace, AkunMarketplace.id == Pesanan.akun_id)
            .where(perlu | batal)
        )
    ).all()
    events = {f"order:{r.id}:{'cancel' if r.batal else 'process'}": r for r in rows}
    if not await session.get(PushTerkirim, "baseline:order"):
        await _remember(session, ["baseline:order", *events])
        return
    fresh = await _unseen(session, list(events))
    if not fresh:
        return
    await _remember(session, fresh)
    for sub, allowed in recipients:
        if not sub.pesanan:
            continue
        for kind, single, many in (("process", "Pesanan perlu diproses", "pesanan baru perlu diproses"), ("cancel", "Permintaan pembatalan", "permintaan pembatalan")):
            mine = [
                {"body": f"{events[k].id_eksternal} · {events[k].nama_toko or 'Toko'}", "toko": events[k].nama_toko or "Toko", "url": f"/pesanan/{events[k].id}", "tag": k}
                for k in fresh
                if k.endswith(":" + kind) and _sees(allowed, events[k].akun_id)
            ]
            if mine:
                await _deliver(session, sub, _summary(mine, single, many, "/pesanan"))


async def _chats(session: AsyncSession, recipients) -> None:
    wanting = [(sub, allowed) for sub, allowed in recipients if sub.chat]
    if not wanting:
        return
    for akun in await services.list_akun_marketplace(session, platform="shopee"):
        if not (akun.access_token and akun.id_toko_eksternal) or not any(_sees(a, akun.id) for _, a in wanting):
            continue
        try:
            conversations = (await erp_shopee_chat.inbox(session, akun, None, True))["conversations"]
        except Exception:  # noqa: BLE001 -- one shop's failure must not stop the others
            logger.warning("push chat: toko %s gagal dibaca", akun.nama_toko, exc_info=True)
            continue
        events = {
            f"chat:{akun.id}:{c['conversation_id']}:{c.get('latest_message_id') or c.get('last_message_timestamp')}": c
            for c in conversations
            if c.get("conversation_id") and (c.get("unread_count") or 0) > 0 and c.get("needs_reply") is not False
        }
        marker = f"baseline:chat:{akun.id}"
        if not await session.get(PushTerkirim, marker):
            await _remember(session, [marker, *events])
            continue
        fresh = await _unseen(session, list(events))
        if not fresh:
            continue
        await _remember(session, fresh)
        items = [{"body": f"{events[k].get('to_name') or 'Pembeli'} mengirim pesan · {akun.nama_toko}", "toko": akun.nama_toko, "url": f"/chat?toko={akun.id}&percakapan={events[k]['conversation_id']}", "tag": k} for k in fresh]
        for sub, allowed in wanting:
            if _sees(allowed, akun.id):
                await _deliver(session, sub, _summary(items, f"Chat baru · {akun.nama_toko}", "chat baru", "/chat"))


async def putaran(session: AsyncSession) -> None:
    """One check of orders and chats; a no-op without keys or subscribers."""
    if not webpush.configured():
        return
    recipients = await _recipients(session)
    if not recipients:
        return
    await _orders(session, recipients)
    await _chats(session, recipients)


async def kirim_uji(session: AsyncSession, user_id: str) -> list[int]:
    """A test notification to every device of one user; the push service's status code per device."""
    codes: list[int] = []
    for sub in (await session.scalars(select(PushSubscription).where(PushSubscription.user_id == user_id))).all():
        status = await webpush.send(_subscription(sub), {"title": "Notifikasi aktif", "body": "Notifikasi dari Ampel Kuning ERP System sudah berfungsi di perangkat ini.", "url": "/dashboard", "tag": "uji"}, ttl=300)
        codes.append(status)
        if status in (404, 410):
            await session.execute(delete(PushSubscription).where(PushSubscription.id == sub.id))
    await session.commit()
    return codes


async def run_forever(session_factory) -> None:
    """Loop until cancelled: a failing round is logged and never stops the loop."""
    interval = interval_seconds()
    if not interval or not webpush.configured():
        return
    rounds = 0
    while True:
        await asyncio.sleep(interval)
        try:
            async with session_factory() as session:
                await putaran(session)
                rounds += 1
                if rounds % 60 == 0:
                    await session.execute(delete(PushTerkirim).where(PushTerkirim.created_at < datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS), ~PushTerkirim.key.like("baseline:%")))
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- keep the loop alive
            logger.exception("putaran push gagal")
