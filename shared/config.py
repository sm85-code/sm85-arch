"""Centralized runtime configuration."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

READ_LEVEL = ("admin", "direktur", "bendahara", "pengawas", "penasihat")
REPORT_READ_LEVEL = READ_LEVEL + ("pengelola",)
WRITE_LEVEL = ("admin", "direktur", "bendahara")
ADMIN_LEVEL = ("admin",)
READONLY_ROLES = ("pengawas", "penasihat")

API_PREFIX = "/api"
APP_TITLE = os.getenv("APP_TITLE", "SM85 Arch API")

CORS_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]

JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", str(24 * 7)))
JWT_COOKIE_NAME = os.getenv("JWT_COOKIE_NAME", "bumdes_token")
