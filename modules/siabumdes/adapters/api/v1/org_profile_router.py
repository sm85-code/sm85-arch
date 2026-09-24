"""Profil BUMDES: kop surat (letterhead) untuk export PDF/Excel/Word, editable
lewat UI alih-alih env var statis. Baca boleh siapa saja yang login (dipakai
di halaman Laporan); ubah/upload logo khusus admin."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modules.siabumdes.adapters.api.deps import get_current_user, require_roles
from modules.siabumdes.adapters.external.org_logo_upload import upload_org_logo
from modules.siabumdes.identity.application.services import get_org_profile, update_org_profile
from modules.siabumdes.identity.infrastructure.models import OrgProfile
from shared.database import get_db

router = APIRouter(prefix="/api/org-profile", tags=["org-profile"])


def _out(row: OrgProfile) -> dict:
    return {
        "org_name": row.org_name,
        "org_legal_name": row.org_legal_name,
        "address": row.address,
        "village": row.village,
        "district": row.district,
        "regency": row.regency,
        "province": row.province,
        "phone": row.phone,
        "email": row.email,
        "tagline": row.tagline,
        "logo_url": row.logo_url,
        "signatory_left_title": row.signatory_left_title,
        "signatory_left_name": row.signatory_left_name,
        "signatory_mid_title": row.signatory_mid_title,
        "signatory_mid_name": row.signatory_mid_name,
        "signatory_right_title": row.signatory_right_title,
        "signatory_right_name": row.signatory_right_name,
        "primary_color": row.primary_color,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


class OrgProfilePatch(BaseModel):
    org_name: Optional[str] = Field(None, min_length=1, max_length=255)
    org_legal_name: Optional[str] = Field(None, max_length=255)
    address: Optional[str] = Field(None, max_length=500)
    village: Optional[str] = Field(None, max_length=120)
    district: Optional[str] = Field(None, max_length=120)
    regency: Optional[str] = Field(None, max_length=120)
    province: Optional[str] = Field(None, max_length=120)
    phone: Optional[str] = Field(None, max_length=60)
    email: Optional[str] = Field(None, max_length=255)
    tagline: Optional[str] = Field(None, max_length=255)
    signatory_left_title: Optional[str] = Field(None, max_length=120)
    signatory_left_name: Optional[str] = Field(None, max_length=255)
    signatory_mid_title: Optional[str] = Field(None, max_length=120)
    signatory_mid_name: Optional[str] = Field(None, max_length=255)
    signatory_right_title: Optional[str] = Field(None, max_length=120)
    signatory_right_name: Optional[str] = Field(None, max_length=255)
    primary_color: Optional[str] = Field(None, min_length=6, max_length=7)


@router.get("")
async def read_org_profile(
    _=Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    row = await get_org_profile(session)
    return _out(row)


@router.put("")
async def write_org_profile(
    payload: OrgProfilePatch,
    _admin=Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_db),
):
    fields = payload.model_dump(exclude_unset=True)
    if "primary_color" in fields and fields["primary_color"]:
        fields["primary_color"] = fields["primary_color"].lstrip("#").upper()
    row = await update_org_profile(session, **fields)
    return _out(row)


@router.post("/logo")
async def upload_logo(
    file: UploadFile = File(...),
    _admin=Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_db),
):
    data = await file.read()
    try:
        logo_url = await upload_org_logo(data, file.filename or "logo.png", file.content_type or "image/png")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Gagal upload logo: {exc}") from exc
    row = await update_org_profile(session, logo_url=logo_url)
    return _out(row)
