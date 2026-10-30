"""Page permissions for Processing; no fixed-role fallback for this scope."""

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import akses, peta_akses
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import get_current_user_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import UserMarketplaceErp


async def processing_access(
    request: Request,
    user: UserMarketplaceErp = Depends(get_current_user_marketplace_erp),
    session: AsyncSession = Depends(get_db_marketplace_erp),
) -> UserMarketplaceErp:
    aturan = peta_akses.cari(request.url.path, request.method)
    if aturan is None:
        raise HTTPException(403, "Akses ditolak")
    halaman, perlu = aturan
    punya = await akses.tingkat_halaman(session, await akses.peran_pengguna(session, user), halaman)
    if punya is None or (perlu == "tulis" and punya != "tulis"):
        raise HTTPException(403, "Akses ditolak")
    return user
