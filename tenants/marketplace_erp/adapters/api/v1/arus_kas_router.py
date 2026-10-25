"""The Arus Kas page for admin and staff: the cash-flow table, ticking transactions as done, and the payment accounts.

Everything else under Laporan Keuangan stays admin-only. Staff may only tick ("sudah dilakukan"), never change figures,
rows or names, so saving here refuses any change besides the tick marks.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import financial_manual as manual
from tenants.marketplace_erp.modules.marketplace_erp.application import financial_reports as reports
from tenants.marketplace_erp.modules.marketplace_erp.application import operasional as ops
from tenants.marketplace_erp.modules.marketplace_erp.application.akses import butuh_halaman
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import require_roles_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import PengaturanUi, UserMarketplaceErp

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
    return {"periode": periode, **await hitung(session, awal, akhir)}


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


# The payment-account list: the accounts that are not craftsmen and the order of all rows, set by an admin. Kept as one
# JSON setting (mpe_pengaturan_ui); craftsmen accounts themselves stay in Tukang Kayu and are never stored here.
KUNCI_REKENING = "rekening:daftar"


class RekeningLain(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    nama: str = Field(min_length=1, max_length=120)
    jenis: str = Field(default="", max_length=80)
    bank: str = Field(default="", max_length=60)
    rekening: str = Field(min_length=1, max_length=40)


class DaftarRekening(BaseModel):
    lain: list[RekeningLain] = Field(default_factory=list, max_length=200)
    urutan: list[str] = Field(default_factory=list, max_length=400)


@router.get("/rekening-daftar")
async def rekening_daftar(session: AsyncSession = Depends(get_db_marketplace_erp)):
    """Saved list, or null while the admin has never changed it (the page then shows its built-in list)."""
    row = await session.get(PengaturanUi, KUNCI_REKENING)
    return json.loads(row.teks) if row else None


@router.put("/rekening-daftar")
async def simpan_rekening_daftar(
    payload: DaftarRekening,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _actor: UserMarketplaceErp = Depends(require_roles_marketplace_erp("admin")),
):
    ids = [r.id for r in payload.lain]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "ID rekening ganda")
    data = payload.model_dump()
    teks = json.dumps(data, ensure_ascii=False)
    row = await session.get(PengaturanUi, KUNCI_REKENING)
    if row:
        row.teks = teks
    else:
        session.add(PengaturanUi(id=KUNCI_REKENING, teks=teks))
    await session.commit()
    return data
