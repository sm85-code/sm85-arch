"""UU05 inventory adjustments and reports."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from modules.uu05_inventory.application.coa import validate_coa_codes
from modules.uu05_inventory.infrastructure.models import (
    Product,
    StockAdjustment,
    StockCard,
)


class InventoryAdjustReportsMixin:
    async def adjust_stock(
        self,
        *,
        product_id: str,
        quantity_delta: int,
        reason: str,
        adjustment_date: date,
        notes: Optional[str] = None,
        unit_usaha_id: Optional[str] = None,
        debit_account_code: Optional[str] = None,
        credit_account_code: Optional[str] = None,
        created_by: str = "system-inventory",
    ) -> dict[str, Any]:
        if quantity_delta == 0:
            raise ValueError("quantity_delta tidak boleh 0")
        reason_clean = (reason or "").strip().lower()
        if reason_clean not in {"rusak", "kadaluarsa", "koreksi"}:
            raise ValueError("reason harus rusak, kadaluarsa, atau koreksi")
        product = await self.session.get(Product, product_id)
        if not product:
            raise ValueError("produk tidak ditemukan")
        new_qty = product.qty_on_hand + quantity_delta
        if new_qty < 0:
            raise ValueError("penyesuaian membuat stok negatif")

        amount = (product.cost_price * Decimal(abs(quantity_delta))).quantize(Decimal("0.01"))
        journal_reference = None
        finance_status = "skipped"

        if amount > 0:
            if not (debit_account_code or "").strip() or not (credit_account_code or "").strip():
                raise ValueError(
                    "debit_account_code dan credit_account_code wajib diisi "
                    "ketika penyesuaian berdampak nilai (HPP x |delta| > 0)"
                )
            await validate_coa_codes(self.session, debit_account_code, credit_account_code)

        product.qty_on_hand = new_qty
        adj = StockAdjustment(
            product_id=product_id,
            adjustment_date=adjustment_date,
            quantity_delta=quantity_delta,
            reason=reason_clean,
            notes=(notes or "").strip() or None,
        )
        self.session.add(adj)
        await self.session.flush()

        if amount > 0:
            uid = unit_usaha_id or product.unit_usaha_id
            journal_reference = f"stock-adj:{adj.id}"
            side = "loss" if quantity_delta < 0 else "gain"
            desc = (
                f"Penyesuaian stok {side} {product.sku} "
                f"delta={quantity_delta} ({reason_clean})"
            )
            await self.finance.record_inventory_journal(
                movement_date=adjustment_date,
                unit_usaha_id=uid,
                amount=amount,
                debit_account_code=debit_account_code.strip(),
                credit_account_code=credit_account_code.strip(),
                description=desc,
                reference=journal_reference,
                created_by=created_by,
                transaction_type="inventory_adjustment",
            )
            finance_status = "posted"

        return {
            "id": adj.id,
            "product_id": product_id,
            "sku": product.sku,
            "adjustment_date": adj.adjustment_date.isoformat(),
            "quantity_delta": adj.quantity_delta,
            "reason": adj.reason,
            "notes": adj.notes,
            "qty_on_hand": product.qty_on_hand,
            "cost_impact": str(amount),
            "journal_reference": journal_reference,
            "finance_status": finance_status,
        }


    async def list_adjustments(self, *, limit: int = 100) -> list[dict[str, Any]]:
        stmt = (
            select(StockAdjustment)
            .options(selectinload(StockAdjustment.product))
            .order_by(StockAdjustment.adjustment_date.desc(), StockAdjustment.created_at.desc())
            .limit(min(limit, 500))
        )
        rows = (await self.session.scalars(stmt)).all()
        return [
            {
                "id": a.id,
                "product_id": a.product_id,
                "sku": a.product.sku if a.product else None,
                "product_name": a.product.name if a.product else None,
                "adjustment_date": a.adjustment_date.isoformat(),
                "quantity_delta": a.quantity_delta,
                "reason": a.reason,
                "notes": a.notes,
            }
            for a in rows
        ]


    async def stock_valuation_report(self) -> dict[str, Any]:
        await self.ensure_categories()
        products = (
            await self.session.scalars(
                select(Product).options(selectinload(Product.category)).order_by(Product.name.asc())
            )
        ).all()
        lines = []
        total_qty = 0
        total_value = Decimal("0.00")
        by_category: dict[str, dict[str, Any]] = {}
        for p in products:
            value = (p.cost_price * Decimal(p.qty_on_hand)).quantize(Decimal("0.01"))
            total_qty += p.qty_on_hand
            total_value += value
            cat_name = p.category.name if p.category else "Lainnya"
            bucket = by_category.setdefault(
                cat_name, {"category": cat_name, "qty": 0, "value": Decimal("0.00"), "sku_count": 0}
            )
            bucket["qty"] += p.qty_on_hand
            bucket["value"] += value
            bucket["sku_count"] += 1
            lines.append(self._product_dict(p))
        return {
            "summary": {
                "sku_count": len(products),
                "total_qty": total_qty,
                "total_value": str(total_value),
            },
            "by_category": [
                {
                    "category": v["category"],
                    "qty": v["qty"],
                    "value": str(v["value"]),
                    "sku_count": v["sku_count"],
                }
                for v in by_category.values()
            ],
            "products": lines,
        }


    async def movement_summary_report(
        self,
        *,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> dict[str, Any]:
        stmt = select(StockCard).where(StockCard.finance_status != "cancelled")
        if date_from:
            stmt = stmt.where(StockCard.movement_date >= date_from)
        if date_to:
            stmt = stmt.where(StockCard.movement_date <= date_to)
        rows = (await self.session.scalars(stmt)).all()
        in_qty = sum(c.quantity for c in rows if c.direction == "in")
        out_qty = sum(c.quantity for c in rows if c.direction == "out")
        in_value = sum((c.total_value for c in rows if c.direction == "in"), Decimal("0.00"))
        out_value = sum((c.total_value for c in rows if c.direction == "out"), Decimal("0.00"))
        return {
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "stock_in": {
                "qty": in_qty,
                "value": str(in_value),
                "count": sum(1 for c in rows if c.direction == "in"),
            },
            "stock_out": {
                "qty": out_qty,
                "value": str(out_value),
                "count": sum(1 for c in rows if c.direction == "out"),
            },
            "net_qty": in_qty - out_qty,
        }
