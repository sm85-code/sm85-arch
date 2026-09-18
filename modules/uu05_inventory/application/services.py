"""Stock movements + finance impact via siabumdes public contract."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.siabumdes.public_service import SiabumdesPublicService
from modules.uu05_inventory.infrastructure.models import Product, StockCard


class InventoryService:
    def __init__(
        self,
        session: AsyncSession,
        finance: Optional[SiabumdesPublicService] = None,
    ) -> None:
        self.session = session
        self.finance = finance or SiabumdesPublicService(session)

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
        tx_id = await self.finance.record_inventory_journal(
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
