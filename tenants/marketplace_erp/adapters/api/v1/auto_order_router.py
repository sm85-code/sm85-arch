from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tenants.marketplace_erp.modules.marketplace_erp.application.workflow_schemas import Input
from tenants.marketplace_erp.modules.marketplace_erp.application import auto_orders
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import AutoOrderPolicy, AutoOrderAttempt, AkunMarketplace, Pesanan, UserMarketplaceErp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import require_roles_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp

router = APIRouter(prefix="/pesanan-otomatis")
DB = Depends(get_db_marketplace_erp)
ADMIN = Depends(require_roles_marketplace_erp("admin"))


class PolicyIn(Input):
    akun_id: str = Field(min_length=1, max_length=64)
    enabled: bool
    branch_id: str | None = Field(default=None, min_length=1, max_length=64)
    sender_real_name: str | None = Field(default=None, min_length=1, max_length=255)


class PoliciesIn(Input):
    items: list[PolicyIn] = Field(min_length=1, max_length=100)


@router.get("")
async def settings(session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    shops = (await session.execute(select(AkunMarketplace).where(AkunMarketplace.platform == "shopee").order_by(AkunMarketplace.nama_toko))).scalars().all()
    rules = {r.akun_id: r for r in (await session.execute(select(AutoOrderPolicy))).scalars()}
    attempts = (await session.execute(select(AutoOrderAttempt, Pesanan.id_eksternal, AkunMarketplace.nama_toko).join(Pesanan, Pesanan.id == AutoOrderAttempt.pesanan_id).join(AkunMarketplace, AkunMarketplace.id == AutoOrderAttempt.akun_id).order_by(AutoOrderAttempt.created_at.desc()).limit(50))).all()
    return {"worker_enabled": auto_orders.erp_shopee.live_sync_enabled(), "interval_seconds": 60,
            "items": [{"akun_id": a.id, "nama_toko": a.nama_toko, "connected": bool(a.access_token and a.id_toko_eksternal and a.status != "nonaktif"),
                       "enabled": rules[a.id].enabled if a.id in rules else False,
                       "enabled_at": rules[a.id].enabled_at if a.id in rules else None,
                       "branch_id": rules[a.id].branch_id if a.id in rules else None,
                       "sender_real_name": rules[a.id].sender_real_name if a.id in rules else None} for a in shops],
            "attempts": [{"pesanan_id": r.pesanan_id, "akun_id": r.akun_id, "order_sn": sn, "nama_toko": name, "status": r.status, "message": r.message, "created_at": r.created_at} for r, sn, name in attempts]}


@router.patch("")
async def save(body: PoliciesIn, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    ids = [i.akun_id for i in body.items]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "Toko tidak boleh duplikat")
    shops = {a.id: a for a in (await session.execute(select(AkunMarketplace).where(AkunMarketplace.id.in_(ids)).order_by(AkunMarketplace.id).with_for_update())).scalars()}
    for item in body.items:
        shop = shops.get(item.akun_id)
        if not shop or shop.platform != "shopee":
            raise HTTPException(422, "Pengaturan hanya untuk toko Shopee")
        if item.enabled and (shop.status == "nonaktif" or not shop.access_token or not shop.id_toko_eksternal):
            raise HTTPException(409, "Hubungkan toko sebelum mengaktifkan otomatis")
    for item in body.items:
        rule = await session.get(AutoOrderPolicy, item.akun_id)
        if rule is None:
            rule = AutoOrderPolicy(akun_id=item.akun_id, enabled=False)
            session.add(rule)
        if item.enabled and not rule.enabled:
            rule.enabled_at = auto_orders.now()
        rule.enabled, rule.enabled_by = item.enabled, user.id
        rule.branch_id, rule.sender_real_name = item.branch_id, item.sender_real_name
    await session.commit()
    return {"ok": True}


@router.post("/{pesanan_id}/sudah-diperiksa")
async def reviewed(pesanan_id: str, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    row = (await session.execute(select(AutoOrderAttempt).where(AutoOrderAttempt.pesanan_id == pesanan_id).with_for_update())).scalar_one_or_none()
    if not row or row.status != "unknown":
        raise HTTPException(409, "Hanya hasil belum pasti yang dapat ditandai sudah diperiksa")
    row.status, row.reviewed_by = "reviewed", user.id
    row.message = "Admin telah memeriksa Seller Centre. Tidak diulang otomatis; lanjutkan manual bila diperlukan."
    await session.commit()
    return {"ok": True}
