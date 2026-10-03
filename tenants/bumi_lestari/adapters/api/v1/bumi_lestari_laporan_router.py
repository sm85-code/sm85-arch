"""HTTP surface for bumi_lestari -- dashboard, laporan umum, laporan kas kecil.
Laporan kas iklan belum disediakan (dikerjakan admin nanti; hanya admin yang boleh mengakses kas iklan)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application import laporan_services as svc
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_laporan import (
    BelumCairOut,
    DashboardOut,
    LaporanImprestOut,
    LaporanUmumOut,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import require_roles_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser

laporan_router = APIRouter()
PERIODE = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="YYYY-MM")


@laporan_router.get("/dashboard", response_model=DashboardOut)
async def dashboard(
    periode: str | None = PERIODE,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    user: BlUser = Depends(require_roles_bumi_lestari("admin", "owner")),
):
    return await svc.dashboard(session, user, periode)


@laporan_router.get("/laporan/umum", response_model=LaporanUmumOut)
async def laporan_umum(
    dari: date | None = None,
    sampai: date | None = None,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    user: BlUser = Depends(require_roles_bumi_lestari("admin", "owner")),
):
    return await svc.laporan_umum(session, user, dari, sampai)


@laporan_router.get("/laporan/belum-cair", response_model=BelumCairOut)
async def laporan_belum_cair(
    per_tanggal: date | None = None,
    session: AsyncSession = Depends(get_db_bumi_lestari),
    _: BlUser = Depends(require_roles_bumi_lestari("admin", "owner")),
):
    """Order marketplace/Toko web sudah dikirim, belum cair, belum retur -- per saluran (AB-BC-1)."""
    return await svc.belum_cair(session, per_tanggal)


@laporan_router.get("/laporan/kas-kecil", response_model=LaporanImprestOut)
async def laporan_kas_kecil(
    periode: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="YYYY-MM"),
    saldo_fisik: Decimal | None = Query(default=None, ge=0),
    session: AsyncSession = Depends(get_db_bumi_lestari),
    _: BlUser = Depends(require_roles_bumi_lestari("admin", "owner", "staff")),
):
    """Laporan kas kecil bulanan + rincian mingguan; `saldo_fisik` (uang yang dihitung) menandai selisih."""
    return await svc.laporan_imprest(session, "kas_kecil", periode, saldo_fisik)
