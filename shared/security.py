"""Password hashing and tenant-scoped JWT encode/decode primitives.

Cookie handling lives in each tenant's own ``infrastructure/auth.py``
(madrasah, toko, marketplace_erp each use their own cookie name).

Tenant isolation (B1):
- Each module passes ``tenant`` on encode and ``expected_tenant`` on decode.
- Tokens carry ``aud`` (tenant id) and ``iss`` (``sm85:<tenant>``).
- Signing secret prefers ``JWT_SECRET_<TENANT>`` (e.g. ``JWT_SECRET_TOKO``),
  falling back to shared ``JWT_SECRET`` when the tenant-specific env is unset
  so live deploys do not break before ops set the new vars.
- Legacy tokens minted before this change (no aud/iss) are still accepted
  when the signature verifies — residual cross-tenant risk until those
  sessions expire or users re-login. See PR notes.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException, status
from jose import JWTError, jwt
from passlib.context import CryptContext

from shared.config import (
    JWT_ALGORITHM,
    JWT_EXPIRE_HOURS,
    JWT_SECRET,
    jwt_issuer_for,
    public_role,
)

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return _pwd.verify(plain, hashed)
    except Exception:
        return False


def jwt_secret_for(tenant: str) -> str:
    """Resolve signing/verify secret for a tenant with live-safe fallback.

    Order: ``JWT_SECRET_<TENANT_UPPER>`` if set and non-empty, else ``JWT_SECRET``.
    Example: tenant ``toko`` → ``JWT_SECRET_TOKO`` → ``JWT_SECRET``.
    """
    key = f"JWT_SECRET_{tenant.strip().upper()}"
    specific = os.getenv(key, "").strip()
    if specific:
        return specific
    return JWT_SECRET


def create_access_token(
    subject: str,
    role: str,
    session_version: int,
    extra: Optional[dict[str, Any]] = None,
    *,
    tenant: str,
) -> str:
    secret = jwt_secret_for(tenant)
    if not secret:
        raise RuntimeError("JWT_SECRET must be set")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "role": public_role(role),
        "sv": session_version,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=JWT_EXPIRE_HOURS)).timestamp()),
        "aud": tenant,
        "iss": jwt_issuer_for(tenant),
    }
    if extra:
        # Tenant claims win over accidental extra overrides.
        extra_clean = {k: v for k, v in extra.items() if k not in {"aud", "iss"}}
        payload.update(extra_clean)
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_access_token(
    token: str,
    *,
    expected_tenant: Optional[str] = None,
) -> dict[str, Any]:
    if expected_tenant:
        secret = jwt_secret_for(expected_tenant)
    else:
        secret = JWT_SECRET
    if not secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Auth belum dikonfigurasi")
    try:
        # python-jose rejects tokens that *contain* aud unless audience= is
        # passed; legacy tokens omit aud. Disable jose's aud check and enforce
        # aud/iss ourselves so both shapes work during migration.
        payload = jwt.decode(
            token,
            secret,
            algorithms=[JWT_ALGORITHM],
            options={"verify_aud": False},
        )
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesi tidak valid") from exc

    if expected_tenant:
        aud = payload.get("aud")
        iss = payload.get("iss")
        expected_iss = jwt_issuer_for(expected_tenant)
        if aud is not None or iss is not None:
            if aud != expected_tenant or iss != expected_iss:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Sesi tidak valid",
                )
        # else: legacy token without aud/iss — accepted for migration window
    return payload
