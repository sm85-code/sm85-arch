"""Analisis Produk-only GET namespace and transaction-level PostgreSQL write protection."""
import asyncio
from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from tenants.marketplace_erp.modules.marketplace_erp.application import product_analysis, summary
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import database
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    PeranTambahan, Role, RoleHalaman, StaffAkunMarketplace, StaffTokoPenjualan,
)

router = APIRouter(prefix="/product-analysis", tags=["Analisis Produk"])


_read_slots = asyncio.Semaphore(2)


async def readonly_session():
    if database.SessionLocal is None:
        raise HTTPException(503, "Database Analisis Produk belum tersedia")
    try:
        await asyncio.wait_for(_read_slots.acquire(), timeout=0.2)
    except TimeoutError:
        raise HTTPException(503, "Analisis Produk sedang sibuk. Coba lagi sebentar.") from None
    try:
        async with database.SessionLocal() as session:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            await session.execute(text("SET LOCAL statement_timeout = '5000ms'"))
            await session.execute(text("SET LOCAL lock_timeout = '500ms'"))
            try:
                yield session
            finally:
                await session.rollback()
    finally:
        _read_slots.release()


async def scope(session, user):
    # Read existing role rows directly; never invoke legacy role seed/self-heal.
    roles = {user.role, *(await session.scalars(select(PeranTambahan.role_id).where(PeranTambahan.user_id == user.id))).all()}
    if "admin" in roles:
        return None
    grant = await session.scalar(select(RoleHalaman.role_id).join(Role, Role.id == RoleHalaman.role_id)
                                 .where(RoleHalaman.role_id.in_(roles), RoleHalaman.halaman == "analisis-produk",
                                        RoleHalaman.tingkat == "baca").limit(1))
    if not grant:
        raise HTTPException(403, "Akses Analisis Produk ditolak")
    scopes = (await session.scalars(select(Role.akses_toko).where(Role.id.in_(roles)))).all()
    if "semua" in scopes:
        return None
    native = (await session.scalars(select(StaffAkunMarketplace.akun_id).where(StaffAkunMarketplace.user_id == user.id))).all()
    internal = (await session.scalars(select(StaffTokoPenjualan.toko_id).where(StaffTokoPenjualan.user_id == user.id))).all()
    return [*native, *("internal:" + x for x in internal)]


@router.get("")
async def get_product_analysis(
    response: Response,
    preset: Literal["hari_ini", "kemarin", "7", "30", "bulan_ini", "bulan_lalu", "khusus"] = "bulan_ini",
    start: date | None = None, end: date | None = None,
    tab: Literal["sales", "links"] = "sales",
    search: str = Query("", max_length=200),
    shop: str = Query("", max_length=80),
    page: int = Query(1, ge=1, le=10000), page_size: int = Query(25, ge=1, le=100),
    user=Depends(get_current_user_marketplace_erp), session=Depends(readonly_session),
):
    response.headers["Cache-Control"] = "no-store, private"
    start, end = summary.period(preset, start, end)
    try:
        async with asyncio.timeout(12):
            allowed = await scope(session, user)
            if shop and allowed is not None and shop not in allowed:
                raise HTTPException(403, "Akses toko ditolak")
            if tab == "links":
                result = await product_analysis.links(session, allowed, shop, search, page, page_size)
            else:
                result = await product_analysis.read(session, start, end, allowed, shop, search, page, page_size)
            shops = select(product_analysis.A.id, product_analysis.A.nama_toko).where(product_analysis.A.platform == "shopee")
            if allowed is not None:
                shops = shops.where(product_analysis.A.id.in_(allowed))
            result.update(shops=[dict(id=r.id, name=r.nama_toko) for r in (await session.execute(shops)).all()],
                          notes=product_analysis.NOTES, page=page, page_size=page_size, start=start, end=end)
            return jsonable_encoder(result, custom_encoder={Decimal: str})
    except (TimeoutError, DBAPIError):
        raise HTTPException(503, "Pembacaan Analisis Produk gagal atau melewati batas waktu. Data tidak dianggap nol; coba periode lebih pendek.") from None
