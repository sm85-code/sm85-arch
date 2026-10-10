"""Account-scoped Shopee commerce; credentials and provider paths stay on the server."""
import json
import logging
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.commerce_schemas import AddressConfig, ChannelEdit, HolidayEdit, ModelAdd, ModelsInit, OrderNote, ShopProfileEdit
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee_commerce as adapter
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import pastikan_akses_akun, require_roles_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import ProdukListing, UserMarketplaceErp

from .management_router import account, listing, refreshed

logger = logging.getLogger(__name__)
router = APIRouter()
DB = Depends(get_db_marketplace_erp)
ADMIN = Depends(require_roles_marketplace_erp("admin"))
STAFF = Depends(require_roles_marketplace_erp("admin", "owner", "staff"))


async def save_confirmed(session, result):
    try:
        await session.commit()
    except Exception:
        logger.exception("Confirmed Shopee change could not persist the local snapshot")
        await session.rollback()
        result["warnings"].append("Perubahan dikonfirmasi Shopee, tetapi data ERP belum diperbarui. Refresh atau sinkronkan data sebelum mengubah lagi.")
    return result


async def order(session, user, ident):
    row = await services.get_pesanan(session, ident)
    if row.platform != "shopee" or not row.akun_id or not row.status_marketplace:
        raise HTTPException(409, "Pesanan ini tidak terhubung ke Shopee")
    await pastikan_akses_akun(user, session, row.akun_id)
    return row, await services.akun_shopee_pengelolaan(session, row.akun_id)


@router.delete("/katalog-shopee/{id}/shopee")
async def delete_product(id: str, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    k, a = await listing(session, user, id)
    result = await adapter.delete_product(session, a, k.item_id)
    # Keep historical references and stock; only mark the removed marketplace listing.
    k.status = "SELLER_DELETE"
    rows = (await session.execute(select(ProdukListing).where(ProdukListing.akun_id == a.id, ProdukListing.platform == "shopee"))).scalars()
    for row in rows:
        if row.id_eksternal.split(":")[0] == str(k.item_id):
            row.aktif = False
    return await save_confirmed(session, result)


@router.post("/katalog-shopee/{id}/varian")
async def add_models(id: str, body: ModelAdd, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    k, a = await listing(session, user, id)
    return await refreshed(session, a, k, await adapter.add_models(session, a, k.item_id, body))


@router.post("/katalog-shopee/{id}/inisialisasi-varian")
async def init_models(id: str, body: ModelsInit, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    k, a = await listing(session, user, id)
    return await refreshed(session, a, k, await adapter.init_models(session, a, k.item_id, body))


@router.delete("/katalog-shopee/{id}/varian/{model_id}")
async def delete_model(id: str, model_id: int = Path(gt=0), session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    k, a = await listing(session, user, id)
    return await refreshed(session, a, k, await adapter.delete_model(session, a, k.item_id, model_id))


@router.get("/katalog-shopee/{id}/informasi-shopee/{jenis}")
async def product_details(id: str, jenis: Literal["promosi", "pelanggaran"], session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    k, a = await listing(session, user, id)
    return await adapter.product_details(session, a, k.item_id, jenis)


@router.get("/pesanan/{id}/pelacakan")
async def tracking(id: str, package_number: str | None = Query(None, min_length=1, max_length=100), session: AsyncSession = DB, user: UserMarketplaceErp = STAFF):
    p, a = await order(session, user, id)
    return await adapter.tracking(session, a, p.id_eksternal, package_number)


@router.patch("/pesanan/{id}/catatan-shopee")
async def note(id: str, body: OrderNote, session: AsyncSession = DB, user: UserMarketplaceErp = STAFF):
    p, a = await order(session, user, id)
    result = await adapter.acknowledged(session, a, "/api/v2/order/set_note", {"order_sn": p.id_eksternal, "note": body.note})
    details = json.loads(p.detail_json or "{}")
    details["note"] = body.note
    p.detail_json = json.dumps(details)
    return await save_confirmed(session, result)


@router.get("/pesanan/{id}/pendapatan-shopee")
async def order_income(id: str, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    p, a = await order(session, user, id)
    return await adapter.order_income(session, a, p.id_eksternal)


@router.get("/akun/{id}/pendapatan-shopee")
async def income(id: str, dari: date, sampai: date, income_status: Literal[1, 2] = 2, cursor: str = Query("", max_length=500), page_size: int = Query(30, ge=1, le=100), session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    return await adapter.income(session, await account(session, user, id), dari, sampai, income_status, cursor, page_size)


@router.get("/akun/{id}/iklan/performa-jam")
async def hourly_ads(id: str, tanggal: date, campaign_id: int | None = Query(None, gt=0), session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    return await adapter.hourly_ads(session, await account(session, user, id), tanggal, campaign_id)


@router.get("/akun/{id}/pengaturan-shopee/{bagian}")
async def settings(id: str, bagian: Literal["profil", "libur", "jasa-kirim", "alamat"], session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    return await adapter.settings_read(session, await account(session, user, id), bagian)


@router.patch("/akun/{id}/pengaturan-shopee/profil")
async def profile(id: str, body: ShopProfileEdit, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    a = await account(session, user, id)
    result = await adapter.acknowledged(session, a, "/api/v2/shop/update_profile", body.model_dump(mode="json", exclude_none=True))
    if body.shop_name is not None:
        a.nama_toko = body.shop_name
        return await save_confirmed(session, result)


@router.put("/akun/{id}/pengaturan-shopee/libur")
async def holiday(id: str, body: HolidayEdit, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    return await adapter.acknowledged(session, await account(session, user, id), "/api/v2/shop/set_shop_holiday_mode", body.model_dump(mode="json", exclude_none=True))


@router.patch("/akun/{id}/pengaturan-shopee/jasa-kirim/{channel_id}")
async def channel(id: str, body: ChannelEdit, channel_id: int = Path(gt=0), session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    return await adapter.update_channel(session, await account(session, user, id), channel_id, body)


@router.put("/akun/{id}/pengaturan-shopee/alamat")
async def address_config(id: str, body: AddressConfig, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    return await adapter.address_config(session, await account(session, user, id), body)
