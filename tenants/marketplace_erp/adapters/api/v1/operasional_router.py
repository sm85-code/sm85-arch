"""Operasional Umum / Operasional Packing: dated operating-cost entries (rules in application/operasional.py)."""

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import akses
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
    tingkat = await ops.pastikan_baca(session, jenis, await akses.peran_pengguna(session, user))
    return await ops.daftar(session, jenis, periode, tingkat == "tulis")


@router.put("/{jenis}/pihak/{pihak_id}")
async def simpan_peta(
    jenis: str,
    pihak_id: str,
    isian: ops.PetaIsian,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(get_current_user_marketplace_erp),
):
    await ops.pastikan_tulis(session, jenis, await akses.peran_pengguna(session, user))
    return await ops.simpan_peta(session, jenis, pihak_id, isian)


@router.post("/{jenis}", status_code=status.HTTP_201_CREATED)
async def tambah(
    jenis: str,
    isian: ops.Isian,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(get_current_user_marketplace_erp),
):
    await ops.pastikan_tulis(session, jenis, await akses.peran_pengguna(session, user))
    return await ops.tambah(session, jenis, isian, user.id)


@router.put("/{jenis}/{id_}")
async def ubah(
    jenis: str,
    id_: str,
    isian: ops.Isian,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(get_current_user_marketplace_erp),
):
    await ops.pastikan_tulis(session, jenis, await akses.peran_pengguna(session, user))
    return await ops.ubah(session, jenis, id_, isian, user.id)


@router.delete("/{jenis}/{id_}", status_code=status.HTTP_204_NO_CONTENT)
async def hapus(
    jenis: str,
    id_: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(get_current_user_marketplace_erp),
):
    await ops.pastikan_tulis(session, jenis, await akses.peran_pengguna(session, user))
    await ops.hapus(session, jenis, id_)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
