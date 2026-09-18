"""Session + profile + user-admin routes matching frontend-siabumdes."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.api.deps import get_current_user, require_roles
from modules.identity.application.services import (
    authenticate,
    create_user,
    get_system_control,
    list_users,
    set_blocked_periods,
    user_to_out,
)
from modules.identity.infrastructure.models import User
from shared.config import public_role
from shared.database import get_db
from shared.security import (
    clear_auth_cookie,
    create_access_token,
    hash_password,
    set_auth_cookie,
    verify_password,
)

router = APIRouter(prefix="/api", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    email: EmailStr
    name: str
    password: str
    role: str
    unit_usaha_id: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class PasswordResetRequest(BaseModel):
    new_password: str


class ProfileUpdateRequest(BaseModel):
    name: str
    email: EmailStr
    username: str


class BlockedPeriodsRequest(BaseModel):
    blocked_periods: list[str] = Field(default_factory=list)


def _token_for(user: User) -> str:
    return create_access_token(
        user.id,
        user.role,
        user.session_version,
        {"name": user.name, "unit": user.unit_usaha_id},
    )


@router.post("/auth/login")
async def login(
    payload: LoginRequest,
    response: Response,
    session: AsyncSession = Depends(get_db),
):
    try:
        user = await authenticate(session, payload.username, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    set_auth_cookie(response, _token_for(user))
    control = await get_system_control(session)
    return {"user": user_to_out(user, recording_locked=control.recording_locked)}


@router.post("/auth/logout")
async def logout(response: Response):
    clear_auth_cookie(response)
    return {"ok": True}


@router.get("/auth/me")
async def me(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    control = await get_system_control(session)
    return user_to_out(user, recording_locked=control.recording_locked)


@router.post("/auth/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password baru minimal 8 karakter")
    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=400, detail="Password baru harus berbeda")
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Password sementara salah")
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    user.session_version += 1
    return {"ok": True}


@router.put("/auth/profile")
async def update_profile(
    payload: ProfileUpdateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    user.name = payload.name.strip()
    user.email = str(payload.email)
    user.username = payload.username.strip()
    control = await get_system_control(session)
    await session.flush()
    return user_to_out(user, recording_locked=control.recording_locked)


@router.post("/auth/register")
async def register(
    payload: RegisterRequest,
    _: User = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_db),
):
    try:
        user = await create_user(
            session,
            username=payload.username,
            email=str(payload.email),
            name=payload.name,
            password=payload.password,
            role=payload.role,
            unit_usaha_id=payload.unit_usaha_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return user_to_out(user)


@router.get("/users")
async def users(
    _: User = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_db),
):
    control = await get_system_control(session)
    rows = await list_users(session)
    return [user_to_out(row, recording_locked=control.recording_locked) for row in rows]


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    _: User = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_db),
):
    target = await session.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")
    if public_role(target.role) == "admin":
        raise HTTPException(status_code=400, detail="Admin tidak dapat dihapus")
    await session.delete(target)
    return {"deleted": 1}


@router.post("/users/{user_id}/reset-password")
async def reset_password(
    user_id: str,
    payload: PasswordResetRequest,
    _: User = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_db),
):
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password sementara minimal 8 karakter")
    target = await session.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")
    target.password_hash = hash_password(payload.new_password)
    target.must_change_password = True
    target.session_version += 1
    return {"ok": True}


@router.put("/users/{user_id}/blocked-periods")
async def update_blocked_periods(
    user_id: str,
    payload: BlockedPeriodsRequest,
    _: User = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_db),
):
    try:
        user = await set_blocked_periods(session, user_id, payload.blocked_periods)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    control = await get_system_control(session)
    return user_to_out(user, recording_locked=control.recording_locked)
