"""Durable EasyBoss → existing manual-order service. All writes are local ERP transactions."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select, text

from tenants.marketplace_erp.modules.marketplace_erp.application import pesanan_manual
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import easyboss as eb
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    BridgeOrder, BridgeRun, BridgeState, Pesanan, TokoPenjualan,
)

PLATFORMS = {"lazada", "tiktokshop"}
_local_lock = asyncio.Lock()
LOCK_ID = 8520261106


def now():
    return datetime.now(timezone.utc)


@asynccontextmanager
async def exclusive(session):
    """Dedicated PostgreSQL session lock survives per-order commits; released on disconnect/restart.

    Local lock is only for SQLite tests. PostgreSQL is mandatory in the actual ERP.
    """
    if session.bind.dialect.name == "postgresql":
        async with session.bind.connect() as conn:
            ok = await conn.scalar(text("SELECT pg_try_advisory_lock(:id)"), {"id": LOCK_ID})
            await conn.commit()
            if not ok:
                raise HTTPException(409, "Sinkronisasi sedang berjalan")
            try:
                yield
            finally:
                await conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": LOCK_ID})
                await conn.commit()
    else:
        if _local_lock.locked():
            raise HTTPException(409, "Sinkronisasi sedang berjalan")
        async with _local_lock:
            yield


async def state(session):
    row = await session.get(BridgeState, "easyboss")
    if row is None:
        row = BridgeState(id="easyboss", enabled=False)
        session.add(row)
        await session.flush()
    return row


async def shop_plan(session, shops):
    existing = list((await session.scalars(select(TokoPenjualan))).all())
    result, seen = [], set()
    for shop in shops:
        if shop.platform not in PLATFORMS:
            continue
        key = (shop.platform, shop.shop_id)
        if key in seen:
            raise eb.BridgeProblem("duplicate_shop_identity")
        seen.add(key)
        matched = [t for t in existing if t.jenis == "internal" and t.platform == shop.platform and
                   (t.easyboss_shop_id == shop.shop_id or (shop.marketplace_shop_id and
                    t.marketplace_shop_id == shop.marketplace_shop_id))]
        if any(t.easyboss_shop_id and t.easyboss_shop_id != shop.shop_id for t in matched):
            raise eb.BridgeProblem("conflicting_shop_identity")
        candidate = [t for t in existing if t.platform == shop.platform and t.nama_key == shop.name.casefold()]
        action = "update" if len(matched) == 1 else "review" if matched or candidate else "create"
        result.append({"shop": shop.model_dump(), "action": action,
                       "toko_id": matched[0].id if len(matched) == 1 else None,
                       "candidates": [t.id for t in candidate if t.jenis == "internal" and not t.easyboss_shop_id]})
    unresolved = [t.id for t in existing if t.jenis == "internal" and t.platform in PLATFORMS and not t.easyboss_shop_id]
    return {"items": result, "manual_review": unresolved}


async def sync_shops(session, gateway, *, preview=True, links=None, expected=None):
    async with exclusive(session):
        plan = await shop_plan(session, await gateway.shops())
        digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
        if preview:
            return {**plan, "fingerprint": digest}
        if digest != expected:
            raise HTTPException(409, "Pratinjau berubah; periksa ulang sebelum sinkronisasi")
        links = links or {}
        rows = {t.id: t for t in (await session.scalars(select(TokoPenjualan))).all()}
        used = set()
        if len(set(links.values())) != len(links):
            raise HTTPException(422, "Satu toko ERP tidak boleh dipetakan ke dua toko EasyBoss")
        for item in plan["items"]:
            shop = eb.Shop.model_validate(item["shop"])
            chosen = links.get(shop.platform + ":" + shop.shop_id)
            if chosen:
                t = rows.get(chosen)
                if chosen in used or not t or t.jenis != "internal" or t.platform != shop.platform or t.easyboss_shop_id:
                    raise HTTPException(422, "Pemetaan toko tidak valid")
                if item["action"] == "update":
                    raise HTTPException(422, "Identitas toko sudah terhubung")
                used.add(chosen)
            elif item["action"] == "review":
                continue
            elif item["toko_id"]:
                t = rows[item["toko_id"]]
            else:
                t = TokoPenjualan(jenis="internal", platform=shop.platform, nama=shop.name,
                                  nama_key=shop.name.casefold(), keterangan="", aktif=shop.active)
                session.add(t)
            # Never silently re-enable a shop disabled by an operator.
            t.nama, t.nama_key = shop.name, shop.name.casefold()
            t.sumber, t.easyboss_shop_id = "easyboss", shop.shop_id
            t.marketplace_shop_id, t.sinkron_at = shop.marketplace_shop_id, now()
            t.sinkron_status = "synced" if shop.active else "source_inactive"
            if not shop.active:
                t.aktif = False
        for id_ in plan["manual_review"]:
            if id_ not in used:
                rows[id_].sinkron_status = "needs_review"
        session.add(BridgeRun(kind="shops", status="success", finished_at=now()))
        await session.commit()
        return {**plan, "fingerprint": digest}


def identity(order):
    return (order.platform, order.shop_id, order.order_id)


def code(order):
    # Manual order codes have global uniqueness; source display number remains in reference + metadata.
    return "EB-" + hashlib.sha256(json.dumps(identity(order)).encode()).hexdigest()


def operational_snapshot(snapshot):
    # Display-only enrichment must not flag unchanged production orders for review.
    display = {"payment_method", "city", "order_total", "buyer_paid", "currency",
               "recipient_name", "recipient_phone", "recipient_address"}
    result = {k: v for k, v in snapshot.items() if k not in display}
    result.setdefault("cod", False)
    result.setdefault("cod_eligible", False)
    if isinstance(result.get("items"), list):
        result["items"] = [{k: v for k, v in line.items() if k not in {"unit_price", "line_total", "image_url"}}
                           for line in result["items"]]
    return result


async def ingest(session, order):
    key = identity(order)
    row = await session.scalar(select(BridgeOrder).where(BridgeOrder.platform == key[0],
                              BridgeOrder.shop_id == key[1], BridgeOrder.order_id == key[2]).with_for_update())
    if row is None:
        row = BridgeOrder(platform=key[0], shop_id=key[1], order_id=key[2], status="pending",
                          source_status=getattr(order, "source_status", ""), snapshot_json="{}")
        session.add(row)
        await session.flush()
    row.source_status, row.updated_at = getattr(order, "source_status", ""), now()
    row.snapshot_json = order.model_dump_json()
    row.attempts += 1
    if isinstance(order, eb.RejectedOrder):
        row.error = "invalid_source_order"
        row.status = "review" if row.pesanan_id else "failed"
        return "failed"
    if row.pesanan_id:
        previous = await session.get(Pesanan, row.pesanan_id)
        original = json.loads(previous.detail_json).get("easyboss", {})
        if order.status == "cancelled" or operational_snapshot(original) != operational_snapshot(order.model_dump(mode="json")):
            row.status, row.error = "review", "source_changed_after_import"
        return "skipped"  # Never mutate production work after source changes.
    if order.status != "ready" or not (order.paid is True or (order.cod is True and order.cod_eligible is True)):
        row.status, row.error = "skipped", "ineligible_status"
        return "skipped"
    reason = None
    if not order.complete:
        reason = "incomplete_order_snapshot"
    elif order.ordered_at is None:
        reason = "missing_ordered_at"
    elif order.deadline is None:
        reason = "missing_deadline"
    elif not order.buyer:
        reason = "missing_buyer"
    elif not order.items:
        reason = "missing_items"
    elif len({i.item_id for i in order.items}) != len(order.items):
        reason = "duplicate_item_identity"
    shop = await session.scalar(select(TokoPenjualan).where(TokoPenjualan.platform == order.platform,
                                TokoPenjualan.easyboss_shop_id == order.shop_id, TokoPenjualan.jenis == "internal"))
    if not shop or not shop.aktif or shop.sinkron_status != "synced":
        reason = "shop_unmapped_or_inactive"
    if reason:
        row.status, row.error = "failed", reason
        return "failed"
    row.toko_id = shop.id
    try:
        payload = pesanan_manual.PesananManualIn(
            toko_id=shop.id, kode_pesanan=code(order), pemesan=order.buyer,
            ordered_at=order.ordered_at, deadline=order.deadline, reference=order.order_id,
            items=[pesanan_manual.ItemManual(produk=i.product, varian=i.variant, qty=i.quantity,
                                            note="\n".join(x for x in [i.note, order.buyer_note] if x)) for i in order.items])
    except ValueError:
        row.status, row.error = "failed", "invalid_manual_order"
        return "failed"
    pesanan = await pesanan_manual.buat(session, payload)
    detail = json.loads(pesanan.detail_json)
    detail["easyboss"] = order.model_dump(mode="json")
    pesanan.detail_json = json.dumps(detail)
    row.pesanan_id, row.status, row.error = pesanan.id, "imported", None
    await session.flush()  # Source identity, items and Pesanan commit together in caller.
    return "imported"


async def run_orders(session, gateway, *, retry=False, automatic=False):
    async with exclusive(session):
        st = await state(session)
        if automatic and not st.enabled:
            await session.commit()
            return {"status": "paused"}
        # A previous running row under an acquired exclusive lock means its process died.
        for stale in (await session.scalars(select(BridgeRun).where(BridgeRun.status == "running"))).all():
            stale.status, stale.error, stale.finished_at = "interrupted", "process_interrupted", now()
        run = BridgeRun(kind="retry" if retry else "orders")
        session.add(run)
        st.heartbeat = now()
        started = now()
        await session.commit()
        try:
            # Full scan deliberately avoids losing failed, moved-tab, and delayed source records.
            # Checkpoint is diagnostic until upstream changed-since guarantees are verified.
            from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters.easyboss_mcp import MCPGateway
            if isinstance(gateway, MCPGateway):
                gateway.tracked_orders = list((await session.execute(select(
                    BridgeOrder.platform, BridgeOrder.shop_id, BridgeOrder.order_id).where(
                    (BridgeOrder.pesanan_id.is_not(None)) | (BridgeOrder.status.in_(["failed", "review"]))
                ))).all())
            orders = await gateway.orders(None)
            unique, conflicts = {}, set()
            for order in orders:
                if order.platform not in PLATFORMS:
                    continue
                key = identity(order)
                if key in unique and unique[key] != order:
                    conflicts.add(key)
                unique[key] = order
            failures = set((await session.execute(select(BridgeOrder.platform, BridgeOrder.shop_id, BridgeOrder.order_id)
                           .where(BridgeOrder.status == "failed"))).all()) if retry else None
            for key, order in unique.items():
                if failures is not None and key not in failures:
                    continue
                if key in conflicts:
                    order = eb.RejectedOrder(platform=order.platform, shop_id=order.shop_id, order_id=order.order_id)
                try:
                    async with session.begin_nested():
                        outcome = await ingest(session, order)
                except Exception:
                    # No upstream payloads, credentials, buyer names or SQL parameters in logs/errors.
                    row = await session.scalar(select(BridgeOrder).where(BridgeOrder.platform == key[0],
                         BridgeOrder.shop_id == key[1], BridgeOrder.order_id == key[2]))
                    if row is None:
                        row = BridgeOrder(platform=key[0], shop_id=key[1], order_id=key[2],
                                          source_status=getattr(order, "source_status", ""), snapshot_json=order.model_dump_json())
                        session.add(row)
                    row.status, row.error = "failed", "import_transaction_failed"
                    row.attempts = (row.attempts or 0) + 1
                    outcome = "failed"
                audited = await session.scalar(select(BridgeOrder).where(BridgeOrder.platform == key[0],
                         BridgeOrder.shop_id == key[1], BridgeOrder.order_id == key[2]))
                if audited:
                    events = json.loads(audited.audit_json or "[]")
                    event = {"source_hash": hashlib.sha256(audited.snapshot_json.encode()).hexdigest(),
                             "status": audited.status, "source_status": audited.source_status,
                             "error": audited.error, "pesanan_id": audited.pesanan_id}
                    if not events or any(events[-1].get(k) != v for k, v in event.items()):
                        events.append({**event, "at": now().isoformat(), "run_id": run.id})
                        audited.audit_json = json.dumps(events)
                setattr(run, outcome, getattr(run, outcome) + 1)
                await session.commit()
            st.checkpoint, run.status = started, "partial" if run.failed else "success"
        except Exception as exc:
            run.status, run.error = "failed", str(exc) if isinstance(exc, eb.BridgeProblem) else "sync_failed"
        run.finished_at, st.heartbeat = now(), now()
        await session.commit()
        return run_dict(run)


def run_dict(row):
    return {k: getattr(row, k) for k in ("id", "kind", "status", "started_at", "finished_at", "imported", "skipped", "failed", "error")}


async def dashboard(session):
    st = await session.get(BridgeState, "easyboss")
    runs = (await session.scalars(select(BridgeRun).order_by(BridgeRun.started_at.desc()).limit(50))).all()
    errors = (await session.scalars(select(BridgeOrder).where(BridgeOrder.error.is_not(None))
              .order_by(BridgeOrder.updated_at.desc()).limit(100))).all()
    totals = dict((await session.execute(select(BridgeOrder.status, func.count()).group_by(BridgeOrder.status))).all())
    totals["imported"] = await session.scalar(select(func.count()).select_from(BridgeOrder).where(BridgeOrder.pesanan_id.is_not(None)))
    heartbeat = st.heartbeat if st else None
    healthy = bool(heartbeat and now() - heartbeat.replace(tzinfo=timezone.utc) < timedelta(seconds=180))
    return {"configured": eb.configured(), "connection": "unverified" if not eb.configured() else
            ("ok" if runs and runs[0].status == "success" else "unknown"),
            "scheduler": "disabled" if os.getenv("EASYBOSS_BRIDGE_SCHEDULER") != "1" else
            "paused" if not st or not st.enabled else "running" if healthy else "stale",
            "enabled": bool(st and st.enabled), "heartbeat": heartbeat,
            "last_sync": st.checkpoint if st else None,
            "shops": await session.scalar(select(func.count()).select_from(TokoPenjualan).where(TokoPenjualan.sumber == "easyboss")),
            "counts": totals, "runs": [run_dict(r) for r in runs],
            "errors": [{k: getattr(r, k) for k in ("id", "platform", "shop_id", "order_id", "status", "error", "attempts", "updated_at")} for r in errors]}


async def run_forever(factory):
    interval = max(60, int(os.getenv("EASYBOSS_BRIDGE_INTERVAL_SECONDS", "60")))
    while True:
        try:
            async with factory() as session:
                async with eb.client() as client:
                    await run_orders(session, eb.Gateway(client), automatic=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # Next tick retries; no secret-bearing exception strings are logged.
        await asyncio.sleep(interval)
