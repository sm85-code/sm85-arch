"""Opt-in drop-off dispatch. One durable attempt per order, never automatic replay."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func, update
from fastapi import HTTPException
from . import services
from ..infrastructure.models import AutoOrderPolicy, AutoOrderAttempt, AkunMarketplace, Pesanan, UserMarketplaceErp, ItemPesanan, StokReservasi
from ..infrastructure.adapters import erp_shopee

logger = logging.getLogger(__name__)


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
        await session.commit()
        return None
    ident = row.id
    session.add(AutoOrderAttempt(pesanan_id=ident, akun_id=row.akun_id, status="running", message="Memeriksa kesiapan drop off."))
    await session.commit()  # Durable claim precedes every provider call.
    return ident


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
        except Exception as exc:
            await session.rollback()
            attempt = await session.get(AutoOrderAttempt, ident)
            attempt.status = "unknown" if sent else "blocked"
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
