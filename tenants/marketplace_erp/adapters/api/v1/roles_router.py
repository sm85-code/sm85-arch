"""Define roles by access (shops, pages, read or write): admin only."""

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import akses
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import require_roles_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp

router = APIRouter(prefix="/roles", tags=["Role"], dependencies=[Depends(require_roles_marketplace_erp("admin"))])


class RoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nama: str = Field(max_length=80)
    keterangan: str = Field(default="", max_length=255)
    akses_toko: str
    halaman: dict[str, str] = Field(default_factory=dict)  # page -> "baca" | "tulis" (pages left out: no access)


@router.get("")
async def daftar(session: AsyncSession = Depends(get_db_marketplace_erp)):
    result = await akses.daftar(session)
    await session.commit()
    return result


@router.post("", status_code=status.HTTP_201_CREATED)
async def buat(body: RoleIn, session: AsyncSession = Depends(get_db_marketplace_erp)):
    return {"id": await akses.buat(session, body.nama, body.keterangan, body.akses_toko, body.halaman)}


@router.put("/{role_id}")
async def ubah(role_id: str, body: RoleIn, session: AsyncSession = Depends(get_db_marketplace_erp)):
    await akses.ubah(session, role_id, body.nama, body.keterangan, body.akses_toko, body.halaman)
    return {"id": role_id}


@router.delete("/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def hapus(role_id: str, session: AsyncSession = Depends(get_db_marketplace_erp)):
    await akses.hapus(session, role_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
