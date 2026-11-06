"""ERP cookie/JWT + existing page-level permissions; no owner credentials used by scheduler."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import order_bridge as bridge
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import easyboss as eb
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import require_roles_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import BridgeOrder, BridgeRun

router = APIRouter(prefix="/order-bridge", tags=["Order Bridge"],
                   dependencies=[Depends(require_roles_marketplace_erp("admin"))])


class SyncIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview: bool = True
    fingerprint: str | None = Field(default=None, max_length=64)
    links: dict[str, str] = Field(default_factory=dict, max_length=2000)


class EnabledIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


@router.get("")
async def dashboard(session: AsyncSession = Depends(get_db_marketplace_erp)):
    return await bridge.dashboard(session)


@router.post("/shops")
async def shops(body: SyncIn, session: AsyncSession = Depends(get_db_marketplace_erp)):
    try:
        async with eb.client() as client:
            return await bridge.sync_shops(session, eb.Gateway(client), preview=body.preview,
                                           links=body.links, expected=body.fingerprint)
    except eb.BridgeProblem as exc:
        raise HTTPException(503, str(exc)) from None
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Konflik identitas atau nama toko; periksa pratinjau ulang") from None


@router.post("/orders")
async def orders(retry: bool = False, session: AsyncSession = Depends(get_db_marketplace_erp)):
    try:
        async with eb.client() as client:
            return await bridge.run_orders(session, eb.Gateway(client), retry=retry)
    except eb.BridgeProblem as exc:
        raise HTTPException(503, str(exc)) from None


@router.put("/automation")
async def automation(body: EnabledIn, session: AsyncSession = Depends(get_db_marketplace_erp)):
    if body.enabled and not eb.configured():
        raise HTTPException(409, "Kontrak EasyBoss belum diverifikasi")
    async with bridge.exclusive(session):
        st = await bridge.state(session)
        st.enabled = body.enabled
        session.add(BridgeRun(kind="automation", status="enabled" if body.enabled else "paused", finished_at=bridge.now()))
        await session.commit()
    return {"enabled": st.enabled}


@router.get("/logs/{id_}")
async def log(id_: str, session: AsyncSession = Depends(get_db_marketplace_erp)):
    import json
    row = await session.get(BridgeOrder, id_)
    if row is None:
        raise HTTPException(404, "Log tidak ditemukan")
    return {"id": row.id, "status": row.status, "error": row.error, "attempts": row.attempts,
            "pesanan_id": row.pesanan_id, "snapshot": json.loads(row.snapshot_json), "audit": json.loads(row.audit_json)}
