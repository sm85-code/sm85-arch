"""UU05 inventory stock movements."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from modules.uu05_inventory.application.coa import validate_coa_codes
from modules.uu05_inventory.infrastructure.models import Product, StockCard


class InventoryStockMixin:
    async def stock_in(
        self,
        *,
        product_id: str,
        quantity: int,
        unit_cost: Decimal,
        movement_date: date,
        unit_usaha_id: str,
        debit_account_code: str,
        credit_account_code: str,
        created_by: str = "system-inventory",
    ) -> StockCard:
        if quantity <= 0:
            raise ValueError("quantity must be > 0")
        product = await self.session.get(Product, product_id)
        if not product:
            raise ValueError("product not found")
        total = (unit_cost * quantity).quantize(Decimal("0.01"))
        card = StockCard(
            product_id=product_id,
            movement_date=movement_date,
            direction="in",
            quantity=quantity,
            unit_cost=unit_cost,
            total_value=total,
            reference="",
            finance_status="pending",
        )
        product.qty_on_hand += quantity
        product.cost_price = unit_cost
        self.session.add(card)
        await self.session.flush()
        card.reference = f"stock-in:{card.id}"
        await validate_coa_codes(self.session, debit_account_code, credit_account_code)
        await self.finance.record_inventory_journal(
            movement_date=movement_date,
            unit_usaha_id=unit_usaha_id,
            amount=total,
            debit_account_code=debit_account_code,
            credit_account_code=credit_account_code,
            description=f"Stok masuk {product.sku} x{quantity}",
            reference=card.reference,
            created_by=created_by,
            transaction_type="inventory_purchase",
        )
        card.finance_status = "posted"
        await self.session.flush()
        return card


    async def stock_out(
        self,
        *,
        product_id: str,
        quantity: int,
        movement_date: date,
        unit_usaha_id: str,
        debit_account_code: str,
        credit_account_code: str,
        created_by: str = "system-inventory",
    ) -> StockCard:
        if quantity <= 0:
            raise ValueError("quantity must be > 0")
        product = await self.session.get(Product, product_id)
        if not product:
            raise ValueError("product not found")
        if product.qty_on_hand < quantity:
            raise ValueError("insufficient stock")
        unit_cost = product.cost_price
        total = (unit_cost * quantity).quantize(Decimal("0.01"))
        card = StockCard(
            product_id=product_id,
            movement_date=movement_date,
            direction="out",
            quantity=quantity,
            unit_cost=unit_cost,
            total_value=total,
            reference="",
            finance_status="pending",
        )
        product.qty_on_hand -= quantity
        self.session.add(card)
        await self.session.flush()
        card.reference = f"stock-out:{card.id}"
        await validate_coa_codes(self.session, debit_account_code, credit_account_code)
        await self.finance.record_inventory_journal(
            movement_date=movement_date,
            unit_usaha_id=unit_usaha_id,
            amount=total,
            debit_account_code=debit_account_code,
            credit_account_code=credit_account_code,
            description=f"Stok keluar / COGS {product.sku} x{quantity}",
            reference=card.reference,
            created_by=created_by,
            transaction_type="inventory_cogs",
        )
        card.finance_status = "posted"
        await self.session.flush()
        return card


    async def cancel_movement(self, stock_card_id: str) -> None:
        card = await self.session.get(StockCard, stock_card_id)
        if not card or card.finance_status == "cancelled":
            return
        product = await self.session.get(Product, card.product_id)
        if product:
            if card.direction == "in":
                product.qty_on_hand = max(0, product.qty_on_hand - card.quantity)
            else:
                product.qty_on_hand += card.quantity
        if card.reference:
            await self.finance.cancel_inventory_journal(card.reference)
        card.finance_status = "cancelled"
        await self.session.flush()


    async def list_movements(
        self,
        *,
        product_id: Optional[str] = None,
        direction: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        stmt = (
            select(StockCard)
            .options(selectinload(StockCard.product).selectinload(Product.category))
            .order_by(StockCard.movement_date.desc(), StockCard.created_at.desc())
            .limit(min(limit, 500))
        )
        if product_id:
            stmt = stmt.where(StockCard.product_id == product_id)
        if direction in {"in", "out"}:
            stmt = stmt.where(StockCard.direction == direction)
        rows = (await self.session.scalars(stmt)).all()
        return [
            {
                "id": c.id,
                "product_id": c.product_id,
                "sku": c.product.sku if c.product else None,
                "product_name": c.product.name if c.product else None,
                "movement_date": c.movement_date.isoformat(),
                "direction": c.direction,
                "quantity": c.quantity,
                "unit_cost": str(c.unit_cost),
                "total_value": str(c.total_value),
                "reference": c.reference,
                "finance_status": c.finance_status,
            }
            for c in rows
        ]
