"""Strict administrator-only, read-only escrow API."""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.application import akses, saldo_easyboss


async def admin(user=Depends(get_current_user_marketplace_erp), session: AsyncSession = Depends(get_db_marketplace_erp)):
    if 'admin' not in await akses.peran_pengguna(session, user):
        raise HTTPException(403, 'Saldo EasyBoss hanya untuk Administrator')
    return user


router = APIRouter(prefix='/saldo-easyboss', tags=['Saldo EasyBoss'], dependencies=[Depends(admin)])


@router.get('')
async def saldo(platform: Literal['', 'tiktok', 'lazada'] = '', toko: str = Query('', max_length=64), status: str = Query('', max_length=80), search: str = Query('', max_length=64), page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
    sources = await saldo_easyboss.snapshot()
    if sources is None:
        return {'loading': True}
    return saldo_easyboss.projection(sources, platform, toko, status, search, page, page_size)
