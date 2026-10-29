"""Admin-only creation of the ads-agent account. No default password is shipped."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application.agen_pengguna import PenggunaAgenIn, buat
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import require_roles_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp

router = APIRouter(tags=["Agen Iklan"])


@router.post("/users/agen-iklan", status_code=201)
async def buat_pengguna_agen(
    body: PenggunaAgenIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    _=Depends(require_roles_marketplace_erp("admin")),
):
    hasil = await buat(session, body)
    await session.commit()
    return hasil
