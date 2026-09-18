"""UU05 inventory application services: catalog, movements, adjustments, reports."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from modules.siabumdes.infrastructure.models import UnitUsaha
from modules.siabumdes.public_service import SiabumdesPublicService
from modules.uu05_inventory.infrastructure.models import (
    Product,
    StockAdjustment,
    StockCard,
    StockCategory,
)

UU05_CODE = "UU05"

DEFAULT_CATEGORIES: list[tuple[str, str]] = [
    ("PRT", "Peralatan Rumah Tangga"),
    ("PD", "Peralatan Dapur"),
    ("MM", "Makanan & Minuman"),
    ("FUR", "Furniture"),
    ("PHP", "Perawatan Hewan Peliharaan"),
    ("LAI", "Lainnya"),
]


class InventoryService:
    def __init__(
        self,
        session: AsyncSession,
        finance: Optional[SiabumdesPublicService] = None,
    ) -> None:
        self.session = session
        self.finance = finance or SiabumdesPublicService(session)

    async def resolve_uu05_unit(self) -> UnitUsaha:
        unit = await self.session.scalar(select(UnitUsaha).where(UnitUsaha.code == UU05_CODE))
        if not unit:
            raise ValueError("Unit usaha UU05 belum terdaftar")
        return unit

    async def ensure_categories(self, unit_usaha_id: Optional[str] = None) -> list[StockCategory]:
        if unit_usaha_id:
            uid = unit_usaha_id
        else:
            uid = (await self.resolve_uu05_unit()).id
        existing = (
            await self.session.scalars(select(StockCategory).where(StockCategory.unit_usaha_id == uid))
        ).all()
        by_code = {c.code: c for c in existing}
        changed = False
        for code, name in DEFAULT_CATEGORIES:
            if code in by_code:
                if by_code[code].name != name:
                    by_code[code].name = name
                    changed = True
                continue
            global_row = await self.session.scalar(select(StockCategory).where(StockCategory.code == code))
            if global_row:
                by_code[code] = global_row
                continue
            row = StockCategory(code=code, name=name, unit_usaha_id=uid)
            self.session.add(row)
            by_code[code] = row
            changed = True
        if changed:
            await self.session.flush()
        return [by_code[code] for code, _ in DEFAULT_CATEGORIES if code in by_code]

    async def list_categories(self) -> list[dict[str, Any]]:
        rows = await self.ensure_categories()
        return [{"id": r.id, "code": r.code, "name": r.name, "unit_usaha_id": r.unit_usaha_id} for r in rows]

    def _product_query(
        self,
        *,
        unit_usaha_id: Optional[str] = None,
        category_id: Optional[str] = None,
        q: Optional[str] = None,
    ) -> Select[tuple[Product]]:
        stmt = select(Product).options(selectinload(Product.category)).order_by(Product.name.asc())
        if unit_usaha_id:
            stmt = stmt.where(Product.unit_usaha_id == unit_usaha_id)
        if category_id:
            stmt = stmt.where(Product.category_id == category_id)
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(or_(Product.sku.ilike(like), Product.name.ilike(like)))
        return stmt

    @staticmethod
    def _product_dict(p: Product) -> dict[str, Any]:
        return {
            "id": p.id,
            "sku": p.sku,
            "name": p.name,
            "category_id": p.category_id,
            "category_code": p.category.code if p.category else None,
            "category_name": p.category.name if p.category else None,
            "unit_usaha_id": p.unit_usaha_id,
            "unit_of_measure": p.unit_of_measure,
            "cost_price": str(p.cost_price),
            "sell_price": str(p.sell_price),
            "qty_on_hand": p.qty_on_hand,
            "stock_value": str((p.cost_price * Decimal(p.qty_on_hand)).quantize(Decimal("0.01"))),
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }

    async def list_products(
        self,
        *,
        unit_usaha_id: Optional[str] = None,
        category_id: Optional[str] = None,
        q: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        await self.ensure_categories()
        rows = (
            await self.session.scalars(
                self._product_query(unit_usaha_id=unit_usaha_id, category_id=category_id, q=q)
            )
        ).all()
        return [self._product_dict(p) for p in rows]

    async def create_product(
        self,
        *,
        sku: str,
        name: str,
        category_id: str,
        unit_usaha_id: Optional[str] = None,
        unit_of_measure: str = "pcs",
        cost_price: Decimal = Decimal("0"),
        sell_price: Decimal = Decimal("0"),
        opening_qty: int = 0,
    ) -> dict[str, Any]:
        unit = await self.resolve_uu05_unit()
        uid = unit_usaha_id or unit.id
        await self.ensure_categories(uid)
        cat = await self.session.get(StockCategory, category_id)
        if not cat:
            raise ValueError("kategori tidak ditemukan")
        sku_clean = sku.strip().upper()
        if not sku_clean or not name.strip():
            raise ValueError("SKU dan nama wajib diisi")
        if opening_qty < 0:
            raise ValueError("qty awal tidak boleh negatif")
        exists = await self.session.scalar(
            select(Product).where(Product.sku == sku_clean, Product.unit_usaha_id == uid)
        )
        if exists:
            raise ValueError(f"SKU {sku_clean} sudah ada")
        product = Product(
            sku=sku_clean,
            name=name.strip(),
            category_id=category_id,
            unit_usaha_id=uid,
            unit_of_measure=(unit_of_measure or "pcs").strip() or "pcs",
            cost_price=Decimal(cost_price).quantize(Decimal("0.01")),
            sell_price=Decimal(sell_price).quantize(Decimal("0.01")),
            qty_on_hand=opening_qty,
        )
        self.session.add(product)
        await self.session.flush()
        await self.session.refresh(product, attribute_names=["category"])
        return self._product_dict(product)

    async def update_product(
        self,
        product_id: str,
        *,
        name: Optional[str] = None,
        category_id: Optional[str] = None,
        unit_of_measure: Optional[str] = None,
        cost_price: Optional[Decimal] = None,
        sell_price: Optional[Decimal] = None,
    ) -> dict[str, Any]:
        product = await self.session.scalar(
            select(Product).options(selectinload(Product.category)).where(Product.id == product_id)
        )
        if not product:
            raise ValueError("produk tidak ditemukan")
        if name is not None:
            product.name = name.strip()
        if category_id is not None:
            cat = await self.session.get(StockCategory, category_id)
            if not cat:
                raise ValueError("kategori tidak ditemukan")
            product.category_id = category_id
        if unit_of_measure is not None:
            product.unit_of_measure = unit_of_measure.strip() or product.unit_of_measure
        if cost_price is not None:
            product.cost_price = Decimal(cost_price).quantize(Decimal("0.01"))
        if sell_price is not None:
            product.sell_price = Decimal(sell_price).quantize(Decimal("0.01"))
        await self.session.flush()
        await self.session.refresh(product, attribute_names=["category"])
        return self._product_dict(product)

    async def delete_product(self, product_id: str) -> None:
        product = await self.session.get(Product, product_id)
        if not product:
            raise ValueError("produk tidak ditemukan")
        if product.qty_on_hand != 0:
            raise ValueError("stok harus 0 sebelum produk dihapus")
        await self.session.delete(product)
        await self.session.flush()

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

    async def adjust_stock(
        self,
        *,
        product_id: str,
        quantity_delta: int,
        reason: str,
        adjustment_date: date,
        notes: Optional[str] = None,
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
        return {
            "id": adj.id,
            "product_id": product_id,
            "sku": product.sku,
            "adjustment_date": adj.adjustment_date.isoformat(),
            "quantity_delta": adj.quantity_delta,
            "reason": adj.reason,
            "notes": adj.notes,
            "qty_on_hand": product.qty_on_hand,
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
