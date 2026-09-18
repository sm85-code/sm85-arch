"""Centralized runtime configuration for App Platform + local dev."""
from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

READ_LEVEL = ("admin", "direktur", "bendahara", "pengawas", "penasihat")
REPORT_READ_LEVEL = READ_LEVEL + ("pengelola",)
WRITE_LEVEL = ("admin", "direktur", "bendahara", "pengelola")
ADMIN_LEVEL = ("admin",)
READONLY_ROLES = ("pengawas", "penasihat")

API_PREFIX = "/api"
APP_TITLE = os.getenv("APP_TITLE", "SM85 Arch API")

# Frontend public role names. Internal aliases are mapped on the way out.
PUBLIC_ROLES = (
    "admin",
    "direktur",
    "bendahara",
    "pengelola",
    "pengawas",
    "penasihat",
)
ROLE_ALIASES_TO_PUBLIC = {
    "unit_manager": "pengelola",
    "unit-manager": "pengelola",
    "manager": "pengelola",
    "pengelola_unit": "pengelola",
}


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [part.strip().rstrip("/") for part in raw.split(",") if part.strip()]


CORS_ORIGINS = _csv("CORS_ORIGINS", "http://localhost:3000")
CORS_ORIGIN_REGEX = os.getenv("CORS_ORIGIN_REGEX", "").strip() or None

JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", str(24 * 7)))
JWT_COOKIE_NAME = os.getenv("JWT_COOKIE_NAME", "bumdes_token")

# Split-host App Platform deploy: third-party cookie requires None + Secure.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").strip().lower() in {"1", "true", "yes"}
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "none").strip().lower() or "none"
COOKIE_PATH = os.getenv("COOKIE_PATH", "/")

POSTGRES_SSL = os.getenv("POSTGRES_SSL", "true").strip().lower() in {"1", "true", "yes"}


def public_role(raw: str | None) -> str:
    """Map stored/internal role names to the frontend contract."""
    value = (raw or "").strip().lower()
    return ROLE_ALIASES_TO_PUBLIC.get(value, value)


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
