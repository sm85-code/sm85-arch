"""HTTP surface for bumi_lestari dokumen cetak -- Purchase Order & Invoice mingguan (data JSON)."""
from __future__ import annotations

import os
from datetime import date

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application import dokumen_services as svc
from tenants.bumi_lestari.modules.bumi_lestari.application.pdf_dokumen import render_invoice, render_po
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_dokumen import (
    BagikanIn,
    BagikanOut,
    InvoiceOut,
    PurchaseOrderOut,
)
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


def _pdf(isi: bytes, nama_file: str) -> Response:
    return Response(
        content=isi, media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{nama_file}"', "Cache-Control": "private, no-store"},
    )


@dokumen_router.get("/invoice-reseller/{pelanggan_id}/pdf")
async def invoice_pdf(pelanggan_id: str, tanggal: date | None = None, session: AsyncSession = _db(), _: BlUser = _guard()):
    inv = await svc.invoice_untuk(session, pelanggan_id, tanggal)
    return _pdf(render_invoice(inv), inv.nomor.replace("/", "-") + ".pdf")


@dokumen_router.get("/po/{pemasok_id}/pdf")
async def po_pdf(pemasok_id: str, tanggal: date | None = None, session: AsyncSession = _db(), _: BlUser = _guard()):
    po = await svc.po_untuk(session, pemasok_id, tanggal)
    return _pdf(render_po(po), po.nomor.replace("/", "-") + ".pdf")


@dokumen_router.post("/dokumen/bagikan", response_model=BagikanOut)
async def bagikan(payload: BagikanIn, request: Request, session: AsyncSession = _db(), user: BlUser = _guard()):
    """Tombol "Kirim ke WhatsApp": kembalikan `wa_link`; dibuka di HP -> WhatsApp pengguna terbuka
    dengan pesan + tautan PDF siap kirim ke penjual lain / tukang."""
    base = os.getenv("BUMI_LESTARI_PUBLIC_URL") or str(request.base_url)
    return await svc.bagikan_dokumen(session, user, payload, base)


@dokumen_router.get("/dokumen-publik/{token}")
async def dokumen_publik(token: str, session: AsyncSession = _db()):
    """PDF untuk penerima (tanpa login); token acak, berlaku 30 hari."""
    isi, nama_file = await svc.pdf_dari_token(session, token)
    return _pdf(isi, nama_file)
