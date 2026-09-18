"""Normalized PostgreSQL ORM models for core finance.

Replaces legacy JSONB application_entities / Mongo-style documents.
All PKs are UUID strings (no ObjectId / _id).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
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


class UnitUsaha(Base):
    __tablename__ = "unit_usaha"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    revenue_scheme: Mapped[str] = mapped_column(Text, nullable=False, default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    accounts: Mapped[list["Account"]] = relationship(back_populates="unit_usaha")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="unit_usaha")


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("code", "group_code", name="uq_accounts_code_group"),
        Index("ix_accounts_category", "category"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)  # aset|kewajiban|ekuitas|pendapatan|beban
    subcategory: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    normal_balance: Mapped[str] = mapped_column(String(8), nullable=False)  # debit|kredit
    parent_code: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    group_code: Mapped[str] = mapped_column(String(20), nullable=False, default="BUMDES", index=True)
    unit_usaha_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("unit_usaha.id", ondelete="SET NULL"), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    unit_usaha: Mapped[Optional["UnitUsaha"]] = relationship(back_populates="accounts")
    journal_items: Mapped[list["JournalItem"]] = relationship(back_populates="account")


class Transaction(Base):
    """Business transaction header (source document)."""

    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_date", "date"),
        Index("ix_transactions_reference", "reference"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    unit_usaha_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("unit_usaha.id", ondelete="SET NULL"), nullable=True, index=True
    )
    transaction_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    debit_account_code: Mapped[str] = mapped_column(String(32), nullable=False)
    credit_account_code: Mapped[str] = mapped_column(String(32), nullable=False)
    mitra_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    reference: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    unit_usaha: Mapped[Optional["UnitUsaha"]] = relationship(back_populates="transactions")
    journal_entry: Mapped[Optional["JournalEntry"]] = relationship(
        back_populates="transaction", uselist=False
    )


class JournalEntry(Base):
    """Double-entry journal header linked 1:1 to a Transaction."""

    __tablename__ = "journal_entries"
    __table_args__ = (Index("ix_journal_entries_date", "entry_date"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    transaction_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("transactions.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    memo: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    transaction: Mapped["Transaction"] = relationship(back_populates="journal_entry")
    items: Mapped[list["JournalItem"]] = relationship(
        back_populates="journal_entry", cascade="all, delete-orphan"
    )


class JournalItem(Base):
    """Debit/credit line item — normalized from embedded JSONB legs."""

    __tablename__ = "journal_items"
    __table_args__ = (Index("ix_journal_items_account", "account_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    journal_entry_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("journal_entries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # debit | credit
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)

    journal_entry: Mapped["JournalEntry"] = relationship(back_populates="items")
    account: Mapped["Account"] = relationship(back_populates="journal_items")
