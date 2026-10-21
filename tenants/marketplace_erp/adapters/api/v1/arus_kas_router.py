"""The Arus Kas page for admin and staff: the cash-flow table, ticking transactions as done, and the payment accounts.

Everything else under Laporan Keuangan stays admin-only. Staff may only tick ("sudah dilakukan"), never change figures,
rows or names, so saving here refuses any change besides the tick marks.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import financial_manual as manual
from tenants.marketplace_erp.modules.marketplace_erp.application import financial_reports as reports
from tenants.marketplace_erp.modules.marketplace_erp.application import operasional as ops
from tenants.marketplace_erp.modules.marketplace_erp.application.akses import butuh_halaman
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import UserMarketplaceErp

baca = butuh_halaman("arus_kas", "baca")
tulis = butuh_halaman("arus_kas", "tulis")  # ticking transactions; any other change is refused below
router = APIRouter(prefix="/arus-kas", tags=["Arus Kas"], dependencies=[Depends(baca)])

TANDA = "ok:"
PERIODE = Query(pattern=r"^\d{4}-\d{2}$")


@router.get("/laporan")
async def laporan(periode: str = PERIODE, session: AsyncSession = Depends(get_db_marketplace_erp)):
    return await reports.overview(session, periode)


@router.get("/operasional")
async def operasional(periode: str = PERIODE, session: AsyncSession = Depends(get_db_marketplace_erp)):
    """Operasional Umum / Packing entries per date and category, which the table shows in the matching rows."""
    return await ops.untuk_arus_kas(session, periode)


@router.get("/biaya-produksi")
async def biaya_produksi(periode: str = PERIODE, session: AsyncSession = Depends(get_db_marketplace_erp)):
    """Cost of the production stages finished in the month, from Pemetaan Order. A reference only: never posted to the sheet."""
    from .processing_workflow import biaya_produksi as hitung

    awal, akhir = ops.rentang(periode)
    return {"periode": periode, "items": await hitung(session, awal, akhir)}


@router.get("/lembar")
async def lembar(periode: str = PERIODE, session: AsyncSession = Depends(get_db_marketplace_erp)):
    return await manual.muat(session, periode)


def hanya_tanda(lama: dict, baru: manual.Lembar) -> bool:
    """True when the new sheet differs from the saved one in tick marks only."""
    sisa = lambda sel: {k: v for k, v in sel.items() if not k.startswith(TANDA)}  # noqa: E731
    return (
        sisa(lama["sel"]) == sisa(baru.sel)
        and lama["baris"] == [b.model_dump() for b in baru.baris]
        and lama["hapus"] == baru.hapus
    )


@router.put("/lembar")
async def simpan_lembar(
    payload: manual.Simpan,
    periode: str = PERIODE,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    actor: UserMarketplaceErp = Depends(tulis),
):
    saved = await manual.muat(session, periode)
    if not hanya_tanda(saved["lembar"], payload.lembar):
        raise HTTPException(403, "Di halaman ini hanya centang 'sudah dilakukan' yang dapat diubah")
    return await manual.simpan(session, periode, payload, actor)


@router.get("/rekening-tukang")
async def rekening_tukang(session: AsyncSession = Depends(get_db_marketplace_erp)):
    from .processing_workflow import party_list

    return [
        {"id": p["id"], "nama": p["nama"], "nama_bank": (p.get("nama_bank") or "").strip(), "no_rekening": p["no_rekening"].strip()}
        for p in await party_list(session, "tukang")
        if (p.get("no_rekening") or "").strip()
    ]
