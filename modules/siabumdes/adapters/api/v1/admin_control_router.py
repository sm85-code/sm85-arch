"""Admin system lock. Period close lives in adapters/api/v1/period_close_router.py."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from modules.siabumdes.adapters.api.deps import require_roles
from modules.siabumdes.identity.application.services import get_system_control, set_recording_lock
from modules.siabumdes.identity.infrastructure.models import User
from shared.database import get_db

router = APIRouter(prefix="/api", tags=["admin-control"])


class SystemLockRequest(BaseModel):
    locked: bool
    note: str = ""


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
