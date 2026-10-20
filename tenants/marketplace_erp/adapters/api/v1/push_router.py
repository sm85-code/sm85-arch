"""Web Push subscriptions: each user turns notifications on or off per device; the keys come from the server environment."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .workflow_router import staff
from tenants.marketplace_erp.modules.marketplace_erp.application import push_notifications, webpush
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import PushSubscription, UserMarketplaceErp

router = APIRouter(prefix="/push", tags=["Notifikasi push"])
DB = Depends(get_db_marketplace_erp)
STAFF = Depends(staff)


class Keys(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p256dh: str = Field(min_length=1, max_length=200)
    auth: str = Field(min_length=1, max_length=100)


class Subscribe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(min_length=1, max_length=1000)
    keys: Keys
    pesanan: bool = True
    chat: bool = True


class Endpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(min_length=1, max_length=1000)


@router.get("/konfigurasi")
async def konfigurasi(user: UserMarketplaceErp = STAFF):
    settings = webpush.configured()
    return {"aktif": bool(settings), "kunci_publik": settings[0] if settings else None}


@router.post("/langganan")
async def subscribe(body: Subscribe, session: AsyncSession = DB, user: UserMarketplaceErp = STAFF):
    if not webpush.configured():
        raise HTTPException(409, "Notifikasi belum diaktifkan di server")
    if not webpush.endpoint_valid(body.endpoint):
        raise HTTPException(422, "Perangkat ini tidak didukung untuk notifikasi")
    sub = await session.scalar(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    if sub is None:
        sub = PushSubscription(user_id=user.id, endpoint=body.endpoint, p256dh=body.keys.p256dh, auth=body.keys.auth)
        session.add(sub)
    # A shared device that logs in as someone else is handed over to the current user.
    sub.user_id, sub.p256dh, sub.auth, sub.pesanan, sub.chat = user.id, body.keys.p256dh, body.keys.auth, body.pesanan, body.chat
    await session.commit()
    return {"aktif": True, "pesanan": sub.pesanan, "chat": sub.chat}


@router.post("/status")
async def status(body: Endpoint, session: AsyncSession = DB, user: UserMarketplaceErp = STAFF):
    sub = await session.scalar(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint, PushSubscription.user_id == user.id))
    return {"aktif": sub is not None, "pesanan": bool(sub and sub.pesanan), "chat": bool(sub and sub.chat)}


@router.post("/berhenti")
async def unsubscribe(body: Endpoint, session: AsyncSession = DB, user: UserMarketplaceErp = STAFF):
    await session.execute(delete(PushSubscription).where(PushSubscription.endpoint == body.endpoint, PushSubscription.user_id == user.id))
    await session.commit()
    return {"aktif": False}


@router.post("/uji")
async def test_push(session: AsyncSession = DB, user: UserMarketplaceErp = STAFF):
    if not webpush.configured():
        raise HTTPException(409, "Notifikasi belum diaktifkan di server")
    return {"terkirim": await push_notifications.kirim_uji(session, user.id)}
