"""Unauthenticated endpoints consumed by frontend-siabumdes Landing.jsx."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from modules.siabumdes.application.reporting import ReportingService
from shared.database import get_db

router = APIRouter(prefix="/api/public", tags=["public"])


@router.get("/summary")
async def public_summary(session: AsyncSession = Depends(get_db)):
    year = date.today().year
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    return await ReportingService(session).public_summary(start, end)
