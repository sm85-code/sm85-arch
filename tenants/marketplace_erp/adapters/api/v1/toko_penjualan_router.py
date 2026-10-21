"""Toko Internal & Reseller (rules in application/toko_penjualan.py)."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import toko_penjualan as tp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import require_roles_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp

# The role settings decide (peta_akses): reading and changing need the Kelola Toko page (baca / tulis).
router = APIRouter(prefix="/toko-penjualan", tags=["Toko Internal & Reseller"], dependencies=[Depends(require_roles_marketplace_erp("admin"))])


class TokoBaru(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nama: str = Field(max_length=200)
    jenis: str
    platform: str
    kode: str | None = Field(default=None, max_length=10)
    keterangan: str = Field(default="", max_length=1000)


class TokoUbah(TokoBaru):
    aktif: bool = True


@router.get("")
async def daftar(session: AsyncSession = Depends(get_db_marketplace_erp)):
    return await tp.daftar(session)


@router.post("", status_code=201)
async def buat(body: TokoBaru, session: AsyncSession = Depends(get_db_marketplace_erp)):
    return await tp.buat(session, body.nama, body.jenis, body.platform, body.kode, body.keterangan)


@router.put("/{id_}")
async def ubah(id_: str, body: TokoUbah, session: AsyncSession = Depends(get_db_marketplace_erp)):
    return await tp.ubah(session, id_, body.nama, body.jenis, body.platform, body.kode, body.keterangan, body.aktif)
