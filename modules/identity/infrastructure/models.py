"""Users, system-wide recording lock, and closed accounting periods."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from shared.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("username", name="uq_users_username"), UniqueConstraint("email", name="uq_users_email"))

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    unit_usaha_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    blocked_periods: Mapped[list[str]] = mapped_column(ARRAY(String(7)), nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SystemControl(Base):
    """Singleton row (id=default) for application-wide recording lock."""

    __tablename__ = "system_controls"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="default")
    recording_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ClosedPeriod(Base):
    __tablename__ = "closed_periods"
    __table_args__ = (UniqueConstraint("period", "group_code", name="uq_closed_periods_period_group"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)  # YYYY-MM
    group_code: Mapped[str] = mapped_column(String(20), nullable=False, default="BUMDES")
    laba_bersih: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    entries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    closed_by: Mapped[str] = mapped_column(String(36), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
