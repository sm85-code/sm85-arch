"""Account-scoped work notifications. Acknowledgements are private to each user."""
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy import case, func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .workflow_router import staff
from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.workflow_schemas import Input
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import akun_ids_diizinkan
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import AkunMarketplace, NotificationRead, Pesanan, UserMarketplaceErp

router = APIRouter(prefix="/notifikasi")
DB = Depends(get_db_marketplace_erp)
STAFF = Depends(staff)
Key = Annotated[str, Field(min_length=1, max_length=255, pattern=r"^(order|chat):[^\s]+$")]


class ReadIn(Input):
    keys: list[Key] = Field(default_factory=list, max_length=1000)
    semua_pesanan: bool = False


async def order_sources(session, user):
    allowed = await akun_ids_diizinkan(user, session)
    cancel = (Pesanan.platform == "shopee") & (Pesanan.status_marketplace == "IN_CANCEL") & Pesanan.status.in_(["unpaid", "to_ship"])
    processing = services._kondisi_tahap("perlu_diproses")
    kind = case((cancel, literal("cancel")), else_=literal("process"))
    key = literal("order:") + Pesanan.id + literal(":") + kind
    return select(
        key.label("key"), Pesanan.id.label("id"), Pesanan.id_eksternal.label("order_sn"),
        Pesanan.akun_id.label("akun_id"), AkunMarketplace.nama_toko.label("nama_toko"),
        kind.label("kind"), func.coalesce(Pesanan.dipesan_at, Pesanan.created_at).label("created_at"),
    ).outerjoin(AkunMarketplace, AkunMarketplace.id == Pesanan.akun_id).where(
        cancel | processing,
        *services._kondisi_pesanan(akun_diizinkan=allowed),
    ).subquery()


@router.get("")
async def notifications(
    halaman: int = Query(1, ge=1),
    session: AsyncSession = DB, user: UserMarketplaceErp = STAFF,
):
    source = await order_sources(session, user)
    read = select(NotificationRead.key).where(NotificationRead.user_id == user.id)
    unread = await session.scalar(select(func.count()).select_from(source).where(source.c.key.not_in(read)))
    rows = (await session.execute(select(source).order_by(source.c.kind, source.c.created_at.desc(), source.c.id).offset((halaman - 1) * 50).limit(51))).mappings().all()
    keys = [r["key"] for r in rows[:50]]
    acknowledged = set((await session.scalars(read.where(NotificationRead.key.in_(keys)))).all()) if keys else set()
    return {"items": [{**dict(r), "read": r["key"] in acknowledged, "title": "Permintaan pembatalan" if r["kind"] == "cancel" else "Pesanan perlu diproses", "href": f"/pesanan/{r['id']}"} for r in rows[:50]], "unread": unread or 0, "halaman": halaman, "ada_lagi": len(rows) > 50}


@router.post("/status-dibaca")
async def read_status(body: ReadIn, session: AsyncSession = DB, user: UserMarketplaceErp = STAFF):
    return {"keys": list((await session.scalars(select(NotificationRead.key).where(NotificationRead.user_id == user.id, NotificationRead.key.in_(body.keys)))).all())}


@router.post("/dibaca")
async def acknowledge(body: ReadIn, session: AsyncSession = DB, user: UserMarketplaceErp = STAFF):
    # Only this user's acknowledgements change; no Shopee writes or order changes.
    keys = set(body.keys)
    source = await order_sources(session, user)
    order_keys = {k for k in keys if k.startswith("order:")}
    valid_order_keys = set((await session.scalars(select(source.c.key).where(source.c.key.in_(order_keys)))).all())
    keys = {k for k in keys if k.startswith("chat:")} | valid_order_keys
    if body.semua_pesanan:
        keys.update((await session.scalars(select(source.c.key))).all())
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    # Keep parameter counts bounded, also for a large order backlog.
    values = [{"user_id": user.id, "key": key, "created_at": datetime.now(timezone.utc)} for key in sorted(keys)]
    for offset in range(0, len(values), 200):
        await session.execute(insert(NotificationRead).values(values[offset:offset + 200]).on_conflict_do_nothing(index_elements=["user_id", "key"]))
    await session.commit()
    return {"ok": True}
