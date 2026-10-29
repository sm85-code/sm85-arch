"""Read-only surface for the ads agent. Writes are added behind guardrails in later changes."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import agen_ringkasan, services
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import pastikan_akses_akun, require_roles_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import UserMarketplaceErp

router = APIRouter(tags=["Agen Iklan"])


@router.get("/akun/{akun_id}/iklan/agen/ringkasan")
async def ringkasan_iklan_agen(
    akun_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(require_roles_marketplace_erp("admin")),
):
    """Compact ads snapshot for one shop: campaigns, products, keywords, hourly performance, balance, shop ad toggles.

    Read only. Auto product-ad endpoints are listed under ``api_usang``; prefer GMV Max or manual ads.
    """
    akun = await services.get_akun_marketplace(session, akun_id)
    await pastikan_akses_akun(user, session, akun.id)
    if akun.platform != "shopee":
        raise HTTPException(status_code=409, detail="API iklan ini khusus toko Shopee.")
    return await agen_ringkasan.ringkasan_toko(session, akun)
