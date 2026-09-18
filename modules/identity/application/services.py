"""Auth, RBAC, system lock, and period-close use cases."""
from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.identity.infrastructure.models import ClosedPeriod, SystemControl, User
from modules.siabumdes.infrastructure.models import Transaction, UnitUsaha
from shared.config import PUBLIC_ROLES, public_role
from shared.security import hash_password, verify_password

_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")


def user_to_out(user: User, *, recording_locked: bool = False) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "name": user.name,
        "role": public_role(user.role),
        "unit_usaha_id": user.unit_usaha_id,
        "active": user.active,
        "must_change_password": user.must_change_password,
        "blocked_periods": list(user.blocked_periods or []),
        "edits_locked": recording_locked,
    }


async def get_system_control(session: AsyncSession) -> SystemControl:
    row = await session.get(SystemControl, "default")
    if row:
        return row
    row = SystemControl(id="default", recording_locked=False)
    session.add(row)
    await session.flush()
    return row


async def find_user_by_login(session: AsyncSession, username: str) -> Optional[User]:
    stmt: Select[tuple[User]] = select(User).where(
        or_(User.username == username, User.email == username)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def authenticate(session: AsyncSession, username: str, password: str) -> User:
    user = await find_user_by_login(session, username.strip())
    if not user or not verify_password(password, user.password_hash):
        raise ValueError("Username atau password salah")
    if not user.active:
        raise PermissionError("Akun dinonaktifkan")
    return user


async def create_user(
    session: AsyncSession,
    *,
    username: str,
    email: str,
    name: str,
    password: str,
    role: str,
    unit_usaha_id: Optional[str],
) -> User:
    mapped = public_role(role)
    if mapped not in PUBLIC_ROLES:
        raise ValueError("Role tidak valid")
    if mapped == "pengelola" and not unit_usaha_id:
        raise ValueError("Pengelola harus memiliki unit_usaha_id")
    existing = await session.execute(
        select(User).where(or_(User.username == username, User.email == email))
    )
    if existing.scalar_one_or_none():
        raise ValueError("Email atau username sudah terdaftar")
    user = User(
        username=username.strip(),
        email=email.strip(),
        name=name.strip(),
        password_hash=hash_password(password),
        role=mapped,
        unit_usaha_id=unit_usaha_id,
        must_change_password=True,
    )
    session.add(user)
    await session.flush()
    return user


async def list_users(session: AsyncSession) -> list[User]:
    rows = await session.execute(select(User).order_by(User.created_at.desc()))
    return list(rows.scalars())


def normalize_periods(raw: list[Any]) -> list[str]:
    cleaned: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if _PERIOD_RE.match(value):
            cleaned.append(value)
    return sorted(set(cleaned))


async def set_blocked_periods(session: AsyncSession, user_id: str, periods: list[Any]) -> User:
    user = await session.get(User, user_id)
    if not user:
        raise LookupError("User tidak ditemukan")
    user.blocked_periods = normalize_periods(periods)
    await session.flush()
    return user


async def set_recording_lock(
    session: AsyncSession,
    *,
    locked: bool,
    actor_id: str,
    note: str = "",
) -> SystemControl:
    control = await get_system_control(session)
    control.recording_locked = locked
    control.locked_by = actor_id if locked else None
    control.locked_at = datetime.now(timezone.utc) if locked else None
    control.note = note or ""
    control.updated_at = datetime.now(timezone.utc)
    await session.flush()
    return control


async def assert_recording_allowed(
    session: AsyncSession,
    user: User,
    tx_date: Optional[date] = None,
    group_code: str = "BUMDES",
) -> None:
    """Additional access rules win over legacy per-user locks."""
    role = public_role(user.role)
    control = await get_system_control(session)
    if control.recording_locked and role != "admin":
        raise PermissionError("Pencatatan dikunci oleh Admin")
    if tx_date:
        period = tx_date.strftime("%Y-%m")
        if period in (user.blocked_periods or []) and role != "admin":
            raise PermissionError(f"Periode {period} terkunci untuk akun ini")
        closed = await session.execute(
            select(ClosedPeriod).where(
                ClosedPeriod.period == period,
                ClosedPeriod.group_code == group_code,
            )
        )
        if closed.scalar_one_or_none() and role != "admin":
            raise PermissionError(f"Buku periode {period} ({group_code}) sudah ditutup")


async def close_period(
    session: AsyncSession,
    *,
    period: str,
    group: str,
    actor_id: str,
) -> ClosedPeriod:
    if not _PERIOD_RE.match(period):
        raise ValueError("Format period harus YYYY-MM")
    group_code = (group or "BUMDES").strip().upper()
    existing = await session.execute(
        select(ClosedPeriod).where(
            ClosedPeriod.period == period,
            ClosedPeriod.group_code == group_code,
        )
    )
    if existing.scalar_one_or_none():
        raise ValueError(f"Periode {period} untuk {group_code} sudah ditutup")

    year, month = int(period[:4]), int(period[5:7])
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])

    unit_id = None
    if group_code != "BUMDES":
        unit = (
            await session.execute(select(UnitUsaha).where(UnitUsaha.code == group_code))
        ).scalar_one_or_none()
        unit_id = unit.id if unit else None

    stmt = select(func.count(Transaction.id), func.coalesce(func.sum(Transaction.amount), 0)).where(
        Transaction.date >= start,
        Transaction.date <= end,
    )
    if unit_id:
        stmt = stmt.where(Transaction.unit_usaha_id == unit_id)
    else:
        stmt = stmt.where(Transaction.unit_usaha_id.is_(None))
    count, total = (await session.execute(stmt)).one()

    row = ClosedPeriod(
        period=period,
        group_code=group_code,
        entries=int(count or 0),
        laba_bersih=Decimal(str(total or 0)),
        closed_by=actor_id,
    )
    session.add(row)
    await session.flush()
    return row


async def list_closed_periods(session: AsyncSession) -> list[ClosedPeriod]:
    rows = await session.execute(
        select(ClosedPeriod).order_by(ClosedPeriod.period.desc(), ClosedPeriod.group_code.asc())
    )
    return list(rows.scalars())


async def reopen_period(session: AsyncSession, period: str, group: str) -> int:
    if not _PERIOD_RE.match(period):
        raise ValueError("Format period harus YYYY-MM")
    group_code = (group or "BUMDES").strip().upper()
    row = (
        await session.execute(
            select(ClosedPeriod).where(
                ClosedPeriod.period == period,
                ClosedPeriod.group_code == group_code,
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise LookupError("Periode tertutup tidak ditemukan")
    deleted_entries = row.entries
    await session.delete(row)
    await session.flush()
    return deleted_entries
