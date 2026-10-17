"""Admin-only document layout storage; never issues invoices or purchase orders."""
import json
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import require_roles_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import ProcessingDocumentTemplate

router = APIRouter(dependencies=[Depends(require_roles_marketplace_erp("admin"))])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class TemplateColumn(Strict):
    field: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=200)
    width: int = Field(default=20, ge=1, le=100)
    align: Literal["left", "center", "right"] = "left"
    header_background: str = Field(default="#e2e8f0", pattern=r"^#[0-9a-fA-F]{6}$")
    header_color: str = Field(default="#172033", pattern=r"^#[0-9a-fA-F]{6}$")
    total: bool = False


class TemplateBlock(Strict):
    id: str = Field(min_length=1, max_length=80)
    kind: Literal["text", "logo", "line", "table", "total"]
    x: int = Field(ge=0, le=1123)
    y: int = Field(ge=0, le=1123)
    width: int = Field(ge=20, le=1123)
    height: int = Field(ge=8, le=1123)
    text: str = Field(default="", max_length=5000)
    font_size: int = Field(default=14, ge=8, le=72)
    bold: bool = False
    align: Literal["left", "center", "right"] = "left"
    color: str = Field(default="#172033", pattern=r"^#[0-9a-fA-F]{6}$")
    image_url: str = Field(default="", max_length=2000)
    columns: list[TemplateColumn] = Field(default_factory=list, max_length=12)
    row_count: int = Field(default=3, ge=1, le=12)
    show_totals: bool = False
    border: bool = False

    @model_validator(mode="after")
    def valid_block(self):
        if self.image_url:
            parsed = urlsplit(self.image_url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("URL logo harus HTTPS tanpa kredensial")
        return self


class TemplateInput(Strict):
    version: int = Field(ge=0)
    orientation: Literal["portrait", "landscape"] = "portrait"
    blocks: list[TemplateBlock] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_ids(self):
        width, height = (1123, 794) if self.orientation == "landscape" else (794, 1123)
        if any(block.x + block.width > width or block.y + block.height > height for block in self.blocks):
            raise ValueError("Elemen harus berada di dalam halaman A4 yang dipilih")
        if len({block.id for block in self.blocks}) != len(self.blocks):
            raise ValueError("ID elemen harus unik")
        return self


async def template_row(session: AsyncSession, kind: str):
    """Recover only a missing template table when shared startup repair was skipped.

    No existing layout is replaced; unrelated database failures still propagate.
    """
    try:
        return await session.get(ProcessingDocumentTemplate, kind)
    except DBAPIError as exc:
        original = exc.orig
        missing = getattr(original, "sqlstate", None) == "42P01" or (
            session.bind.dialect.name == "sqlite"
            and "no such table: mpe_processing_document_template" in str(original)
        )
        if not missing:
            raise
        await session.rollback()
        async with session.bind.begin() as connection:
            if connection.dialect.name == "postgresql":
                # INV and PO load concurrently; serialize additive DDL across workers.
                await connection.execute(text("SELECT pg_advisory_xact_lock(8265071701)"))
            await connection.run_sync(
                lambda sync: ProcessingDocumentTemplate.__table__.create(sync, checkfirst=True)
            )
        return await session.get(ProcessingDocumentTemplate, kind)


@router.get("/document-templates/{kind}", summary="Load template with isolated storage initialization")
async def read_template(kind: Literal["INV", "PO"], session: AsyncSession = Depends(get_db_marketplace_erp)):
    row = await template_row(session, kind)
    stored = json.loads(row.payload_json) if row else None
    # Backward-compatible with templates stored before page settings were introduced.
    if isinstance(stored, list) or stored is None:
        stored = {"orientation": "portrait", "blocks": stored}
    return {"kind": kind, "version": row.version if row else 0, **stored}


@router.put("/document-templates/{kind}")
async def save_template(kind: Literal["INV", "PO"], payload: TemplateInput,
                        session: AsyncSession = Depends(get_db_marketplace_erp)):
    current = await template_row(session, kind)
    if (current and current.version != payload.version) or (not current and payload.version):
        raise HTTPException(409, "Desain telah berubah. Muat ulang sebelum menyimpan.")
    serialized = json.dumps({"orientation": payload.orientation, "blocks": [block.model_dump(mode="json") for block in payload.blocks]})
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
