"""UU05 inventory HTTP API (/api/v1/uu05_inventory)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modules.uu05_inventory.application.services import InventoryService
from shared.database import get_db

router = APIRouter(prefix="/api/v1/uu05_inventory", tags=["uu05_inventory"])


class StockInRequest(BaseModel):
    product_id: str
    quantity: int = Field(gt=0)
    unit_cost: Decimal = Field(ge=0)
    movement_date: date
    unit_usaha_id: str
    debit_account_code: str
    credit_account_code: str
    created_by: str = "system-inventory"


class StockOutRequest(BaseModel):
    product_id: str
    quantity: int = Field(gt=0)
    movement_date: date
    unit_usaha_id: str
    debit_account_code: str
    credit_account_code: str
    created_by: str = "system-inventory"


class CancelMovementRequest(BaseModel):
    stock_card_id: str


@router.post("/stock-in")
async def stock_in(
    body: StockInRequest,
    session: AsyncSession = Depends(get_db),
):
    svc = InventoryService(session)
    try:
        card = await svc.stock_in(
            product_id=body.product_id,
            quantity=body.quantity,
            unit_cost=body.unit_cost,
            movement_date=body.movement_date,
            unit_usaha_id=body.unit_usaha_id,
            debit_account_code=body.debit_account_code,
            credit_account_code=body.credit_account_code,
            created_by=body.created_by,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return {
        "id": card.id,
        "reference": card.reference,
        "finance_status": card.finance_status,
        "quantity": card.quantity,
        "total_value": str(card.total_value),
    }


@router.post("/stock-out")
async def stock_out(
    body: StockOutRequest,
    session: AsyncSession = Depends(get_db),
):
    svc = InventoryService(session)
    try:
        card = await svc.stock_out(
            product_id=body.product_id,
            quantity=body.quantity,
            movement_date=body.movement_date,
            unit_usaha_id=body.unit_usaha_id,
            debit_account_code=body.debit_account_code,
            credit_account_code=body.credit_account_code,
            created_by=body.created_by,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return {
        "id": card.id,
        "reference": card.reference,
        "finance_status": card.finance_status,
        "quantity": card.quantity,
        "total_value": str(card.total_value),
    }


@router.post("/cancel-movement")
async def cancel_movement(
    body: CancelMovementRequest,
    session: AsyncSession = Depends(get_db),
):
    svc = InventoryService(session)
    await svc.cancel_movement(body.stock_card_id)
    return {"cancelled": body.stock_card_id}
