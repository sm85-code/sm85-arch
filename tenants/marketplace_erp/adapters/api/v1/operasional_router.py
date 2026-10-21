"""Operasional Umum / Operasional Packing: dated operating-cost entries (rules in application/operasional.py)."""

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import operasional as ops
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import UserMarketplaceErp

router = APIRouter(prefix="/operasional", tags=["Operasional"])
PERIODE = Query(pattern=r"^\d{4}-\d{2}$")


@router.get("/{jenis}")
async def daftar(
    jenis: str,
    periode: str = PERIODE,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(get_current_user_marketplace_erp),
):
    ops.pastikan_baca(jenis, user.role)
    return await ops.daftar(session, jenis, periode, user.role)


@router.post("/{jenis}", status_code=status.HTTP_201_CREATED)
async def tambah(
    jenis: str,
    isian: ops.Isian,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(get_current_user_marketplace_erp),
):
    ops.pastikan_tulis(jenis, user.role)
    return await ops.tambah(session, jenis, isian, user.id)


@router.put("/{jenis}/{id_}")
async def ubah(
    jenis: str,
    id_: str,
    isian: ops.Isian,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(get_current_user_marketplace_erp),
):
    ops.pastikan_tulis(jenis, user.role)
    return await ops.ubah(session, jenis, id_, isian, user.id)


@router.delete("/{jenis}/{id_}", status_code=status.HTTP_204_NO_CONTENT)
async def hapus(
    jenis: str,
    id_: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(get_current_user_marketplace_erp),
):
    ops.pastikan_tulis(jenis, user.role)
    await ops.hapus(session, jenis, id_)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
