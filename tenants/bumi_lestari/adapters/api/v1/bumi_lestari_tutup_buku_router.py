"""HTTP surface -- tutup buku bulanan (spesifikasi 8.10). Menutup & buka darurat khusus admin."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application import tutup_buku_services as svc
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas import BatalIn
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_tutup_buku import KesiapanOut, TutupBukuOut
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import require_roles_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser

tutup_buku_router = APIRouter()
LIHAT = Depends(require_roles_bumi_lestari("admin", "owner"))
ADMIN = Depends(require_roles_bumi_lestari("admin"))


@tutup_buku_router.get("/tutup-buku", response_model=list[TutupBukuOut])
async def daftar(session: AsyncSession = Depends(get_db_bumi_lestari), user: BlUser = LIHAT):
    return await svc.list_tutup_buku(session)


@tutup_buku_router.get("/tutup-buku/{periode}/kesiapan", response_model=KesiapanOut)
async def kesiapan(periode: str, session: AsyncSession = Depends(get_db_bumi_lestari), user: BlUser = LIHAT):
    return await svc.kesiapan(session, periode)


@tutup_buku_router.get("/tutup-buku/{periode}", response_model=TutupBukuOut)
async def detail(periode: str, session: AsyncSession = Depends(get_db_bumi_lestari), user: BlUser = LIHAT):
    row = await svc.get_tutup_buku(session, periode)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bulan ini belum pernah tutup buku")
    return row


@tutup_buku_router.post("/tutup-buku/{periode}", response_model=TutupBukuOut, status_code=status.HTTP_201_CREATED)
async def tutup(periode: str, session: AsyncSession = Depends(get_db_bumi_lestari), user: BlUser = ADMIN):
    return await svc.tutup(session, user, periode)


@tutup_buku_router.post("/tutup-buku/{periode}/buka", response_model=TutupBukuOut)
async def buka(periode: str, payload: BatalIn, session: AsyncSession = Depends(get_db_bumi_lestari), user: BlUser = ADMIN):
    return await svc.buka_darurat(session, user, periode, payload.alasan)
