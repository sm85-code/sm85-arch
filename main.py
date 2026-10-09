"""FastAPI application entrypoint (modular monolith).

Serves the madrasah, marketplace_erp, store and bumi_lestari tenants.
SIABUMDES moved to its own service: sm85-code/backend-siabumdes.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from time import monotonic
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from tenants.bumi_lestari.adapters.api.v1.bumi_lestari_router import bumi_lestari_router
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import database as bumi_lestari_database
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.seeder import ensure_bumi_lestari_schema
from tenants.madrasah.adapters.api.v1.madrasah_router import madrasah_router
from tenants.madrasah.modules.madrasah.infrastructure import database as madrasah_database
from tenants.madrasah.modules.madrasah.infrastructure.seeder import ensure_madrasah_schema
from tenants.marketplace_erp.adapters.api.v1.marketplace_erp_router import marketplace_erp_router
from tenants.marketplace_erp.modules.marketplace_erp.application import auto_sync as marketplace_erp_auto_sync
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure import database as marketplace_erp_database
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.seeder import ensure_marketplace_erp_schema
from tenants.store.adapters.api.v1.store_admin_router import store_admin_router
from tenants.store.adapters.api.v1.store_buyer_router import store_buyer_router
from tenants.store.modules.store.infrastructure import database as store_database
from tenants.store.modules.store.infrastructure.seeder import ensure_store_schema
from shared.config import APP_TITLE, CORS_ORIGIN_REGEX, CORS_ORIGINS, origin_allowed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("sm85.audit")


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        # Each tenant's startup step is isolated: a failure repairing the
        # madrasah schema (e.g. DATABASE_URL_MADRASAH unset) must not affect,
        # and is not affected by, the other tenants' startup.
        await ensure_madrasah_schema()
    except Exception:
        logger.exception("madrasah schema repair failed — app continues")
    try:
        # Same isolation for marketplace_erp: adds columns introduced after
        # first deploy (mpe_users.must_change_password) and flags a seeded
        # owner still on the default password. No-op when
        # DATABASE_URL_MARKETPLACE_ERP is unset.
        await ensure_marketplace_erp_schema()
    except Exception:
        logger.exception("marketplace_erp schema repair failed — app continues")
    try:
        # Store: adds the product slug column (SEO URLs) to databases created before it existed and backfills it.
        await ensure_store_schema()
    except Exception:
        logger.exception("store schema repair failed — app continues")
    try:
        await ensure_bumi_lestari_schema()
    except Exception:
        logger.exception("bumi_lestari schema repair failed — app continues")
    # Marketplace ERP: pull Shopee orders in the background (SHOPEE_AUTO_SYNC_MINUTES, 0 = off).
    tugas_sinkron = None
    interval = marketplace_erp_auto_sync.interval_seconds()
    if interval and marketplace_erp_database.SessionLocal is not None:
        tugas_sinkron = asyncio.create_task(
            marketplace_erp_auto_sync.jalankan_berkala(marketplace_erp_database.SessionLocal, interval)
        )
    media_task = None
    if store_database.SessionLocal is not None:
        from tenants.store.modules.store.application.media_cleanup import run_forever
        media_task = asyncio.create_task(run_forever(store_database.SessionLocal))
    assistant_task = None
    if marketplace_erp_database.SessionLocal is not None:
        from tenants.marketplace_erp.modules.marketplace_erp.application.assistant.service import run_forever as run_assistant
        assistant_task = asyncio.create_task(run_assistant(marketplace_erp_database.SessionLocal))
    try:
        yield
    finally:
        if assistant_task is not None:
            assistant_task.cancel()
        if media_task is not None:
            media_task.cancel()
        if tugas_sinkron is not None:
            tugas_sinkron.cancel()


app = FastAPI(title=APP_TITLE, version="1.0.0", lifespan=lifespan)
_login_attempts: dict[str, deque[float]] = defaultdict(deque)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-CSRF-Token", "X-Requested-With"],
    expose_headers=["Content-Disposition", "X-Request-ID"],
)

app.include_router(madrasah_router, prefix="/api/madrasah", tags=["Madrasah"])
app.include_router(marketplace_erp_router, prefix="/api/marketplace-erp", tags=["Marketplace ERP"])
app.include_router(bumi_lestari_router, prefix="/api/bumi-lestari", tags=["Bumi Lestari"])
app.include_router(store_admin_router, prefix="/api/store/admin", tags=["Store Admin"])
app.include_router(store_buyer_router, prefix="/api/store/buyer", tags=["Store Buyer"])


def _apply_cors(response, origin: str | None) -> None:
    if origin and origin_allowed(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Vary"] = "Origin"


class CsrfOriginMiddleware(BaseHTTPMiddleware):
    """Reject unknown-origin mutations; rate-limit login. Never block CORS preflight.

    Class-based (BaseHTTPMiddleware) instead of the @app.middleware("http")
    decorator: that decorator is deprecated and removed in Starlette 1.0, and
    this middleware guards the cookie-based auth every frontend (diniyah/
    madrasah, marketplace_erp, store) depends on -- it must keep working across a starlette bump.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if request.url.path.endswith("/auth/login"):
                now = monotonic()
                host = request.client.host if request.client else "unknown"
                attempts = _login_attempts[host]
                while attempts and now - attempts[0] > 60:
                    attempts.popleft()
                if len(attempts) >= 10:
                    response = JSONResponse(
                        status_code=429,
                        content={"detail": "Terlalu banyak percobaan login"},
                    )
                    _apply_cors(response, request.headers.get("origin"))
                    return response
                attempts.append(now)
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            source = origin or (referer and "/".join(referer.split("/")[:3]))
            if source and CORS_ORIGINS and not origin_allowed(source):
                response = JSONResponse(
                    status_code=403,
                    content={"detail": "Permintaan lintas situs ditolak"},
                )
                return response
        try:
            response = await call_next(request)
        except Exception:
            tracking_id = uuid4().hex
            logger.exception("unhandled tracking_id=%s method=%s", tracking_id, request.method)
            response = JSONResponse(
                status_code=500,
                content={"detail": "Terjadi kesalahan internal pada server", "tracking_id": tracking_id},
                headers={"X-Request-ID": tracking_id},
            )
            _apply_cors(response, request.headers.get("origin"))
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            logger.info(
                "mutation method=%s path=%s status=%s",
                request.method,
                request.url.path,
                response.status_code,
            )
        return response


app.add_middleware(CsrfOriginMiddleware)


_TENANT_DB_MODULES = {
    "madrasah": madrasah_database,
    "marketplace_erp": marketplace_erp_database,
    "store": store_database,
    "bumi_lestari": bumi_lestari_database,
}
_HEALTH_DB_TIMEOUT_SECONDS = 3.0


async def _ping_tenant(name: str, module) -> str:
    """Ping one tenant engine. "not_configured" when its DATABASE_URL_* is unset."""
    engine = getattr(module, "engine", None)
    if engine is None:
        return "not_configured"

    async def _select_one() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(_select_one(), timeout=_HEALTH_DB_TIMEOUT_SECONDS)
        return "connected"
    except Exception as exc:
        logger.warning("health: %s database unavailable (%s)", name, type(exc).__name__)
        return "unavailable"


@app.get("/health")
async def health(db: bool = False):
    """Liveness for DO App Platform: always 200 while the process is up.

    Does not depend on any DATABASE_URL (the old SIABUMDES one is gone). By
    default it touches no database so platform health checks stay fast; pass
    ``?db=true`` to also ping every configured tenant database (reported as
    connected / unavailable / not_configured). Tenant database state never
    turns this into a non-200: one tenant being down or unset must not take
    the whole app out of rotation.
    """
    if not db:
        return {"status": "ok"}
    names = list(_TENANT_DB_MODULES)
    results = await asyncio.gather(*(_ping_tenant(n, _TENANT_DB_MODULES[n]) for n in names))
    return {"status": "ok", "databases": dict(zip(names, results))}


@app.get("/")
async def root():
    return {"app": APP_TITLE, "version": "1.0.0"}
