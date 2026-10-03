"""HTTP surface -- "Kirim ke laporan keuangan" (bl_kiriman) dan log audit (admin)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application import kiriman_services as svc
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas import BatalIn
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_kiriman import (
    AuditOut,
    DrafSumberOut,
    KirimanDetailOut,
    KirimanItemOut,
    KirimanOut,
    KirimIn,
    KirimSemuaIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import require_roles_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlAuditLog, BlUser

kiriman_router = APIRouter()
# Owner masih boleh mengirim (sama dengan akses owner saat ini); owner hanya-lihat menyusul (spesifikasi KP-LP-3).
OWNER_UP = ("admin", "owner")


def _guard(*roles: str):
    return Depends(require_roles_bumi_lestari(*(roles or OWNER_UP)))


@kiriman_router.get("/kiriman/draf", response_model=list[DrafSumberOut])
async def draf(
    sumber: str | None = None,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    user: BlUser = _guard(),
):
    return await svc.ringkasan_draf(session, user, sumber)


@kiriman_router.post("/kiriman", response_model=KirimanOut, status_code=status.HTTP_201_CREATED)
async def kirim(payload: KirimIn, session: AsyncSession = Depends(get_db_bumi_lestari), user: BlUser = _guard()):
    return await svc.kirim(session, user, payload.sumber, payload.sampai_tanggal, payload.tutup_kas_mingguan_id)


@kiriman_router.post("/kiriman/semua", response_model=list[KirimanOut], status_code=status.HTTP_201_CREATED)
async def kirim_semua(
    payload: KirimSemuaIn, session: AsyncSession = Depends(get_db_bumi_lestari), user: BlUser = _guard()
):
    return await svc.kirim_semua(session, user, payload.sampai_tanggal, payload.tutup_kas_mingguan_id)


@kiriman_router.get("/kiriman", response_model=list[KirimanOut])
async def list_kiriman(
    sumber: str | None = None,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    user: BlUser = _guard(),
):
    return await svc.list_kiriman(session, user, sumber)


@kiriman_router.get("/kiriman/{kiriman_id}", response_model=KirimanDetailOut)
async def detail_kiriman(kiriman_id: str, session: AsyncSession = Depends(get_db_bumi_lestari), user: BlUser = _guard()):
    kiriman, items = await svc.detail_kiriman(session, user, kiriman_id)
    out = KirimanDetailOut.model_validate(kiriman)
    out.items = [KirimanItemOut.model_validate(i) for i in items]
    return out


@kiriman_router.post("/kiriman/{kiriman_id}/batal", response_model=KirimanOut)
async def batal_kiriman(
    kiriman_id: str, payload: BatalIn, session: AsyncSession = Depends(get_db_bumi_lestari), user: BlUser = _guard()
):
    return await svc.batal_kiriman(session, user, kiriman_id, payload.alasan)


@kiriman_router.get("/audit", response_model=list[AuditOut])
async def list_audit(
    entitas: str | None = None,
    entitas_id: str | None = None,
    limit: int = 200,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    _: BlUser = _guard("admin"),
):
    stmt = select(BlAuditLog).order_by(BlAuditLog.waktu.desc()).limit(min(max(limit, 1), 1000))
    if entitas:
        stmt = stmt.where(BlAuditLog.entitas == entitas)
    if entitas_id:
        stmt = stmt.where(BlAuditLog.entitas_id == entitas_id)
    return list((await session.execute(stmt)).scalars())
