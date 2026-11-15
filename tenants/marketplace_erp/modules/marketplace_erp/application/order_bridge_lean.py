"""Lean EasyBoss order-bridge scheduling: few upstream reads, hard daily cap, 429 backoff.

One round (default every 30 minutes):
  * reads page 1 of the 'all' tab per platform (TikTok, Lazada), newest first, and only pages further
    while every row on the page is still unknown;
  * fetches order details ONLY for orders that are new (inside the lookback window) or whose list
    signature (package status/id) changed since the last round;
  * never re-reads every tracked order; failed receipts are retried at most every few hours;
  * every upstream call spends from a persisted per-day (WIB) budget; at the cap the bridge stops
    for the rest of the day and alerts once;
  * an EasyBoss 429 (also when it is hidden in an HTTP-200 JSON body) stops the round and backs off
    for at least 60 minutes, without retry loops.
State lives in mpe_bridge_state.lean_json so restarts and extra workers cannot reset or double it.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import easyboss as eb
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import BridgeOrder

logger = logging.getLogger("easyboss_bridge")
WIB = timezone(timedelta(hours=7))
NATIVE = {"tiktokshop": "tiktok", "lazada": "lazada"}
SEEN_KEEP_DAYS = 14
# Problems that end the whole round (everything else is order-specific and skipped).
FATAL = {"upstream_rate_limited", "daily_cap_reached", "round_cap_reached", "mcp_read_failed", "mcp_list_contract"}


def _env_int(name, default, floor=None):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(floor, value) if floor is not None else value


def interval_seconds():
    return _env_int("EASYBOSS_BRIDGE_INTERVAL_SECONDS", 1800, floor=600)


def daily_cap():
    return _env_int("EASYBOSS_BRIDGE_DAILY_CAP", 1000, floor=1)


def round_max():
    return _env_int("EASYBOSS_BRIDGE_ROUND_MAX", 80, floor=4)


def backoff_minutes():
    return _env_int("EASYBOSS_BRIDGE_BACKOFF_MINUTES", 60, floor=60)


def lookback():
    return timedelta(hours=_env_int("EASYBOSS_BRIDGE_LOOKBACK_HOURS", 72, floor=1))


def max_pages():
    return _env_int("EASYBOSS_BRIDGE_MAX_PAGES", 3, floor=1)


def slow_every():
    return timedelta(hours=_env_int("EASYBOSS_BRIDGE_SLOW_HOURS", 6, floor=1))


def call_delay():
    try:
        return max(0.0, float(os.getenv("EASYBOSS_BRIDGE_CALL_DELAY_SECONDS", "2")))
    except ValueError:
        return 2.0


def alert_ratio():
    try:
        return min(1.0, max(0.1, float(os.getenv("EASYBOSS_BRIDGE_ALERT_RATIO", "0.8"))))
    except ValueError:
        return 0.8


def _ts(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def kstr(key):
    return "|".join(key)


def load(st):
    try:
        data = json.loads(st.lean_json or "{}")
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("seen", {})
    data.setdefault("alerts", [])
    return data


def dump(lean):
    return json.dumps(lean, sort_keys=True, default=str)


def rollover(lean, now):
    today = now.astimezone(WIB).date().isoformat()
    if lean.get("day") != today:
        lean.update(day=today, requests=0, alerted_near=False, alerted_cap=False)
    lean.setdefault("requests", 0)


def precheck(lean, *, now, automatic):
    """Return a reason to skip this round without touching EasyBoss, or None."""
    rollover(lean, now)
    until = _ts(lean.get("backoff_until"))
    if until and now < until:
        return "backoff"
    if lean["requests"] >= daily_cap():
        return "daily_cap"
    last = _ts(lean.get("last_round_at"))
    if automatic and last and now - last < timedelta(seconds=interval_seconds() - 60):
        return "not_due"
    return None


class Budget:
    """Spends one unit per upstream call; raises before the daily or per-round cap is exceeded."""

    def __init__(self, lean, now, *, cap=None, per_round=None):
        rollover(lean, now)
        self.lean, self.cap = lean, cap or daily_cap()
        self.per_round, self.round = per_round or round_max(), 0

    def spend(self):
        if self.lean["requests"] >= self.cap:
            if not self.lean.get("alerted_cap"):
                self.lean["alerted_cap"] = True
                self.lean["alerts"].append(
                    f"EasyBoss bridge berhenti: batas harian {self.cap} akses tercapai. Lanjut otomatis besok (WIB).")
            raise eb.BridgeProblem("daily_cap_reached")
        if self.round >= self.per_round:
            raise eb.BridgeProblem("round_cap_reached")
        self.lean["requests"] += 1
        self.round += 1
        if not self.lean.get("alerted_near") and self.lean["requests"] >= int(self.cap * alert_ratio()):
            self.lean["alerted_near"] = True
            self.lean["alerts"].append(
                f"Peringatan: EasyBoss bridge sudah {self.lean['requests']} dari batas {self.cap} akses hari ini.")


def back_off(lean, now):
    until = now + timedelta(minutes=backoff_minutes())
    lean["backoff_until"] = until.isoformat()
    lean["alerts"].append("EasyBoss menolak karena terlalu sering (429). Bridge berhenti sampai "
                          + until.astimezone(WIB).strftime("%H:%M WIB") + ", tanpa coba ulang.")


async def plan(session, gateway, lean, *, now, retry=False):
    """List stage. Returns ([(key, signature)] needing detail). Spends only list calls."""
    known = {(r.platform, r.shop_id, r.order_id): r for r in (await session.scalars(select(BridgeOrder))).all()}
    seen = lean["seen"]
    wanted = {}
    if not retry:
        cutoff = now - lookback()
        per_page = 50
        for canonical, native in NATIVE.items():
            for page in range(1, max_pages() + 1):
                total, rows = await gateway.recent(native, page, per_page)
                groups = {}
                for row in rows:
                    groups.setdefault(row["key"], []).append(row)
                stop = False
                for key, parts in groups.items():
                    sig = ",".join(sorted(f"{p['status']}:{p['package']}" for p in parts))
                    ordered = min((p["ordered_at"] for p in parts if p["ordered_at"]), default=None)
                    old = seen.get(kstr(key))
                    if old is not None:
                        if old[0] == sig:
                            stop = True
                            seen[kstr(key)] = [sig, now.isoformat()]
                        else:
                            wanted[key] = sig
                    elif key in known:
                        # First sight since the lean scheduler started: the receipt already exists.
                        stop = True
                        seen[kstr(key)] = [sig, now.isoformat()]
                    elif ordered is not None and ordered < cutoff:
                        # Historical order outside the lookback window: never import from history.
                        stop = True
                        seen[kstr(key)] = [sig, now.isoformat()]
                    else:
                        wanted[key] = sig
                if stop or len(rows) < per_page or page * per_page >= total:
                    break
    last_slow = _ts(lean.get("last_slow_at"))
    if retry or last_slow is None or now - last_slow >= slow_every():
        lean["last_slow_at"] = now.isoformat()
        cap = 20 if retry else 10
        due = sorted((r for r in known.values() if r.status == "failed"
                      and (retry or now - _ts(r.updated_at.isoformat()) >= slow_every())),
                     key=lambda r: r.updated_at)[:cap]
        for r in due:
            key = (r.platform, r.shop_id, r.order_id)
            wanted.setdefault(key, (seen.get(kstr(key)) or ["retry"])[0])
    return sorted(wanted.items())


def prune(lean, now):
    keep = now - timedelta(days=SEEN_KEEP_DAYS)
    lean["seen"] = {k: v for k, v in lean["seen"].items()
                    if isinstance(v, list) and len(v) == 2 and (_ts(v[1]) or now) >= keep}


async def send_alerts(lean):
    """Deliver queued alerts through the existing Pipip Telegram bot when configured; always log."""
    alerts, lean["alerts"] = lean.get("alerts") or [], []
    token, chat = os.getenv("EASYBOSS_BRIDGE_ALERT_TELEGRAM_TOKEN"), os.getenv("EASYBOSS_BRIDGE_ALERT_CHAT_ID")
    for text in alerts:
        logger.warning("easyboss-bridge ALERT: %s", text)
        if not (token and chat):
            continue
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                await client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                  json={"chat_id": chat, "text": text, "disable_web_page_preview": True})
        except Exception:
            logger.warning("easyboss-bridge alert delivery failed")  # never log the URL (contains the token)
    return alerts
