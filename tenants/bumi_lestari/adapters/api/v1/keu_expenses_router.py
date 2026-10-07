"""Installed on the existing tenant-local owner/admin finance router."""
from datetime import date
from typing import Literal

from fastapi import Query
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application import keu_expenses as svc, schemas_keu as legacy
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_keu_expenses import CATEGORIES, ExpenseTab, PengeluaranIn
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser


def install(router, DB, ACTOR):
    @router.get("/pengeluaran/kategori")
    async def categories():
        return [{"id": key, "tab": tab, "nama": name} for key, (tab, name, _) in CATEGORIES.items()]

    @router.post("/pengeluaran", status_code=201)
    async def create(payload: PengeluaranIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
        return await svc.create(session, actor, payload)

    @router.get("/pengeluaran", response_model=legacy.PageOut)
    async def listing(tab: ExpenseTab, tanggal_awal: date, tanggal_akhir: date,
                      search: str = Query("", max_length=255), status: Literal["pengerjaan", "batal", "semua"] = "pengerjaan",
                      limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), session: AsyncSession = DB):
        return await svc.listing(session, tab, tanggal_awal, tanggal_akhir, search, status, limit, offset)
