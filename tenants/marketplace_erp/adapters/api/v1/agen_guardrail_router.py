"""Limits, STOP, and the audit log for the ads agent. These routes do not call Shopee."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import agen_aksi, agen_guardrail, agen_jadwal, services
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import (
    akun_ids_diizinkan,
    pastikan_akses_akun,
    require_roles_marketplace_erp,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import UserMarketplaceErp

router = APIRouter(tags=["Agen Iklan"])
ADMIN = Depends(require_roles_marketplace_erp("admin"))
DB = Depends(get_db_marketplace_erp)


@router.get("/akun/{akun_id}/iklan/agen/batas")
async def baca_batas(akun_id: str, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    akun = await services.get_akun_marketplace(session, akun_id)
    await pastikan_akses_akun(user, session, akun.id)
    return await agen_guardrail.baca_batas(session, akun.id)


@router.put("/akun/{akun_id}/iklan/agen/batas")
async def simpan_batas(akun_id: str, body: agen_guardrail.BatasIn, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    """Replace this shop's ads limits. Omitted fields reset to the safe default (autonomy off, 20% per action)."""
    akun = await services.get_akun_marketplace(session, akun_id)
    await pastikan_akses_akun(user, session, akun.id)
    hasil = await agen_guardrail.simpan_batas(session, akun.id, body, user)
    await session.commit()
    return hasil


@router.get("/iklan/agen/stop")
async def baca_stop_global(session: AsyncSession = DB, _: UserMarketplaceErp = ADMIN):
    return await agen_guardrail.baca_stop(session, None)


@router.put("/iklan/agen/stop")
async def simpan_stop_global(body: agen_guardrail.StopIn, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    """Global kill switch. When aktif, every ads-agent write is refused."""
    hasil = await agen_guardrail.simpan_stop(session, None, body, user)
    await session.commit()
    return hasil


@router.get("/akun/{akun_id}/iklan/agen/stop")
async def baca_stop_toko(akun_id: str, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    akun = await services.get_akun_marketplace(session, akun_id)
    await pastikan_akses_akun(user, session, akun.id)
    return await agen_guardrail.baca_stop(session, akun.id)


@router.put("/akun/{akun_id}/iklan/agen/stop")
async def simpan_stop_toko(akun_id: str, body: agen_guardrail.StopIn, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    akun = await services.get_akun_marketplace(session, akun_id)
    await pastikan_akses_akun(user, session, akun.id)
    hasil = await agen_guardrail.simpan_stop(session, akun.id, body, user)
    await session.commit()
    return hasil


@router.get("/akun/{akun_id}/iklan/agen/jadwal")
async def daftar_jadwal(akun_id: str, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    """Allowed WIB hours per campaign. Rows and the background loop are off until turned on."""
    akun = await services.get_akun_marketplace(session, akun_id)
    await pastikan_akses_akun(user, session, akun.id)
    return await agen_jadwal.daftar(session, akun.id)


@router.put("/akun/{akun_id}/iklan/agen/jadwal")
async def simpan_jadwal(akun_id: str, body: agen_jadwal.JadwalIn, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    akun = await services.get_akun_marketplace(session, akun_id)
    await pastikan_akses_akun(user, session, akun.id)
    hasil = await agen_jadwal.simpan(session, akun.id, body)
    await session.commit()
    return hasil


@router.post("/akun/{akun_id}/iklan/agen/aksi")
async def aksi_agen(akun_id: str, body: agen_aksi.AksiIn, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    """Change budget, target ROAS, pause/resume, bids, or keywords (including negative keywords). Dry-run unless ``kering`` is false.

    A real write needs ``persetujuan`` = ``ya`` unless the shop's autonomy flag is on, and always stays inside the caps.
    STOP refuses the call before any Shopee write.
    """
    akun = await services.get_akun_marketplace(session, akun_id)
    await pastikan_akses_akun(user, session, akun.id)
    return await agen_aksi.terapkan(session, akun, body, user)


@router.post("/akun/{akun_id}/iklan/agen/audit/{audit_id}/kembalikan")
async def kembalikan_agen(akun_id: str, audit_id: str, body: agen_aksi.KembaliIn, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    """Replay the audit row's before-state. Needs ``persetujuan`` = ``ya``. STOP blocks it."""
    akun = await services.get_akun_marketplace(session, akun_id)
    await pastikan_akses_akun(user, session, akun.id)
    return await agen_aksi.kembalikan(session, akun, audit_id, body, user)


@router.get("/iklan/agen/audit")
async def audit(
    akun_id: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    session: AsyncSession = DB,
    user: UserMarketplaceErp = ADMIN,
):
    return await agen_guardrail.daftar_audit(session, akun_id=akun_id, diizinkan=await akun_ids_diizinkan(user, session), limit=limit)
