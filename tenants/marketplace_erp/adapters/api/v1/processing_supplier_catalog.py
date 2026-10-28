"""Admin-only connection to the read-only supplier catalog."""

import os
from decimal import Decimal, InvalidOperation

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import require_roles_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import ProcessingSupplierConnection
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.token_crypto import keyring

router = APIRouter(dependencies=[Depends(require_roles_marketplace_erp("admin"))])
CATALOG_PATH = "/api/v1/uu05_inventory/supplier-catalog"


def source_url():
    return os.getenv("SIABUMDES_API_URL", "https://api-siabumdes.ampelkuning.com").rstrip("/")


def catalog_http_client(timeout=20):
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)


class CatalogLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=255)
    password: SecretStr = Field(min_length=1, max_length=1000)


@router.get("/supplier-catalog/connection")
async def connection_status(session: AsyncSession = Depends(get_db_marketplace_erp)):
    row = await session.get(ProcessingSupplierConnection, "siabumdes")
    return {
        "connected": bool(row),
        "unit_name": row.unit_name if row else None,
        "expires_at": row.expires_at if row else None,
    }


@router.post("/supplier-catalog/connect")
async def connect(payload: CatalogLogin, session: AsyncSession = Depends(get_db_marketplace_erp)):
    try:
        keyring()
    except RuntimeError:
        raise HTTPException(503, "Penyimpanan koneksi belum tersedia")
    try:
        async with catalog_http_client() as client:
            login = await client.post(
                source_url() + "/api/auth/login",
                json={
                    "username": payload.username,
                    "password": payload.password.get_secret_value(),
                },
            )
            if login.status_code != 200:
                raise HTTPException(422, "Login katalog gagal. Gunakan akun admin sumber katalog.")
            result = await client.post(source_url() + CATALOG_PATH + "/access", json={})
            if result.status_code != 200:
                raise HTTPException(409, "Akses katalog belum tersedia untuk akun ini")
            data = result.json()
            token = data["token"]
            if not isinstance(token, str) or not token:
                raise ValueError("Invalid credential")
    except (httpx.HTTPError, ValueError, KeyError):
        raise HTTPException(502, "Sumber katalog belum dapat dihubungi")
    row = await session.get(ProcessingSupplierConnection, "siabumdes")
    if row is None:
        row = ProcessingSupplierConnection(id="siabumdes")
        session.add(row)
    row.token, row.unit_id, row.unit_name, row.expires_at = (
        token,
        data["unit_id"],
        data["unit_name"],
        data["expires_at"],
    )
    await session.commit()
    return {"connected": True, "unit_name": row.unit_name}


async def fetch_supplier_catalog(session):
    row = await session.get(ProcessingSupplierConnection, "siabumdes")
    if not row:
        raise HTTPException(409, "Hubungkan katalog terlebih dahulu")
    try:
        async with catalog_http_client() as client:
            response = await client.get(source_url() + CATALOG_PATH, headers={"X-ERP-Catalog-Key": row.token})
        if response.status_code in [401, 403]:
            raise HTTPException(409, "Koneksi katalog berakhir. Hubungkan ulang.")
        response.raise_for_status()
        data = response.json()
        if data.get("unit_id") != row.unit_id or not isinstance(data.get("items"), list):
            raise ValueError("Catalog unit mismatch")
        for p in data["items"]:
            if (
                not isinstance(p.get("id"), str)
                or not isinstance(p.get("sku"), str)
                or not isinstance(p.get("name"), str)
            ):
                raise ValueError("Invalid product")
            price = Decimal(str(p["sell_price"]))
            if not price.is_finite() or price < 0 or not isinstance(p["qty_on_hand"], int):
                raise ValueError("Invalid price/stock")
        return data
    except (httpx.HTTPError, ValueError, KeyError, TypeError, InvalidOperation):
        raise HTTPException(502, "Katalog otomatis belum dapat dibaca. Harga dan stok tidak dianggap nol.")


@router.get("/supplier-catalog")
async def supplier_catalog(session: AsyncSession = Depends(get_db_marketplace_erp)):
    return await fetch_supplier_catalog(session)


@router.delete("/supplier-catalog/connection")
async def disconnect(session: AsyncSession = Depends(get_db_marketplace_erp)):
    row = await session.get(ProcessingSupplierConnection, "siabumdes")
    if row:
        try:
            async with catalog_http_client(10) as client:
                await client.delete(source_url() + CATALOG_PATH + "/access", headers={"X-ERP-Catalog-Key": row.token})
        except httpx.HTTPError:
            pass
        await session.delete(row)
        await session.commit()
    return {"connected": False}
