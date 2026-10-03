"""Centralized runtime configuration for App Platform + local dev."""
from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

APP_TITLE = os.getenv("APP_TITLE", "SM85 Arch API")

EXTRA_CORS_ORIGINS = [
    "https://banihusen-sgxg4.ondigitalocean.app",
    "https://madrasah-l7kl7.ondigitalocean.app",
]


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [part.strip().rstrip("/") for part in raw.split(",") if part.strip()]


CORS_ORIGINS = list(dict.fromkeys(_csv("CORS_ORIGINS", "http://localhost:3000") + EXTRA_CORS_ORIGINS))
CORS_ORIGIN_REGEX = os.getenv("CORS_ORIGIN_REGEX", "").strip() or None

JWT_SECRET = os.getenv("JWT_SECRET", "")
# Optional per-tenant secrets (B1). When unset, jwt_secret_for() falls back to
# JWT_SECRET so existing App Platform env keeps working after deploy.
# Rotate by setting JWT_SECRET_MADRASAH / _TOKO / _MARKETPLACE_ERP
# (can start equal to JWT_SECRET, then change after re-login window).
JWT_SECRET_MADRASAH = os.getenv("JWT_SECRET_MADRASAH", "").strip()
JWT_SECRET_TOKO = os.getenv("JWT_SECRET_TOKO", "").strip()
JWT_SECRET_MARKETPLACE_ERP = os.getenv("JWT_SECRET_MARKETPLACE_ERP", "").strip()
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", str(24 * 7)))

# Stable tenant ids used as JWT aud (and in iss = "sm85:<tenant>").
JWT_TENANT_MADRASAH = "madrasah"
JWT_TENANT_TOKO = "toko"
JWT_TENANT_MARKETPLACE_ERP = "marketplace_erp"
JWT_TENANT_BUMI_LESTARI = "bumi_lestari"


def jwt_issuer_for(tenant: str) -> str:
    return f"sm85:{tenant}"

COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").strip().lower() in {"1", "true", "yes"}
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "none").strip().lower() or "none"
COOKIE_PATH = os.getenv("COOKIE_PATH", "/")


def public_role(raw: str | None) -> str:
    return (raw or "").strip().lower()


def origin_allowed(origin: str | None) -> bool:
    if not origin:
        return False
    normalized = origin.strip().rstrip("/")
    if normalized in CORS_ORIGINS:
        return True
    if CORS_ORIGIN_REGEX:
        try:
            return re.match(CORS_ORIGIN_REGEX, origin) is not None
        except re.error:
            return False
    return False
