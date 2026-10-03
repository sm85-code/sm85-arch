"""HTTP: talangan (spesifikasi 8.8; Tutup Kas Mingguan langkah 5)."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application import talangan_services as svc
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas import BatalIn
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_talangan import LunasiTalanganIn, TalanganOut
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import require_roles_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser

talangan_router = APIRouter()
OWNER_UP = ("admin", "owner")


def _db():
    return Depends(get_db_bumi_lestari)


@talangan_router.get("/talangan", response_model=list[TalanganOut])
async def list_talangan(
    status: Literal["belum_lunas", "lunas", "semua"] = "belum_lunas", nama: str | None = None,
    session: AsyncSession = _db(), user: BlUser = Depends(require_roles_bumi_lestari(*OWNER_UP)),
):
    return await svc.list_talangan(session, user, status_filter=status, nama=nama)


@talangan_router.get("/talangan/nama", response_model=list[str])
async def daftar_nama(session: AsyncSession = _db(), _: BlUser = Depends(require_roles_bumi_lestari("admin", "owner", "staff"))):
    return await svc.daftar_nama(session)


@talangan_router.post("/talangan/{talangan_id}/lunasi", response_model=TalanganOut)
async def lunasi(
    talangan_id: str, payload: LunasiTalanganIn, session: AsyncSession = _db(),
    user: BlUser = Depends(require_roles_bumi_lestari(*OWNER_UP)),
):
    return await svc.lunasi(session, user, talangan_id, payload)


@talangan_router.post("/talangan/bayar/{bayar_id}/batal", response_model=TalanganOut)
async def batal_bayar(
    bayar_id: str, payload: BatalIn, session: AsyncSession = _db(), user: BlUser = Depends(require_roles_bumi_lestari(*OWNER_UP)),
):
    return await svc.batal_bayar(session, user, bayar_id, payload.alasan)


@talangan_router.post("/talangan/{talangan_id}/batal", response_model=TalanganOut)
async def batal_talangan(
    talangan_id: str, payload: BatalIn, session: AsyncSession = _db(), user: BlUser = Depends(require_roles_bumi_lestari(*OWNER_UP)),
):
    return await svc.batal_talangan(session, user, talangan_id, payload.alasan)
