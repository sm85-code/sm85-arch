"""HTTP surface for bumi_lestari dokumen cetak -- Purchase Order & Invoice mingguan (data JSON)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application import dokumen_services as svc
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_dokumen import InvoiceOut, PurchaseOrderOut
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import require_roles_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser

dokumen_router = APIRouter()


def _guard():
    return Depends(require_roles_bumi_lestari("admin", "owner"))


def _db():
    return Depends(get_db_bumi_lestari)


@dokumen_router.get("/po/siap", response_model=list[PurchaseOrderOut])
async def po_siap(tanggal: date | None = None, session: AsyncSession = _db(), _: BlUser = _guard()):
    """PO per tukang/supplier untuk order yang siap dibayar Selasa ini (belum perlu dicatat)."""
    return await svc.po_dari_siap(session, tanggal)


@dokumen_router.get("/po/pembayaran/{pembayaran_id}", response_model=list[PurchaseOrderOut])
async def po_pembayaran(pembayaran_id: str, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.po_dari_pembayaran(session, pembayaran_id)


@dokumen_router.get("/invoice-reseller", response_model=list[InvoiceOut])
async def invoice_reseller(
    tanggal: date | None = None, pelanggan_id: str | None = None, session: AsyncSession = _db(), _: BlUser = _guard()
):
    """Invoice mingguan per penjual lain (satu per pelanggan yang punya tagihan)."""
    return await svc.invoice_reseller(session, tanggal, pelanggan_id)
