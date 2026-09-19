"""UU05 inventory HTTP API (/api/v1/uu05_inventory)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.api.deps import require_roles
from modules.identity.infrastructure.models import User
from modules.siabumdes.infrastructure.models import UnitUsaha
from modules.uu05_inventory.application.services import InventoryService, UU05_CODE
from shared.config import public_role
from shared.database import get_db

router = APIRouter(prefix="/api/v1/uu05_inventory", tags=["uu05_inventory"])

INVENTORY_ROLES = ("admin", "direktur", "bendahara", "pengelola")


async def require_inventory_user(
    user: User = Depends(require_roles(*INVENTORY_ROLES)),
    session: AsyncSession = Depends(get_db),
) -> User:
    if public_role(user.role) != "pengelola":
        return user
    if not user.unit_usaha_id:
        raise HTTPException(status_code=403, detail="Pengelola belum terikat unit usaha")
    unit = await session.get(UnitUsaha, user.unit_usaha_id)
    if not unit or unit.code != UU05_CODE:
        raise HTTPException(status_code=403, detail="Inventory hanya untuk pengelola UU05")
    return user


def card_out(card) -> dict:
    return {
        "id": card.id,
        "reference": card.reference,
        "finance_status": card.finance_status,
        "quantity": card.quantity,
        "total_value": str(card.total_value),
        "direction": card.direction,
        "product_id": card.product_id,
        "movement_date": card.movement_date.isoformat() if card.movement_date else None,
        "unit_cost": str(card.unit_cost),
    }


class StockInBody(BaseModel):
    product_id: str
    quantity: int = Field(gt=0)
    unit_cost: Decimal = Field(ge=0)
    movement_date: date
    unit_usaha_id: Optional[str] = None
    debit_account_code: str
    credit_account_code: str


class StockOutBody(BaseModel):
    product_id: str
    quantity: int = Field(gt=0)
    movement_date: date
    unit_usaha_id: Optional[str] = None
    debit_account_code: str
    credit_account_code: str


class CancelBody(BaseModel):
    stock_card_id: str


class CancelAdjustBody(BaseModel):
    adjustment_id: str


class ProductCreateBody(BaseModel):
    sku: str
    name: str
    category_id: str
    unit_usaha_id: Optional[str] = None
    unit_of_measure: str = "pcs"
    cost_price: Decimal = Field(default=Decimal("0"), ge=0)
    sell_price: Decimal = Field(default=Decimal("0"), ge=0)
    opening_qty: int = Field(default=0, ge=0)


class ProductUpdateBody(BaseModel):
    name: Optional[str] = None
    category_id: Optional[str] = None
    unit_of_measure: Optional[str] = None
    cost_price: Optional[Decimal] = Field(default=None, ge=0)
    sell_price: Optional[Decimal] = Field(default=None, ge=0)


class AdjustBody(BaseModel):
    product_id: str
    quantity_delta: int
    reason: str
    adjustment_date: date
    notes: Optional[str] = None
    unit_usaha_id: Optional[str] = None
    debit_account_code: Optional[str] = None
    credit_account_code: Optional[str] = None


async def resolve_unit_id(session: AsyncSession, body_unit_id: Optional[str], user: User) -> str:
    if public_role(user.role) == "pengelola" and user.unit_usaha_id:
        return user.unit_usaha_id
    if body_unit_id:
        return body_unit_id
    unit = await session.scalar(select(UnitUsaha).where(UnitUsaha.code == UU05_CODE))
    if not unit:
        raise HTTPException(status_code=422, detail="Unit usaha UU05 belum terdaftar")
    return unit.id


@router.get("/meta")
async def inventory_meta(
    user: User = Depends(require_inventory_user),
    session: AsyncSession = Depends(get_db),
):
    unit = await session.scalar(select(UnitUsaha).where(UnitUsaha.code == UU05_CODE))
    svc = InventoryService(session)
    return {
        "unit_code": UU05_CODE,
        "unit_usaha_id": unit.id if unit else None,
        "unit_name": unit.name if unit else None,
        "role": public_role(user.role),
        "categories": await svc.list_categories(),
    }


@router.get("/categories")
async def list_categories(
    _: User = Depends(require_inventory_user),
    session: AsyncSession = Depends(get_db),
):
    return await InventoryService(session).list_categories()


@router.get("/products")
async def list_products(
    category_id: Optional[str] = None,
    q: Optional[str] = None,
    unit_usaha_id: Optional[str] = None,
    user: User = Depends(require_inventory_user),
    session: AsyncSession = Depends(get_db),
):
    uid = user.unit_usaha_id if public_role(user.role) == "pengelola" else unit_usaha_id
    return await InventoryService(session).list_products(
        unit_usaha_id=uid, category_id=category_id, q=q
    )


@router.post("/products")
async def create_product(
    body: ProductCreateBody,
    user: User = Depends(require_inventory_user),
    session: AsyncSession = Depends(get_db),
):
    svc = InventoryService(session)
    try:
        uid = await resolve_unit_id(session, body.unit_usaha_id, user)
        return await svc.create_product(
            sku=body.sku,
            name=body.name,
            category_id=body.category_id,
            unit_usaha_id=uid,
            unit_of_measure=body.unit_of_measure,
            cost_price=body.cost_price,
            sell_price=body.sell_price,
            opening_qty=body.opening_qty,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/products/{product_id}")
async def update_product(
    product_id: str,
    body: ProductUpdateBody,
    _: User = Depends(require_inventory_user),
    session: AsyncSession = Depends(get_db),
):
    try:
        return await InventoryService(session).update_product(
            product_id,
            name=body.name,
            category_id=body.category_id,
            unit_of_measure=body.unit_of_measure,
            cost_price=body.cost_price,
            sell_price=body.sell_price,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/products/{product_id}")
async def delete_product(
    product_id: str,
    _: User = Depends(require_inventory_user),
    session: AsyncSession = Depends(get_db),
):
    try:
        await InventoryService(session).delete_product(product_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"deleted": product_id}


@router.post("/stock-in")
async def stock_in(
    body: StockInBody,
    user: User = Depends(require_inventory_user),
    session: AsyncSession = Depends(get_db),
):
    svc = InventoryService(session)
    try:
        uid = await resolve_unit_id(session, body.unit_usaha_id, user)
        card = await svc.stock_in(
            product_id=body.product_id,
            quantity=body.quantity,
            unit_cost=body.unit_cost,
            movement_date=body.movement_date,
            unit_usaha_id=uid,
            debit_account_code=body.debit_account_code,
            credit_account_code=body.credit_account_code,
            created_by=user.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return card_out(card)


@router.post("/stock-out")
async def stock_out(
    body: StockOutBody,
    user: User = Depends(require_inventory_user),
    session: AsyncSession = Depends(get_db),
):
    svc = InventoryService(session)
    try:
        uid = await resolve_unit_id(session, body.unit_usaha_id, user)
        card = await svc.stock_out(
            product_id=body.product_id,
            quantity=body.quantity,
            movement_date=body.movement_date,
            unit_usaha_id=uid,
            debit_account_code=body.debit_account_code,
            credit_account_code=body.credit_account_code,
            created_by=user.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return card_out(card)


@router.post("/cancel-movement")
async def cancel_movement(
    body: CancelBody,
    _: User = Depends(require_inventory_user),
    session: AsyncSession = Depends(get_db),
):
    await InventoryService(session).cancel_movement(body.stock_card_id)
    return {"cancelled": body.stock_card_id}


@router.get("/movements")
async def list_movements(
    product_id: Optional[str] = None,
    direction: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    _: User = Depends(require_inventory_user),
    session: AsyncSession = Depends(get_db),
):
    return await InventoryService(session).list_movements(
        product_id=product_id, direction=direction, limit=limit
    )



@router.post("/cancel-adjustment")
async def cancel_adjustment(
    body: CancelAdjustBody,
    _: User = Depends(require_inventory_user),
    session: AsyncSession = Depends(get_db),
):
    await InventoryService(session).cancel_adjustment(body.adjustment_id)
    return {"cancelled": body.adjustment_id}


@router.get("/adjustments")
async def list_adjustments(
    limit: int = Query(100, ge=1, le=500),
    _: User = Depends(require_inventory_user),
    session: AsyncSession = Depends(get_db),
):
    return await InventoryService(session).list_adjustments(limit=limit)


@router.post("/adjustments")
async def create_adjustment(
    body: AdjustBody,
    user: User = Depends(require_inventory_user),
    session: AsyncSession = Depends(get_db),
):
    try:
        uid = await resolve_unit_id(session, body.unit_usaha_id, user)
        return await InventoryService(session).adjust_stock(
            product_id=body.product_id,
            quantity_delta=body.quantity_delta,
            reason=body.reason,
            adjustment_date=body.adjustment_date,
            notes=body.notes,
            unit_usaha_id=uid,
            debit_account_code=body.debit_account_code,
            credit_account_code=body.credit_account_code,
            created_by=user.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/reports/valuation")
async def report_valuation(
    _: User = Depends(require_inventory_user),
    session: AsyncSession = Depends(get_db),
):
    return await InventoryService(session).stock_valuation_report()


@router.get("/reports/movements")
async def report_movements(
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    _: User = Depends(require_inventory_user),
    session: AsyncSession = Depends(get_db),
):
    return await InventoryService(session).movement_summary_report(
        date_from=date_from, date_to=date_to
    )
