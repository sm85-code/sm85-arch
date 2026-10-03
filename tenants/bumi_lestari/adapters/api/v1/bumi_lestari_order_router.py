"""HTTP surface for bumi_lestari Tahap 2 -- katalog, pemasok, saluran, pelanggan, order (owner/admin)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application import order_services as svc
from tenants.bumi_lestari.modules.bumi_lestari.application.schemas_order import (
    HargaGrosirIn,
    HargaGrosirOut,
    OrderIn,
    OrderOut,
    OrderPatch,
    OrderStatusIn,
    PelangganIn,
    PelangganOut,
    PemasokIn,
    PemasokOut,
    ProdukIn,
    ProdukOut,
    ProdukPatch,
    SaluranIn,
    SaluranOut,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import require_roles_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser

order_router = APIRouter()
OWNER_UP = ("admin", "owner")


def _guard():
    return Depends(require_roles_bumi_lestari(*OWNER_UP))


def _db():
    return Depends(get_db_bumi_lestari)


@order_router.get("/produk", response_model=list[ProdukOut])
async def list_produk(jenis_produk: str | None = None, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.list_produk(session, jenis_produk)


@order_router.post("/produk", response_model=ProdukOut, status_code=status.HTTP_201_CREATED)
async def create_produk(payload: ProdukIn, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.create_produk(session, payload)


@order_router.patch("/produk/{produk_id}", response_model=ProdukOut)
async def update_produk(produk_id: str, payload: ProdukPatch, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.update_produk(session, produk_id, payload)


@order_router.get("/pemasok", response_model=list[PemasokOut])
async def list_pemasok(session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.list_pemasok(session)


@order_router.post("/pemasok", response_model=PemasokOut, status_code=status.HTTP_201_CREATED)
async def create_pemasok(payload: PemasokIn, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.create_pemasok(session, payload)


@order_router.get("/saluran", response_model=list[SaluranOut])
async def list_saluran(session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.list_saluran(session)


@order_router.post("/saluran", response_model=SaluranOut, status_code=status.HTTP_201_CREATED)
async def create_saluran(payload: SaluranIn, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.create_saluran(session, payload)


@order_router.get("/pelanggan", response_model=list[PelangganOut])
async def list_pelanggan(session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.list_pelanggan(session)


@order_router.post("/pelanggan", response_model=PelangganOut, status_code=status.HTTP_201_CREATED)
async def create_pelanggan(payload: PelangganIn, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.create_pelanggan(session, payload)


@order_router.get("/harga-grosir", response_model=list[HargaGrosirOut])
async def list_harga_grosir(pelanggan_id: str | None = None, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.list_harga_grosir(session, pelanggan_id)


@order_router.put("/harga-grosir", response_model=HargaGrosirOut)
async def set_harga_grosir(payload: HargaGrosirIn, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.set_harga_grosir(session, payload)


@order_router.get("/order", response_model=list[OrderOut])
async def list_order(
    status_order: str | None = None,
    saluran_id: str | None = None,
    pelanggan_id: str | None = None,
    jenis_produk: str | None = None,
    dari: date | None = None,
    sampai: date | None = None,
    session: AsyncSession = _db(),
    _: BlUser = _guard(),
):
    return await svc.list_order(
        session, status_order=status_order, saluran_id=saluran_id, pelanggan_id=pelanggan_id,
        jenis_produk=jenis_produk, dari=dari, sampai=sampai,
    )


@order_router.post("/order", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
async def create_order(payload: OrderIn, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.create_order(session, payload)


@order_router.get("/order/{order_id}", response_model=OrderOut)
async def get_order(order_id: str, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.get_order(session, order_id)


@order_router.patch("/order/{order_id}", response_model=OrderOut)
async def update_order(order_id: str, payload: OrderPatch, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.update_order(session, order_id, payload)


@order_router.post("/order/{order_id}/status", response_model=OrderOut)
async def ubah_status(order_id: str, payload: OrderStatusIn, session: AsyncSession = _db(), _: BlUser = _guard()):
    return await svc.ubah_status_order(session, order_id, payload)
