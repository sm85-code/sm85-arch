"""Background order sync: keeps ERP orders fresh even when nobody has the ERP open.

Started from main.py's lifespan. Every few minutes it runs the same incremental sync the Pesanan page
triggers (one cheap Shopee call per shop when nothing changed). Safe with several app workers: each shop
is claimed atomically (``klaim_sinkron_pesanan``), so a shop is synced by one worker per round.
"""
from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_MINUTES = 5.0
_ROUND_BUDGET_SECONDS = 240  # shops left over are picked up by the next round


def interval_seconds() -> float:
    """SHOPEE_AUTO_SYNC_MINUTES: minutes between rounds; 0 (or invalid) switches the background sync off."""
    raw = os.getenv("SHOPEE_AUTO_SYNC_MINUTES", str(DEFAULT_INTERVAL_MINUTES)).strip()
    try:
        minutes = float(raw)
    except ValueError:
        return 0.0
    return max(minutes, 0.0) * 60


async def sinkron_semua_toko(session: AsyncSession) -> list[dict]:
    """One round: incrementally sync every connected Shopee shop. [] when live sync is off."""
    if not erp_shopee.live_sync_enabled():
        return []
    akun_list = [
        a
        for a in await services.list_akun_marketplace(session, platform="shopee")
        if a.access_token and a.id_toko_eksternal
    ]
    return await services.sinkron_semua_pesanan(session, akun_list, batas_detik=_ROUND_BUDGET_SECONDS)


async def jalankan_berkala(session_factory, interval: float) -> None:
    """Loop forever (until cancelled): a failing round is logged and never stops the loop."""
    while True:
        await asyncio.sleep(interval)
        try:
            async with session_factory() as session:
                hasil = await sinkron_semua_toko(session)
            gagal = [h for h in hasil if h["hasil"] == "gagal"]
            if gagal:
                logger.warning("sinkron berkala: %d toko gagal: %s", len(gagal), [(h["nama_toko"], h["pesan"]) for h in gagal])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- keep the loop alive
            logger.exception("sinkron berkala gagal")
