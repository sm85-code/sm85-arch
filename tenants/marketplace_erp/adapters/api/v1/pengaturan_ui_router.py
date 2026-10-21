"""Kelola Aplikasi: the texts of the app that can be changed (rules in application/pengaturan_ui.py)."""

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import pengaturan_ui as ui
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import require_roles_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp

# The role settings decide (peta_akses): reading is open to every role, changing needs "tulis" on Kelola Aplikasi.
router = APIRouter(prefix="/pengaturan-ui", tags=["Kelola Aplikasi"], dependencies=[Depends(require_roles_marketplace_erp("admin"))])


class Teks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    teks: str = Field(max_length=200)


@router.get("")
async def daftar(session: AsyncSession = Depends(get_db_marketplace_erp)):
    return await ui.daftar(session)


@router.put("/{id_:path}")
async def simpan(id_: str, body: Teks, session: AsyncSession = Depends(get_db_marketplace_erp)):
    return await ui.simpan(session, id_, body.teks)


@router.delete("/{id_:path}", status_code=status.HTTP_204_NO_CONTENT)
async def kembalikan(id_: str, session: AsyncSession = Depends(get_db_marketplace_erp)):
    await ui.kembalikan(session, id_)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
