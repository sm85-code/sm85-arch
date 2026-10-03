"""HTTP surface definisi kolom (spesifikasi 10.6). Kelola: Admin. Baca: semua peran (disaring per peran)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application import kolom_services as svc
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_kolom import (
    DefinisiKolomIn,
    DefinisiKolomOut,
    DefinisiKolomPatch,
    LabelIntiIn,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import require_roles_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser

kolom_router = APIRouter()
ENTITAS = Query(pattern=r"^(order|produk|pemasok|pelanggan|transaksi|karyawan)$")
_db = Depends(get_db_bumi_lestari)
_admin = Depends(require_roles_bumi_lestari("admin"))


@kolom_router.get("/definisi-kolom", response_model=list[DefinisiKolomOut])
async def list_definisi(
    entitas: str = ENTITAS, session: AsyncSession = _db, user: BlUser = Depends(require_roles_bumi_lestari("admin", "owner", "staff")),
):
    return await svc.list_definisi(session, user, entitas)


@kolom_router.post("/definisi-kolom", response_model=DefinisiKolomOut, status_code=status.HTTP_201_CREATED)
async def create_definisi(payload: DefinisiKolomIn, session: AsyncSession = _db, user: BlUser = _admin):
    return await svc.create_definisi(session, user, payload)


@kolom_router.patch("/definisi-kolom/inti/{entitas}/{kunci}", response_model=DefinisiKolomOut)
async def ubah_label_inti(entitas: str, kunci: str, payload: LabelIntiIn, session: AsyncSession = _db, user: BlUser = _admin):
    return await svc.ubah_label_inti(session, user, entitas, kunci, payload)


@kolom_router.post("/definisi-kolom/inti/{entitas}/{kunci}/reset-label", status_code=status.HTTP_204_NO_CONTENT)
async def reset_label_inti(entitas: str, kunci: str, session: AsyncSession = _db, _: BlUser = _admin):
    await svc.reset_label_inti(session, entitas, kunci)


@kolom_router.patch("/definisi-kolom/{definisi_id}", response_model=DefinisiKolomOut)
async def update_definisi(definisi_id: str, payload: DefinisiKolomPatch, session: AsyncSession = _db, _: BlUser = _admin):
    return await svc.update_definisi(session, definisi_id, payload)


@kolom_router.post("/definisi-kolom/{definisi_id}/nonaktif", response_model=DefinisiKolomOut)
async def nonaktifkan(definisi_id: str, session: AsyncSession = _db, _: BlUser = _admin):
    return await svc.nonaktifkan(session, definisi_id)


@kolom_router.delete("/definisi-kolom/{definisi_id}", status_code=status.HTTP_204_NO_CONTENT)
async def hapus(definisi_id: str, session: AsyncSession = _db, _: BlUser = _admin):
    await svc.hapus(session, definisi_id)
