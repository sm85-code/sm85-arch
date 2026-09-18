"""Admin system lock + accounting period close/unclose (frontend contract)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.api.deps import get_current_user, require_roles
from modules.identity.application.services import (
    close_period,
    get_system_control,
    list_closed_periods,
    reopen_period,
    set_recording_lock,
)
from modules.identity.infrastructure.models import User
from modules.siabumdes.infrastructure.models import UnitUsaha
from shared.database import get_db

router = APIRouter(prefix="/api", tags=["admin-control"])


class SystemLockRequest(BaseModel):
    locked: bool
    note: str = ""


class ClosePeriodRequest(BaseModel):
    period: str = Field(..., examples=["2026-09"])
    group: str = Field(default="BUMDES")


@router.get("/admin/system-lock")
async def get_lock(
    _: User = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_db),
):
    control = await get_system_control(session)
    return {
        "locked": control.recording_locked,
        "locked_at": control.locked_at.isoformat() if control.locked_at else None,
        "locked_by": control.locked_by,
        "note": control.note,
    }


@router.put("/admin/system-lock")
async def put_lock(
    payload: SystemLockRequest,
    admin: User = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_db),
):
    control = await set_recording_lock(
        session,
        locked=payload.locked,
        actor_id=admin.id,
        note=payload.note,
    )
    return {
        "locked": control.recording_locked,
        "locked_at": control.locked_at.isoformat() if control.locked_at else None,
        "locked_by": control.locked_by,
        "note": control.note,
    }


@router.get("/reports/closed-periods")
async def closed_periods(
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    rows = await list_closed_periods(session)
    return [
        {
            "period": row.period,
            "group": row.group_code,
            "laba_bersih": float(row.laba_bersih),
            "entries": row.entries,
            "closed_at": row.closed_at.isoformat(),
            "closed_by": row.closed_by,
        }
        for row in rows
    ]


@router.post("/reports/close-period")
async def post_close_period(
    payload: ClosePeriodRequest,
    admin: User = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_db),
):
    try:
        row = await close_period(
            session,
            period=payload.period,
            group=payload.group,
            actor_id=admin.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "closed": True,
        "period": row.period,
        "group": row.group_code,
        "entries": row.entries,
        "laba_bersih": float(row.laba_bersih),
    }


@router.delete("/reports/close-period")
async def delete_close_period(
    period: str = Query(...),
    group: str = Query("BUMDES"),
    _: User = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_db),
):
    try:
        deleted = await reopen_period(session, period, group)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted_entries": deleted}


@router.get("/unit-usaha")
async def list_unit_usaha(
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    rows = (await session.execute(select(UnitUsaha).order_by(UnitUsaha.code.asc()))).scalars()
    return [
        {
            "id": row.id,
            "code": row.code,
            "name": row.name,
            "description": row.description,
            "active": row.active,
        }
        for row in rows
    ]
