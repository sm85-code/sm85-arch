"""Kategori Transaksi: the categories of money in and out (rules in application/kategori.py)."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import kategori as kat
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import require_roles_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp

# The role settings decide (peta_akses): reading is open to every role, changing needs "tulis" on Kategori Transaksi.
router = APIRouter(prefix="/kategori-transaksi", tags=["Kategori Transaksi"], dependencies=[Depends(require_roles_marketplace_erp("admin"))])


class KategoriBaru(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arah: str
    nama: str = Field(max_length=80)
    kelompok: str
    operasional: str | None = None


class KategoriUbah(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nama: str = Field(max_length=80)
    kelompok: str
    operasional: str | None = None
    aktif: bool = True


class Urutan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arah: str
    ids: list[str]


@router.get("")
async def daftar(session: AsyncSession = Depends(get_db_marketplace_erp)):
    return await kat.daftar(session)


@router.post("", status_code=201)
async def buat(body: KategoriBaru, session: AsyncSession = Depends(get_db_marketplace_erp)):
    return await kat.buat(session, body.arah, body.nama, body.kelompok, body.operasional)


@router.put("/urutan")
async def urutkan(body: Urutan, session: AsyncSession = Depends(get_db_marketplace_erp)):
    await kat.urutkan(session, body.arah, body.ids)
    return {"ok": True}


@router.put("/{id_}")
async def ubah(id_: str, body: KategoriUbah, session: AsyncSession = Depends(get_db_marketplace_erp)):
    return await kat.ubah(session, id_, body.nama, body.kelompok, body.operasional, body.aktif)
