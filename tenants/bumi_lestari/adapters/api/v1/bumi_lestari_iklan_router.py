"""HTTP: kas iklan -- platform, budget 25/75, top up, pengembalian; plafon kas kecil/kas iklan (spesifikasi 8.7)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application import iklan_services as svc
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas import TransaksiOut, TransferOut
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_iklan import (
    BudgetIklanOut,
    PengaturanIklanIn,
    PengaturanIklanOut,
    PengembalianIklanIn,
    PlafonIn,
    PlafonLogOut,
    PlatformIklanIn,
    PlatformIklanOut,
    PlatformIklanPatch,
    TopupIklanIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import require_roles_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser

iklan_router = APIRouter()


def _db():
    return Depends(get_db_bumi_lestari)


def _admin():
    return Depends(require_roles_bumi_lestari("admin"))  # kas iklan: hanya admin


def _pemilik():
    return Depends(require_roles_bumi_lestari("admin", "owner"))


@iklan_router.get("/platform-iklan", response_model=list[PlatformIklanOut])
async def list_platform(session: AsyncSession = _db(), _: BlUser = _admin()):
    return await svc.list_platform(session)


@iklan_router.post("/platform-iklan", response_model=PlatformIklanOut, status_code=status.HTTP_201_CREATED)
async def create_platform(payload: PlatformIklanIn, session: AsyncSession = _db(), _: BlUser = _admin()):
    return await svc.create_platform(session, payload)


@iklan_router.patch("/platform-iklan/{platform_id}", response_model=PlatformIklanOut)
async def update_platform(platform_id: str, payload: PlatformIklanPatch, session: AsyncSession = _db(), _: BlUser = _admin()):
    return await svc.update_platform(session, platform_id, payload)


@iklan_router.get("/kas-iklan/sisa-budget", response_model=BudgetIklanOut)
async def sisa_budget(tanggal: date | None = None, session: AsyncSession = _db(), _: BlUser = _admin()):
    return await svc.sisa_budget(session, tanggal)


@iklan_router.get("/kas-iklan/pengaturan", response_model=PengaturanIklanOut)
async def get_pengaturan(session: AsyncSession = _db(), _: BlUser = _admin()):
    return await svc.get_pengaturan(session)


@iklan_router.put("/kas-iklan/pengaturan", response_model=PengaturanIklanOut)
async def set_pengaturan(payload: PengaturanIklanIn, session: AsyncSession = _db(), user: BlUser = _admin()):
    return await svc.set_pengaturan(session, user, payload)


@iklan_router.post("/kas-iklan/topup", response_model=TransaksiOut, status_code=status.HTTP_201_CREATED)
async def topup(payload: TopupIklanIn, session: AsyncSession = _db(), user: BlUser = _admin()):
    return await svc.topup(session, user, payload)


@iklan_router.post("/kas-iklan/pengembalian", response_model=TransferOut, status_code=status.HTTP_201_CREATED)
async def pengembalian(payload: PengembalianIklanIn, session: AsyncSession = _db(), user: BlUser = _admin()):
    return await svc.pengembalian(session, user, payload)


@iklan_router.put("/akun-kas/{akun_id}/plafon", response_model=PlafonLogOut)
async def ubah_plafon(akun_id: str, payload: PlafonIn, session: AsyncSession = _db(), user: BlUser = _pemilik()):
    return await svc.ubah_plafon(session, user, akun_id, payload)


@iklan_router.get("/akun-kas/{akun_id}/plafon-log", response_model=list[PlafonLogOut])
async def log_plafon(akun_id: str, session: AsyncSession = _db(), user: BlUser = _pemilik()):
    return await svc.log_plafon(session, user, akun_id)
