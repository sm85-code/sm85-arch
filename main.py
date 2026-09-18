"""FastAPI application entrypoint (modular monolith)."""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from time import monotonic

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from adapters.api.v1 import siabumdes_router, uu05_inventory_router
from shared.config import APP_TITLE, CORS_ORIGINS
from shared.database import engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("sm85.audit")

app = FastAPI(title=APP_TITLE, version="0.6.0")
_login_attempts: dict[str, deque[float]] = defaultdict(deque)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS or ["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-CSRF-Token"],
)

app.include_router(siabumdes_router.router)
app.include_router(uu05_inventory_router.router)


@app.middleware("http")
async def validate_csrf_origin(request: Request, call_next):
    """Reject cross-site mutations; rate-limit login."""
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.url.path.endswith("/auth/login"):
            now = monotonic()
            host = request.client.host if request.client else "unknown"
            attempts = _login_attempts[host]
            while attempts and now - attempts[0] > 60:
                attempts.popleft()
            if len(attempts) >= 10:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Terlalu banyak percobaan login"},
                )
            attempts.append(now)
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        source = origin or (referer and "/".join(referer.split("/")[:3]))
        if source and CORS_ORIGINS and source not in CORS_ORIGINS:
            return JSONResponse(
                status_code=403,
                content={"detail": "Permintaan lintas situs ditolak"},
            )
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.exception("unhandled method=%s path=%s", request.method, request.url.path)
        response = JSONResponse(
            status_code=500,
            content={
                "detail": "Terjadi kesalahan internal pada server",
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
                "path": request.url.path,
            },
        )
        origin = request.headers.get("origin")
        if origin and origin in CORS_ORIGINS:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        logger.info(
            "mutation method=%s path=%s status=%s",
            request.method,
            request.url.path,
            response.status_code,
        )
    return response


@app.get("/health")
async def health():
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except Exception as exc:
        logger.exception("database health check failed")
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "database": "unavailable",
                "error_type": type(exc).__name__,
            },
        )


@app.get("/")
async def root():
    return {"app": APP_TITLE, "version": "0.6.0"}
