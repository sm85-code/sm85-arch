"""API baru Bumi Lestari. Layar baru hanya memakai ini."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application import erp_pencairan, order_services
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import require_roles_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser

v2_router = APIRouter(prefix="/v2")


def _db():
    return Depends(get_db_bumi_lestari)


def _guard():
    return Depends(require_roles_bumi_lestari("owner", "admin"))


@v2_router.get("/order")
async def order(session: AsyncSession = _db(), _: BlUser = _guard()):
    return await order_services.list_order(session)


@v2_router.get("/belum-peta")
async def belum_peta(session: AsyncSession = _db(), _: BlUser = _guard()):
    return await erp_pencairan.daftar_belum_peta(session)


@v2_router.post("/peta")
async def peta(payload: dict, session: AsyncSession = _db(), _: BlUser = _guard()):
    try:
        return await erp_pencairan.simpan_peta(session, str(payload.get("nama") or ""), str(payload.get("produk_id") or ""))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@v2_router.post("/tarik-order")
async def tarik_order(hari: int = 30, session: AsyncSession = _db(), _: BlUser = _guard()):
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import SessionLocal

    async with SessionLocal() as erp:
        return await erp_pencairan.tarik_order(session, erp, hari=hari)
