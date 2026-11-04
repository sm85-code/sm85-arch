"""Opt-in drop-off dispatch. One durable attempt per order, never automatic replay.

Exception: Shopee may report READY_TO_SHIP while the package is still pending
("Tertunda", e.g. fraud/payment review) and refuses get_shipping_parameter.
That refusal happens before anything is sent, so the attempt is parked as
"waiting" and re-checked every WAIT_RECHECK until the package is ready, for up
to WAIT_WINDOW after the order time (pending can last ~24h). No cap applies
while pending; once Shopee accepts the request the normal single attempt runs.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func, update
from fastapi import HTTPException
from . import services
from ..infrastructure.models import AutoOrderPolicy, AutoOrderAttempt, AkunMarketplace, Pesanan, UserMarketplaceErp, ItemPesanan, StokReservasi
from ..infrastructure.adapters import erp_shopee

logger = logging.getLogger(__name__)

WAIT_RECHECK = timedelta(minutes=5)
WAIT_WINDOW = timedelta(days=3)
PENDING_MARKERS = ("can only be obtained when package is ready to be shipped",)


def is_pending_error(message) -> bool:
    text = str(message or "").lower()
    return any(m in text for m in PENDING_MARKERS)


def now():
    return datetime.now(timezone.utc)


async def claim(session):
    await session.execute(update(AutoOrderAttempt).where(AutoOrderAttempt.status == "running", AutoOrderAttempt.created_at < now() - timedelta(minutes=10)).values(status="unknown", message="Proses terputus. Periksa Seller Centre; tidak diulang otomatis."))
    row = (await session.execute(select(Pesanan).join(AutoOrderPolicy, AutoOrderPolicy.akun_id == Pesanan.akun_id).join(AkunMarketplace, AkunMarketplace.id == Pesanan.akun_id).where(
        AutoOrderPolicy.enabled.is_(True), AkunMarketplace.status != "nonaktif", Pesanan.platform == "shopee",
        Pesanan.status == "to_ship", Pesanan.status_marketplace == "READY_TO_SHIP",
        func.coalesce(Pesanan.dipesan_at, Pesanan.created_at) >= AutoOrderPolicy.enabled_at,
        ~select(AutoOrderAttempt.pesanan_id).where(AutoOrderAttempt.pesanan_id == Pesanan.id).exists(),
    ).order_by(Pesanan.created_at, Pesanan.id).limit(1).with_for_update(of=Pesanan, skip_locked=True))).scalar_one_or_none()
    if row is None:
        ident = await claim_waiting(session)
        await session.commit()
        return ident
    ident = row.id
    session.add(AutoOrderAttempt(pesanan_id=ident, akun_id=row.akun_id, status="running", message="Memeriksa kesiapan drop off."))
    await session.commit()  # Durable claim precedes every provider call.
    return ident


async def claim_waiting(session):
    """Re-claim an order parked while Shopee still had it pending (Tertunda)."""
    current = now()
    pending_blocked = (AutoOrderAttempt.status == "blocked") & AutoOrderAttempt.message.contains(PENDING_MARKERS[0])
    row = (await session.execute(select(AutoOrderAttempt).join(Pesanan, Pesanan.id == AutoOrderAttempt.pesanan_id).join(AutoOrderPolicy, AutoOrderPolicy.akun_id == Pesanan.akun_id).join(AkunMarketplace, AkunMarketplace.id == Pesanan.akun_id).where(
        AutoOrderPolicy.enabled.is_(True), AkunMarketplace.status != "nonaktif", Pesanan.platform == "shopee",
        Pesanan.status == "to_ship", Pesanan.status_marketplace == "READY_TO_SHIP",
        func.coalesce(Pesanan.dipesan_at, Pesanan.created_at) >= AutoOrderPolicy.enabled_at,
        func.coalesce(Pesanan.dipesan_at, Pesanan.created_at) >= current - WAIT_WINDOW,
        (AutoOrderAttempt.status == "waiting") | pending_blocked,
        AutoOrderAttempt.updated_at <= current - WAIT_RECHECK,
    ).order_by(AutoOrderAttempt.updated_at, AutoOrderAttempt.pesanan_id).limit(1).with_for_update(of=AutoOrderAttempt, skip_locked=True))).scalar_one_or_none()
    if row is None:
        return None
    row.status, row.message = "running", "Memeriksa ulang kesiapan drop off (sebelumnya tertunda di Shopee)."
    row.created_at = current  # Reaper timeout counts from this re-claim.
    logger.info("Auto-order recheck pesanan=%s akun=%s (previously pending at Shopee)", row.pesanan_id, row.akun_id)
    return row.pesanan_id


async def run_one(factory, ident):
    async with factory() as session:
        sent = False
        attempt = await session.get(AutoOrderAttempt, ident)
        if not attempt or attempt.status != "running":
            return
        try:
            order = (await session.execute(select(Pesanan).where(Pesanan.id == ident).with_for_update())).scalar_one()
            shop = (await session.execute(select(AkunMarketplace).where(AkunMarketplace.id == order.akun_id).with_for_update())).scalar_one_or_none()
            policy = (await session.execute(select(AutoOrderPolicy).where(AutoOrderPolicy.akun_id == order.akun_id).with_for_update())).scalar_one_or_none()
            actor = await session.get(UserMarketplaceErp, policy.enabled_by) if policy and policy.enabled_by else None
            if not policy or not policy.enabled or not actor or actor.role != "admin" or not shop or shop.status == "nonaktif":
                raise HTTPException(409, "Izin otomatis tidak aktif; proses manual bila diperlukan.")
            after_activation = await session.scalar(select(func.coalesce(Pesanan.dipesan_at, Pesanan.created_at) >= policy.enabled_at).where(Pesanan.id == ident))
            if not after_activation:
                raise HTTPException(409, "Pesanan mendahului izin aktif terbaru; proses manual bila diperlukan.")
            if order.status != "to_ship" or order.status_marketplace != "READY_TO_SHIP":
                raise HTTPException(409, "Pesanan tidak lagi siap diproses.")
            if (await services.get_settings(session))["gudang_aktif"]:
                items = (await session.execute(select(ItemPesanan).where(ItemPesanan.pesanan_id == ident))).scalars().all()
                reserved = (await session.execute(select(StokReservasi).where(StokReservasi.pesanan_id == ident, StokReservasi.status == "aktif"))).scalars().all()
                needed, held = {}, {}
                for item in items:
                    if not item.produk_id:
                        raise HTTPException(409, "SKU belum terhubung ke gudang; periksa stok secara manual.")
                    needed[item.produk_id] = needed.get(item.produk_id, 0) + item.qty
                for row in reserved:
                    held[row.produk_id] = held.get(row.produk_id, 0) + row.qty
                if not needed or any(held.get(key, 0) < qty for key, qty in needed.items()):
                    raise HTTPException(409, "Reservasi stok belum cukup; periksa secara manual.")
            settings = {"metode": "dropoff"}
            if policy.branch_id:
                settings["branch_id"] = policy.branch_id
            if policy.sender_real_name:
                settings["sender_real_name"] = policy.sender_real_name
            options = await erp_shopee.opsi_pengiriman(session, shop, order.id_eksternal)
            available = next((o for o in options["opsi"] if o["metode"] == "dropoff"), None)
            if not available or not available["tersedia"]:
                raise HTTPException(409, "Drop off tidak tersedia. Tidak dialihkan ke pickup otomatis.")
            # Do not guess a branch or a legal sender name when Shopee requires it.
            if "branch_id" in available["wajib"] and not policy.branch_id:
                raise HTTPException(409, "Isi ID cabang drop off pada pengaturan toko, atau proses manual.")
            if "sender_real_name" in available["wajib"] and not policy.sender_real_name:
                raise HTTPException(409, "Isi nama pengirim pada pengaturan toko, atau proses manual.")
            sent = True  # Any subsequent failure is conservative, including a disconnect.
            async with asyncio.timeout(90):
                await services.proses_pesanan_marketplace(session, ident, settings, auto_authorized=True)
            attempt.status, attempt.message = "succeeded", "Pengiriman drop off diproses. Siapkan paket dan serahkan ke gerai."
            await session.commit()
            logger.info("Auto-order pesanan=%s result=succeeded", ident)
        except Exception as exc:
            await session.rollback()
            attempt = await session.get(AutoOrderAttempt, ident)
            detail = str(getattr(exc, "detail", "") or exc)
            if not sent and is_pending_error(detail):
                attempt.status = "waiting"
                attempt.message = "Pesanan masih tertunda di Shopee; dicoba lagi otomatis setelah siap dikirim."
                logger.info("Auto-order pesanan=%s still pending at Shopee; will recheck", ident)
                await session.commit()
                return
            attempt.status = "unknown" if sent else "blocked"
            logger.info("Auto-order pesanan=%s result=%s", ident, attempt.status)
            attempt.message = ("Hasil pengiriman belum pasti. Periksa Seller Centre sebelum tindakan lain." if sent else str(getattr(exc, "detail", "Pemeriksaan gagal; proses manual setelah diperiksa.")))[:1000]
            await session.commit()
        except asyncio.CancelledError:
            # Durable running receipt blocks replay after shutdown; reaper marks unknown.
            raise


async def run_forever(factory):
    while True:
        await asyncio.sleep(60)
        if not erp_shopee.live_sync_enabled():
            continue
        try:
            logger.info("Auto-order sweep tick")
            for _ in range(10):
                async with factory() as session:
                    ident = await claim(session)
                if ident is None:
                    break
                await run_one(factory, ident)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Automatic Shopee drop-off round failed")
