"""Admin-only, explicit Shopee management; no arbitrary endpoint/body proxy."""
import logging
from datetime import date
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.application.management_schemas import ItemEdit, ModelsEdit, TiersEdit, GmvCreate, GmvEdit, GmvItems
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee_management as adapter, erp_shopee_insights as insights
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import require_roles_marketplace_erp, pastikan_akses_akun
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import UserMarketplaceErp

logger = logging.getLogger(__name__)
router = APIRouter()
DB = Depends(get_db_marketplace_erp)
ADMIN = Depends(require_roles_marketplace_erp("admin"))


async def account(session, user, akun_id):
    await pastikan_akses_akun(user, session, akun_id)
    return await services.akun_shopee_pengelolaan(session, akun_id)


async def listing(session, user, ident):
    katalog, _ = await services.get_katalog_shopee(session, ident)
    return katalog, await account(session, user, katalog.akun_id)


async def refreshed(session, akun, katalog, result):
    try:
        snapshot = await adapter.provider.ambil_satu_produk(session, akun, int(katalog.item_id))
        await services.simpan_katalog_shopee(session, akun, [snapshot], lengkap=False)
        await session.commit()
        result["snapshot_diperbarui"] = True
    except Exception:
        logger.exception("Confirmed Shopee product update could not refresh local catalogue")
        # A confirmed provider write must not be reported as a failed write.
        await session.rollback()
        result["snapshot_diperbarui"] = False
        result["warnings"].append("Perubahan berhasil, tetapi katalog belum diperbarui. Sinkronkan produk ini sebelum mengubah lagi.")
    return result


@router.get("/katalog-shopee/{id}/pengaturan")
async def settings(id: str, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    k, a = await listing(session, user, id)
    parent = await adapter.item(session, a, k.item_id)
    fields = {key: parent.get(key) for key in ("item_name", "item_sku", "description", "category_id", "attribute_list", "brand", "image", "weight", "dimension", "pre_order")}
    variants = await adapter.workflows.read(session, a, "/api/v2/product/get_model_list", {"item_id": int(k.item_id)}) if parent.get("has_model") else {}
    fields["models"] = [{**r, "model_id": str(r["model_id"])} for r in variants.get("model", [])]
    fields["tiers"] = variants.get("tier_variation", [])
    return fields


@router.get("/katalog-shopee/{id}/diagnosis")
async def diagnosis(id: str, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    k, a = await listing(session, user, id)
    return await insights.diagnosis(session, a, [int(k.item_id)])


@router.patch("/katalog-shopee/{id}/informasi")
async def edit_item(id: str, body: ItemEdit, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    k, a = await listing(session, user, id)
    return await refreshed(session, a, k, await adapter.update_item(session, a, k.item_id, body))


@router.patch("/katalog-shopee/{id}/model")
async def edit_models(id: str, body: ModelsEdit, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    k, a = await listing(session, user, id)
    return await refreshed(session, a, k, await adapter.update_variants(session, a, k.item_id, body))


@router.patch("/katalog-shopee/{id}/pilihan-varian")
async def edit_tiers(id: str, body: TiersEdit, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    k, a = await listing(session, user, id)
    return await refreshed(session, a, k, await adapter.update_variants(session, a, k.item_id, body, tiers=True))


@router.get("/akun/{id}/penalti")
async def penalties(id: str, halaman: int = Query(1, ge=1), ukuran: int = Query(25, ge=1, le=100), session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    return await insights.penalties(session, await account(session, user, id), halaman, ukuran)


@router.get("/akun/{id}/gmv-max/kelayakan")
async def eligibility(id: str, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    return await adapter.eligibility(session, await account(session, user, id))


@router.post("/akun/{id}/gmv-max")
async def create_gmv(id: str, body: GmvCreate, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    return await adapter.gmv_mutation(session, await account(session, user, id), body, "create")


@router.patch("/akun/{id}/gmv-max")
async def edit_gmv(id: str, body: GmvEdit, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    return await adapter.gmv_mutation(session, await account(session, user, id), body, "edit")


@router.post("/akun/{id}/gmv-max/produk")
async def gmv_items(id: str, body: GmvItems, session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    return await adapter.gmv_mutation(session, await account(session, user, id), body, "items")


@router.get("/akun/{id}/gmv-max/performa")
async def performance(id: str, mulai: date, selesai: date, campaign_id: int = Query(gt=0), per_produk: bool = False, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), session: AsyncSession = DB, user: UserMarketplaceErp = ADMIN):
    return await adapter.gmv_performance(session, await account(session, user, id), campaign_id, mulai, selesai, offset, limit, per_produk)
