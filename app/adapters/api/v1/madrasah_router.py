"""HTTP surface for the isolated madrasah module."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.madrasah.application import services
from app.modules.madrasah.application.schemas import AbsenBulkRequest, LoginRequest, ProgresCreateRequest
from app.modules.madrasah.infrastructure.database import get_db_madrasah

madrasah_router = APIRouter()
router = madrasah_router


def _user_out(user) -> dict:
    return {"id": user.id, "nama": user.nama, "no_hp": user.no_hp, "role": user.role}


@madrasah_router.post("/auth/login")
async def login(payload: LoginRequest, session: AsyncSession = Depends(get_db_madrasah)):
    try:
        user = await services.login_by_phone(session, payload)
    except services.MadrasahAuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    return {"user": _user_out(user)}


@madrasah_router.post("/absensi/bulk", status_code=status.HTTP_201_CREATED)
async def absensi_bulk(payload: AbsenBulkRequest, session: AsyncSession = Depends(get_db_madrasah)):
    try:
        rows = await services.bulk_insert_absensi(session, payload)
    except services.MadrasahNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"inserted": len(rows), "ids": [row.id for row in rows]}


@madrasah_router.post("/progres", status_code=status.HTTP_201_CREATED)
async def create_progres(payload: ProgresCreateRequest, session: AsyncSession = Depends(get_db_madrasah)):
    row = await services.create_progres(session, payload)
    return {
        "id": row.id,
        "tanggal": row.tanggal.isoformat(),
        "santri_id": row.santri_id,
        "tipe": row.tipe,
        "capaian": row.capaian,
        "catatan_guru": row.catatan_guru,
    }


@madrasah_router.get("/tagihan/{santri_id}")
async def get_tagihan(santri_id: str, session: AsyncSession = Depends(get_db_madrasah)):
    rows = await services.list_tagihan(session, santri_id)
    return [
        {
            "id": row.id,
            "bulan_tahun": row.bulan_tahun,
            "nominal": float(row.nominal),
            "status_bayar": row.status_bayar,
            "santri_id": row.santri_id,
        }
        for row in rows
    ]


@madrasah_router.get("/pengumuman")
async def get_pengumuman(session: AsyncSession = Depends(get_db_madrasah)):
    rows = await services.list_pengumuman(session)
    return [
        {
            "id": row.id,
            "judul": row.judul,
            "isi": row.isi,
            "tanggal": row.tanggal.isoformat(),
            "dibuat_by": row.dibuat_by,
        }
        for row in rows
    ]
