"""Admin-only document layout storage; never issues invoices or purchase orders."""
import json
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import require_roles_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import ProcessingDocumentTemplate

router = APIRouter(dependencies=[Depends(require_roles_marketplace_erp("admin"))])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class TemplateBlock(Strict):
    id: str = Field(min_length=1, max_length=80)
    kind: Literal["text", "logo", "line", "table", "total"]
    x: int = Field(ge=0, le=794)
    y: int = Field(ge=0, le=1123)
    width: int = Field(ge=20, le=794)
    height: int = Field(ge=8, le=1123)
    text: str = Field(default="", max_length=5000)
    font_size: int = Field(default=14, ge=8, le=72)
    bold: bool = False
    align: Literal["left", "center", "right"] = "left"
    color: str = Field(default="#172033", pattern=r"^#[0-9a-fA-F]{6}$")
    image_url: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def valid_block(self):
        if self.x + self.width > 794 or self.y + self.height > 1123:
            raise ValueError("Elemen harus berada di dalam halaman A4")
        if self.image_url:
            parsed = urlsplit(self.image_url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("URL logo harus HTTPS tanpa kredensial")
        return self


class TemplateInput(Strict):
    version: int = Field(ge=0)
    blocks: list[TemplateBlock] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({block.id for block in self.blocks}) != len(self.blocks):
            raise ValueError("ID elemen harus unik")
        return self


@router.get("/document-templates/{kind}")
async def read_template(kind: Literal["INV", "PO"], session: AsyncSession = Depends(get_db_marketplace_erp)):
    row = await session.get(ProcessingDocumentTemplate, kind)
    return {"kind": kind, "version": row.version if row else 0,
            "blocks": json.loads(row.payload_json) if row else None}


@router.put("/document-templates/{kind}")
async def save_template(kind: Literal["INV", "PO"], payload: TemplateInput,
                        session: AsyncSession = Depends(get_db_marketplace_erp)):
    current = await session.get(ProcessingDocumentTemplate, kind)
    if (current and current.version != payload.version) or (not current and payload.version):
        raise HTTPException(409, "Desain telah berubah. Muat ulang sebelum menyimpan.")
    serialized = json.dumps([block.model_dump(mode="json") for block in payload.blocks])
    try:
        if current:
            result = await session.execute(update(ProcessingDocumentTemplate).where(
                ProcessingDocumentTemplate.id == kind, ProcessingDocumentTemplate.version == payload.version
            ).values(payload_json=serialized, version=payload.version + 1))
            if result.rowcount != 1:
                raise HTTPException(409, "Desain baru saja berubah; muat ulang")
        else:
            session.add(ProcessingDocumentTemplate(id=kind, payload_json=serialized, version=1))
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Desain baru saja disimpan admin lain; muat ulang")
    return {"kind": kind, "version": payload.version + 1, "tersimpan": True}
