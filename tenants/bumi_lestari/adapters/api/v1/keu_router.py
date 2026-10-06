"""Authenticated BUMI keu API; mounted under /api/bumi-lestari/keu."""
from __future__ import annotations

from datetime import date
from typing import Literal
from pydantic import AwareDatetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application import keu_import, keu_services as svc, keu_sync, schemas_keu as sc
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_keu as m
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import require_roles_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlKategori, BlUser


class SafeWriteRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            try:
                return await original(request)
            except IntegrityError:
                # Never expose SQL, table names, input parameters or provider secrets.
                raise HTTPException(409, "Data sudah digunakan atau melanggar aturan pencatatan") from None
        return handle


async def finance_user(user: BlUser = Depends(require_roles_bumi_lestari("admin", "owner"))):
    if user.must_change_password:
        raise HTTPException(403, "Ganti kata sandi bawaan terlebih dahulu")
    return user


router = APIRouter(prefix="/keu", route_class=SafeWriteRoute, dependencies=[Depends(finance_user)])
DB = Depends(get_db_bumi_lestari)
ACTOR = Depends(finance_user)


@router.get("/dashboard")
async def dashboard(session: AsyncSession = DB):
    return await svc.dashboard(session)


@router.get("/sumber")
async def sources():
    return await keu_sync.source_options()


@router.get("/kategori")
async def categories(session: AsyncSession = DB):
    return [svc.record(row) for row in (await session.execute(select(BlKategori).where(BlKategori.aktif.is_(True)).order_by(BlKategori.nama))).scalars()]


def register_master(name, model, schema):
    async def listing(session: AsyncSession = DB, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), search: str | None = Query(None, max_length=128)):
        return await svc.page(session, model, limit, offset, search)

    async def create(payload, session: AsyncSession = DB, actor: BlUser = ACTOR):
        if name == "saluran":
            if payload.sistem == "store" and payload.akun_ref != "store":
                svc.bad("Referensi saluran Store harus store")
            if payload.sistem == "marketplace_erp":
                options = await keu_sync.source_options()
                if payload.akun_ref not in {row["id"] for row in options["erp"]}:
                    svc.bad("Akun ERP tidak ditemukan")
        return await svc.create_master(session, actor, name, payload)
    create.__annotations__["payload"] = schema
    router.add_api_route(f"/{name}", listing, methods=["GET"], response_model=sc.PageOut, name=f"keu-list-{name}")
    router.add_api_route(f"/{name}", create, methods=["POST"], status_code=201, name=f"keu-create-{name}")


for _name, _schema in (("saluran", sc.SaluranIn), ("akun", sc.AkunIn), ("pelanggan", sc.PelangganIn), ("vendor", sc.VendorIn), ("produk", sc.ProdukIn)):
    register_master(_name, svc.MASTERS[_name], _schema)


@router.get("/vendor/{key}")
async def vendor_detail(key: str, session: AsyncSession = DB):
    return svc.record(await svc.get(session, m.KeuVendor, key))


@router.patch("/vendor/{key}")
async def save_vendor(key: str, payload: sc.VendorEditIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
    return await svc.save_vendor(session, actor, key, payload)


@router.delete("/vendor/{key}")
async def deactivate_vendor(key: str, session: AsyncSession = DB, actor: BlUser = ACTOR):
    return await svc.deactivate_vendor(session, actor, key)


@router.patch("/produk/{key}")
async def save_product(key: str, payload: sc.ProdukEditIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
    return await svc.save_product(session, actor, key, payload)


@router.get("/vendor-slot")
async def slots(session: AsyncSession = DB):
    return await svc.slots(session)


@router.put("/vendor-slot/{kode}")
async def change_slot(kode: str, payload: sc.VendorIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
    return await svc.set_slot(session, actor, kode, payload)


def register_list(path, model):
    async def listing(session: AsyncSession = DB, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), search: str | None = Query(None, max_length=128)):
        return await svc.page(session, model, limit, offset, search)

    async def order_listing(session: AsyncSession = DB, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), search: str | None = Query(None, max_length=128), status: Literal["pengerjaan", "batal", "semua"] = Query("pengerjaan")):
        return await svc.page(session, model, limit, offset, search, status)

    router.add_api_route(path, order_listing if model in {m.KeuPesanan, m.KeuItem} else listing,
                        methods=["GET"], response_model=sc.PageOut, name=f"keu-list-{model.__tablename__}")


for _path, _model in (("/pesanan", m.KeuPesanan), ("/item", m.KeuItem), ("/alokasi-vendor", m.KeuAlokasiVendor),
                       ("/settlement", m.KeuSettlement), ("/alokasi-settlement", m.KeuAlokasiSettlement),
                       ("/transaksi", m.KeuTransaksi), ("/impor", m.KeuImpor), ("/masukan", m.KeuMasukan)):
    register_list(_path, _model)


@router.get("/pesanan/{key}")
async def order(key: str, session: AsyncSession = DB):
    return await svc.order_detail(session, key)


@router.post("/pesanan", status_code=201)
async def create_order(payload: sc.PesananIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
    return await svc.create_order(session, actor, payload)


@router.post("/pesanan/{key}/status")
async def status_order(key: str, payload: sc.PesananStatusIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
    return await svc.order_status(session, actor, key, payload)


@router.patch("/item/{key}/produk")
async def map_item(key: str, payload: sc.ItemPetaIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
    return await svc.map_item(session, actor, key, payload)


@router.post("/alokasi-vendor", status_code=201)
async def allocate_vendor(payload: sc.AlokasiVendorIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
    return await svc.allocate_vendor(session, actor, payload)


@router.post("/alokasi-vendor/{key}/batal")
async def cancel_allocation(key: str, payload: sc.BatalIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
    return await svc.cancel_allocation(session, actor, key, payload)


@router.post("/settlement", status_code=201)
async def create_settlement(payload: sc.SettlementIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
    return await svc.create_settlement(session, actor, payload)


@router.post("/alokasi-settlement", status_code=201)
async def allocate_settlement(payload: sc.AlokasiSettlementIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
    return await svc.allocate_settlement(session, actor, payload)


@router.post("/settlement/{key}/posting")
async def post_settlement(key: str, payload: sc.PostingSettlementIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
    return await svc.post_settlement(session, actor, key, payload)


@router.post("/transaksi", status_code=201)
async def create_transaction(payload: sc.TransaksiIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
    return await svc.transaction(session, actor, payload)


@router.post("/transaksi/{key}/posting")
async def post_transaction(key: str, session: AsyncSession = DB, actor: BlUser = ACTOR):
    return await svc.post_transaction(session, actor, key)


@router.get("/impor/template")
async def template(jenis: str = Query(pattern="^(order|settlement|biaya)$")):
    return Response(keu_import.template(jenis), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="keu-{jenis}.csv"'})


@router.post("/impor/pratinjau", status_code=201)
async def preview(file: UploadFile = File(), saluran_id: str = Form(max_length=64), jenis: str = Form(pattern="^(order|settlement|biaya)$"),
                  session: AsyncSession = DB, actor: BlUser = ACTOR):
    content = await file.read(keu_import.MAX_BYTES + 1)
    return await keu_import.preview(session, actor, saluran_id, jenis, file.filename or "", content)


@router.get("/impor/{key}")
async def import_detail(key: str, session: AsyncSession = DB):
    return await keu_import.detail(session, key)


@router.post("/impor/{key}/terapkan")
async def apply_import(key: str, session: AsyncSession = DB, actor: BlUser = ACTOR):
    return await keu_import.apply(session, actor, key)


@router.post("/saluran/{key}/tarik")
async def pull(key: str, entitas: str = Query("order", pattern="^(order|settlement)$"),
               tanggal_awal: date | None = None, tanggal_akhir: date | None = None,
               setelah_at: AwareDatetime | None = None, setelah_ref: str | None = Query(None, min_length=1, max_length=64),
               session: AsyncSession = DB, actor: BlUser = ACTOR):
    return await keu_sync.pull(session, actor, key, entitas, tanggal_awal=tanggal_awal, tanggal_akhir=tanggal_akhir,
                               setelah_at=setelah_at, setelah_ref=setelah_ref)


@router.post("/masukan/{key}/ulang")
async def retry(key: str, session: AsyncSession = DB, actor: BlUser = ACTOR):
    return await keu_sync.retry(session, actor, key)
