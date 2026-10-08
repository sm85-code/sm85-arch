"""Resumable read-only synchronization from the user's current workspace."""

import json
import hashlib
import logging
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee as provider
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import (
    akun_ids_diizinkan,
    pastikan_akses_akun,
    require_roles_marketplace_erp,
)
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    SinkronMarketplace,
    Pesanan,
    KatalogShopee,
    ProdukListing,
    UserMarketplaceErp,
)

_log = logging.getLogger(__name__)
router = APIRouter()
actor = require_roles_marketplace_erp("admin", "owner", "staff")


class SyncIn(BaseModel):
    jenis: Literal["pesanan", "katalog", "produk", "listing"]
    ids: list[str] = Field(default_factory=list, max_length=200)
    akun_id: str | None = None


def output(job):
    state = json.loads(job.state_json)
    return {
        "id": job.id,
        "status": job.status,
        "selesai": state["selesai"],
        "tersisa": len(state["queue"]),
        "gagal": state["gagal"],
        "cakupan": state.get("cakupan", {}),
    }


@router.post("/sinkronisasi")
async def create(
    body: SyncIn, session: AsyncSession = Depends(get_db_marketplace_erp), user: UserMarketplaceErp = Depends(actor)
):
    if body.jenis != "pesanan" and user.role not in ("admin", "owner"):
        raise HTTPException(403, "Sinkronisasi produk hanya untuk owner/admin")
    allowed = await akun_ids_diizinkan(user, session)
    accounts = [
        a
        for a in await services.list_akun_marketplace(session)
        if (allowed is None or a.id in allowed) and a.platform == "shopee" and a.id_toko_eksternal
    ]
    if body.akun_id:
        await pastikan_akses_akun(user, session, body.akun_id)
        accounts = [a for a in accounts if a.id == body.akun_id]
    permitted = {a.id for a in accounts}
    queue = []
    if body.ids:
        model = {"pesanan": Pesanan, "katalog": KatalogShopee, "listing": ProdukListing, "produk": ProdukListing}[
            body.jenis
        ]
        field = model.produk_id if body.jenis == "produk" else model.id
        rows = (await session.execute(select(model).where(field.in_(body.ids)))).scalars().all()
        found = set()
        for row in rows:
            if row.akun_id not in permitted:
                continue
            found.add(row.produk_id if body.jenis == "produk" else row.id)
            external = row.id_eksternal if body.jenis != "katalog" else row.item_id
            if body.jenis != "pesanan":
                external = external.split(":")[0]
                if not external.isdigit():
                    raise HTTPException(422, "Mapping produk Shopee tidak valid")
            queue.append(
                {"akun_id": row.akun_id, "kind": "order" if body.jenis == "pesanan" else "item", "external": external}
            )
        if found != set(body.ids):
            raise HTTPException(422, "Ada pilihan tanpa mapping Shopee atau di luar toko yang diizinkan")
        queue = list({(q["akun_id"], q["kind"], q["external"]): q for q in queue}.values())
    else:
        now = int(time.time())
        for a in accounts:
            queue.append(
                {
                    "akun_id": a.id,
                    "kind": "orders" if body.jenis == "pesanan" else "items",
                    "cursor": "",
                    "offset": 0,
                    "from": now - 15 * 86400,
                    "to": now,
                }
            )
    if not queue:
        raise HTTPException(422, "Tidak ada toko Shopee terhubung untuk cakupan ini")
    scope = {"jenis": body.jenis, "ids": sorted(set(body.ids)), "akun_id": body.akun_id}
    active_key = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    stmt = select(SinkronMarketplace).where(
        SinkronMarketplace.user_id == user.id, SinkronMarketplace.active_key == active_key
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing:
        return output(existing)
    job = SinkronMarketplace(
        user_id=user.id,
        active_key=active_key,
        state_json=json.dumps({"queue": queue, "selesai": 0, "gagal": [], "cakupan": scope}),
    )
    session.add(job)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            raise
        return output(existing)
    return output(job)


async def owned(session, user, job_id, lock=False):
    stmt = (
        select(SinkronMarketplace)
        .execution_options(populate_existing=True)
        .where(SinkronMarketplace.id == job_id, SinkronMarketplace.user_id == user.id)
    )
    if lock:
        stmt = stmt.with_for_update(skip_locked=True)
    job = (await session.execute(stmt)).scalar_one_or_none()
    if job is None:
        raise HTTPException(409 if lock else 404, "Sinkronisasi sedang diproses atau tidak ditemukan")
    return job


@router.get("/sinkronisasi/{job_id}")
async def get(
    job_id: str, session: AsyncSession = Depends(get_db_marketplace_erp), user: UserMarketplaceErp = Depends(actor)
):
    return output(await owned(session, user, job_id))


@router.post("/sinkronisasi/{job_id}/lanjut")
async def step(
    job_id: str, session: AsyncSession = Depends(get_db_marketplace_erp), user: UserMarketplaceErp = Depends(actor)
):
    job = await owned(session, user, job_id)
    state = json.loads(job.state_json)
    if not state["queue"]:
        return output(job)
    unit = state["queue"][0]
    await pastikan_akses_akun(user, session, unit["akun_id"])
    akun = await services.akun_shopee_pengelolaan(session, unit["akun_id"], "sinkronisasi")
    await provider.pastikan_token_segar(session, akun)
    job = await owned(session, user, job_id, lock=True)
    state = json.loads(job.state_json)
    if not state["queue"] or state["queue"][0] != unit:
        return output(job)
    added = []
    try:
        async with session.begin_nested():
            kind = unit["kind"]
            if kind == "order":
                data = await provider.signed_shop_request(
                    session,
                    akun,
                    provider._PATH_ORDER_DETAIL,
                    params={
                        "order_sn_list": unit["external"],
                        "response_optional_fields": provider._ORDER_DETAIL_FIELDS,
                    },
                    timeout=15,
                )
                rows = (data.get("response") or {}).get("order_list") or []
                if len(rows) != 1 or str(rows[0].get("order_sn")) != unit["external"]:
                    raise HTTPException(424, "Detail pesanan belum lengkap")
                await services.impor_pesanan_marketplace(session, akun, [provider.normalisasi_pesanan(rows[0])])
            elif kind == "item":
                snapshot = await provider.ambil_satu_produk(session, akun, int(unit["external"]), timeout=15)
                await services.simpan_katalog_shopee(session, akun, [snapshot], lengkap=False)
            elif kind == "orders":
                params = {
                    "time_range_field": "update_time",
                    "time_from": unit["from"],
                    "time_to": unit["to"],
                    "page_size": 50,
                }
                if unit["cursor"]:
                    params["cursor"] = unit["cursor"]
                data = await provider.signed_shop_request(
                    session, akun, provider._PATH_ORDER_LIST, params=params, timeout=15
                )
                resp = data.get("response") or {}
                if not isinstance(resp.get("order_list"), list):
                    raise HTTPException(424, "Daftar pesanan Shopee belum lengkap")
                added = [
                    {"akun_id": akun.id, "kind": "order", "external": str(o["order_sn"])}
                    for o in resp.get("order_list") or []
                ]
                if resp.get("more"):
                    cursor = resp.get("next_cursor")
                    if (
                        not cursor
                        or cursor in [unit["cursor"], *unit.get("seen", [])]
                        or len(unit.get("seen", [])) >= 100
                    ):
                        raise HTTPException(424, "Pagination Shopee tidak bergerak")
                    added.append({**unit, "cursor": cursor, "seen": [*unit.get("seen", []), unit["cursor"]]})
            else:
                data = await provider.signed_shop_request(
                    session,
                    akun,
                    provider._PATH_ITEM_LIST,
                    params={"offset": unit["offset"], "page_size": 50, "item_status": list(provider.STATUS_KATALOG)},
                    timeout=15,
                )
                resp = data.get("response") or {}
                if not isinstance(resp.get("item"), list):
                    raise HTTPException(424, "Daftar produk Shopee belum lengkap")
                added = [
                    {"akun_id": akun.id, "kind": "item", "external": str(o["item_id"])} for o in resp.get("item") or []
                ]
                if resp.get("has_next_page"):
                    offset = int(resp.get("next_offset") or 0)
                    if offset <= unit["offset"] or offset > 100000:
                        raise HTTPException(424, "Pagination Shopee tidak bergerak")
                    added.append({**unit, "offset": offset})
    except Exception as exc:
        # Reads can be retried explicitly; retain each failed unit with its target.
        _log.exception("marketplace_sync_failed job=%s account=%s kind=%s", job.id, akun.id, unit["kind"])
        message = (
            str(exc.detail)
            if isinstance(exc, HTTPException)
            else "Gagal membaca Shopee; coba kembali atau periksa log server"
        )
        added = []
        state["gagal"].append({"unit": unit, "pesan": message})
    else:
        state["selesai"] += 1
    state["queue"] = state["queue"][1:] + added
    if not state["queue"]:
        job.status = "sebagian" if state["gagal"] else "selesai"
        if not state["gagal"]:
            job.active_key = None
    job.state_json = json.dumps(state)
    await session.flush()
    return output(job)


@router.post("/sinkronisasi/{job_id}/ulangi-gagal")
async def retry(
    job_id: str, session: AsyncSession = Depends(get_db_marketplace_erp), user: UserMarketplaceErp = Depends(actor)
):
    job = await owned(session, user, job_id, lock=True)
    state = json.loads(job.state_json)
    if state["queue"]:
        raise HTTPException(409, "Selesaikan antrean sebelum mengulang yang gagal")
    state["queue"] = [failure["unit"] for failure in state["gagal"]]
    state["gagal"] = []
    job.status = "berjalan" if state["queue"] else "selesai"
    job.state_json = json.dumps(state)
    return output(job)
