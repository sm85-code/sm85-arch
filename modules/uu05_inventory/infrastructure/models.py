"""Relational inventory models (UU05)."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StockCategory(Base):
    __tablename__ = "stock_categories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    unit_usaha_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("unit_usaha.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    products: Mapped[list["Product"]] = relationship(back_populates="category")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("sku", "unit_usaha_id", name="uq_products_sku_unit"),
        Index("ix_products_unit", "unit_usaha_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    sku: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("stock_categories.id", ondelete="RESTRICT"), nullable=False
    )
    unit_usaha_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("unit_usaha.id", ondelete="RESTRICT"), nullable=False
    )
    unit_of_measure: Mapped[str] = mapped_column(String(20), nullable=False, default="pcs")
    cost_price: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0.00"))
    sell_price: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0.00"))
    qty_on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    category: Mapped["StockCategory"] = relationship(back_populates="products")
    stock_cards: Mapped[list["StockCard"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    adjustments: Mapped[list["StockAdjustment"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class StockCard(Base):
    """Immutable movement ledger line (in/out)."""

    __tablename__ = "stock_cards"
    __table_args__ = (
        Index("ix_stock_cards_product_date", "product_id", "movement_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    movement_date: Mapped[date] = mapped_column(Date, nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # in | out
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0.00"))
    total_value: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0.00"))
    reference: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    finance_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )  # pending | posted | cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    product: Mapped["Product"] = relationship(back_populates="stock_cards")


class StockAdjustment(Base):
    """Manual / damage / expiry adjustments."""

    __tablename__ = "stock_adjustments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    adjustment_date: Mapped[date] = mapped_column(Date, nullable=False)
    quantity_delta: Mapped[int] = mapped_column(Integer, nullable=False)  # +/- qty
    reason: Mapped[str] = mapped_column(String(64), nullable=False)  # rusak|kadaluarsa|koreksi
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    product: Mapped["Product"] = relationship(back_populates="adjustments")
