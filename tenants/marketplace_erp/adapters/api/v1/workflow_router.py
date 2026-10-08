"""Explicit, account-scoped endpoints. No arbitrary Shopee proxy or browser-supplied signatures."""

from datetime import date
from fastapi import APIRouter, Depends, File, HTTPException, Path, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from tenants.marketplace_erp.modules.marketplace_erp.application import services, workflows
from tenants.marketplace_erp.modules.marketplace_erp.application.workflow_schemas import PublishIn, DisputeIn
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import (
    pastikan_akses_akun,
    require_roles_marketplace_erp,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import UserMarketplaceErp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import (
    erp_shopee_workflows as adapter,
    erp_shopee_upload as media,
    erp_shopee_returns,
)

router = APIRouter()
admin = require_roles_marketplace_erp("admin")
owner = require_roles_marketplace_erp("admin", "owner")
staff = require_roles_marketplace_erp("admin", "owner", "staff")


async def account(session, user, akun_id, fitur):
    await pastikan_akses_akun(user, session, akun_id)
    return await services.akun_shopee_pengelolaan(session, akun_id, fitur)


@router.get("/akun/{akun_id}/publikasi/metadata")
async def metadata(
    akun_id: str,
    category_id: int | None = Query(None, gt=0),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(owner),
):
    return await adapter.metadata(session, await account(session, user, akun_id, "publikasi"), category_id)


@router.get("/akun/{akun_id}/publikasi/merek")
async def brands(
    akun_id: str,
    category_id: int = Query(gt=0),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(owner),
):
    return await adapter.read(
        session,
        await account(session, user, akun_id, "publikasi"),
        "/api/v2/product/get_brand_list",
        {"category_id": category_id, "status": 1, "offset": offset, "page_size": 100},
    )


@router.get("/akun/{akun_id}/publikasi/sumber/{item_id}")
async def source(
    akun_id: str,
    item_id: str = Path(pattern=r"^[1-9][0-9]{0,18}$"),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(owner),
):
    return await adapter.copy_draft(session, await account(session, user, akun_id, "salin produk"), int(item_id))


@router.post("/akun/{akun_id}/publikasi/foto")
async def upload_photo(
    akun_id: str,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(owner),
):
    akun = await account(session, user, akun_id, "foto produk")
    try:
        return await media.upload(session, akun, await file.read(media.MAX_IMAGE + 1))
    finally:
        await file.close()


@router.post("/akun/{akun_id}/publikasi")
async def publish(
    akun_id: str,
    payload: PublishIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(owner),
):
    await account(session, user, akun_id, "publikasi")
    return await workflows.publish(session, akun_id, payload)


@router.get("/akun/{akun_id}/publikasi/hasil/{operation_id}")
async def publication_result(
    akun_id: str,
    operation_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(owner),
):
    await account(session, user, akun_id, "publikasi")
    record = await workflows.receipt(session, akun_id, operation_id)
    if not record:
        raise HTTPException(status_code=404, detail="Operasi publikasi belum tercatat.")
    return workflows.result_from_record(record)


@router.get("/akun/{akun_id}/retur/{sn}/alasan-sengketa")
async def reasons(
    akun_id: str,
    sn: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(staff),
):
    return await adapter.dispute_reasons(session, await account(session, user, akun_id, "sengketa retur"), sn)


@router.post("/akun/{akun_id}/retur/{sn}/bukti")
async def upload_evidence(
    akun_id: str,
    sn: str,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(staff),
):
    akun = await account(session, user, akun_id, "sengketa retur")
    await erp_shopee_returns.detail_retur(session, akun, sn)
    try:
        return await media.upload(session, akun, await file.read(media.MAX_IMAGE + 1), nomor_retur=sn)
    finally:
        await file.close()


@router.post("/akun/{akun_id}/retur/{sn}/sengketa")
async def dispute(
    akun_id: str,
    sn: str,
    payload: DisputeIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(staff),
):
    akun = await account(session, user, akun_id, "sengketa retur")
    result = await adapter.dispute(session, akun, sn, payload)
    try:
        result["retur"] = await services.detail_retur_marketplace(session, akun_id, sn)
    except HTTPException as exc:
        result["warnings"].append(f"Sengketa diterima; detail belum diperbarui: {exc.detail}")
    return result


@router.get("/akun/{akun_id}/transaksi-dana")
async def wallet(
    akun_id: str,
    dari: date,
    sampai: date,
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(admin),
):
    await account(session, user, akun_id, "transaksi dana")
    return await workflows.wallet(session, akun_id, dari, sampai, offset)
