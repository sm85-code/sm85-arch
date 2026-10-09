"""Durable cleanup receipts: failed deletion and rolled-back uploads are retried."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..infrastructure import database
from ..infrastructure.models import MediaCleanup
from ..infrastructure.media_storage import delete_foto
from .services import foto_masih_dipakai

logger = logging.getLogger(__name__)


async def enqueue(session, key):
    if key:
        insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
        stmt = insert(MediaCleanup).values(key=key, created_at=datetime.now(timezone.utc))
        await session.execute(stmt.on_conflict_do_update(index_elements=[MediaCleanup.key],
            set_={"created_at": datetime.now(timezone.utc)}))


async def record_upload(key):
    # Separate transaction: a subsequent business rollback must not lose the receipt.
    if database.SessionLocal is not None:
        async with database.SessionLocal() as session:
            await enqueue(session, key)
            await session.commit()


async def run_once(factory):
    async with factory() as session:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        rows = list((await session.execute(select(MediaCleanup).where(
            MediaCleanup.created_at < cutoff
        ).order_by(MediaCleanup.created_at).limit(20).with_for_update(skip_locked=True))).scalars())
        for row in rows:
            if await foto_masih_dipakai(session, row.key) or await delete_foto(row.key):
                await session.delete(row)
        await session.commit()


async def run_forever(factory):
    while True:
        try:
            await run_once(factory)
            from .order_expiry import expire_idle
            async with factory() as session:
                await expire_idle(session)
                await session.commit()
        except Exception:
            logger.exception("Store media cleanup failed; retry next interval")
        await asyncio.sleep(300)
