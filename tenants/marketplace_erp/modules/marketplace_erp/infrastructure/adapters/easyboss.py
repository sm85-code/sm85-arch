"""Read-only adapter boundary. bridge-v1 is OUR gateway contract, not an EasyBoss API.

Native evidence: serah-terima-ai-ops/99-Alat/easyboss-monitor/monitor.py.
No unverified native detail endpoint or item/status field aliases are guessed here.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator


class BridgeProblem(Exception):
    pass


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Shop(Strict):
    platform: str
    shop_id: str = Field(min_length=1, max_length=128)
    marketplace_shop_id: str | None = Field(default=None, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    active: bool


class RejectedOrder(Strict):
    platform: str
    shop_id: str = Field(min_length=1, max_length=128)
    order_id: str = Field(min_length=1, max_length=200)
    error: str = "invalid_source_order"


class Line(Strict):
    unit_price: Decimal | None = Field(default=None, ge=0)
    line_total: Decimal | None = Field(default=None, ge=0)
    image_url: str | None = Field(default=None, max_length=2048)
    item_id: str = Field(min_length=1, max_length=200)
    product: str = Field(min_length=1, max_length=255)
    variant: str = Field(default="", max_length=255)
    product_sku: str | None = Field(default=None, max_length=200)
    variant_sku: str | None = Field(default=None, max_length=200)
    quantity: int = Field(ge=1, le=100000, strict=True)
    note: str = Field(default="", max_length=2000)


class Order(Strict):
    cod: bool = False
    cod_eligible: bool = False
    order_total: Decimal | None = Field(default=None, ge=0)
    buyer_paid: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="", max_length=10)
    recipient_name: str = Field(default="", max_length=200)
    recipient_phone: str = Field(default="", max_length=200)
    recipient_address: str = Field(default="", max_length=2000)
    platform: str
    shop_id: str = Field(min_length=1, max_length=128)
    order_id: str = Field(min_length=1, max_length=200)
    easyboss_order_id: str | None = Field(default=None, max_length=200)
    package_ids: list[str] = Field(default_factory=list, max_length=100)
    # Must be a complete ORDER-level snapshot, with stable line IDs across packages.
    complete: bool
    status: Literal["ready", "unpaid", "cancelled", "shipped", "completed", "hold", "unknown"]
    paid: bool | None
    source_status: str = Field(min_length=1, max_length=64)
    payment_method: str = Field(default="", max_length=120)
    city: str = Field(default="", max_length=200)
    buyer: str = Field(default="", max_length=200)
    ordered_at: datetime | None
    deadline: datetime | None
    buyer_note: str = Field(default="", max_length=2000)
    carrier: str | None = Field(default=None, max_length=64)
    tracking_number: str | None = Field(default=None, max_length=128)
    items: list[Line] = Field(max_length=100)

    @field_validator("ordered_at", "deadline")
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("timezone required")
        return value


def native_time(value: str) -> datetime:
    """Documented monitor contract: EasyBoss naive gmt* strings mean UTC+8."""
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)


def configured() -> bool:
    return bool(os.getenv("EASYBOSS_BRIDGE_TOKEN")
                and (os.getenv("EASYBOSS_BRIDGE_MCP_URL") or os.getenv("EASYBOSS_BRIDGE_URL"))
                and os.getenv("EASYBOSS_BRIDGE_CONTRACT_VERIFIED") == "1")


class Gateway:
    """Select the configured read-only MCP or bridge-v1 transport."""
    def __new__(cls, client):
        if getattr(client, "bridge_mcp", False):
            from .easyboss_mcp import MCPGateway
            return MCPGateway(client)
        return super().__new__(cls)

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def request(self, path: str, params: dict) -> dict:
        for attempt in range(3):
            try:
                r = await self.client.get(path, params=params)
                if r.status_code == 429 or r.status_code >= 500:
                    raise BridgeProblem("upstream_temporary")
                if r.status_code in (401, 403):
                    raise BridgeProblem("upstream_auth")
                if r.status_code != 200:
                    raise BridgeProblem("upstream_http")
                data = r.json()
                if not isinstance(data, dict) or data.get("contract") != "bridge-v1" or data.get("success") is not True:
                    raise BridgeProblem("upstream_contract")
                return data
            except (httpx.TransportError, BridgeProblem) as exc:
                code = str(exc) if isinstance(exc, BridgeProblem) else "upstream_timeout"
                if code not in ("upstream_timeout", "upstream_temporary") or attempt == 2:
                    raise BridgeProblem(code) from None
                await asyncio.sleep(2 ** attempt)
            except (ValueError, TypeError):
                raise BridgeProblem("upstream_contract") from None
        raise BridgeProblem("upstream_temporary")

    async def pages(self, path: str, model, params=None):
        cursor, seen, rows = None, set(), []
        for _ in range(1000):
            data = await self.request(path, {**(params or {}), **({"cursor": cursor} if cursor else {})})
            try:
                if not isinstance(data["items"], list) or len(data["items"]) > 200:
                    raise ValueError()
                for item in data["items"]:
                    try:
                        rows.append(model.model_validate(item))
                    except ValueError:
                        if model is not Order or not isinstance(item, dict):
                            raise
                        rows.append(RejectedOrder.model_validate({k: item.get(k) for k in ("platform", "shop_id", "order_id")}))
                cursor = data["next_cursor"]
                if cursor is None:
                    return rows
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise ValueError()
                seen.add(cursor)
            except (KeyError, ValueError, TypeError):
                raise BridgeProblem("upstream_contract") from None
        raise BridgeProblem("pagination_limit")

    async def shops(self):
        return await self.pages("shops", Shop)

    async def orders(self, since: datetime | None):
        # Gateway must include changed/cancelled orders and all previous failures on a full scan.
        return await self.pages("orders", Order, {"since": since.isoformat()} if since else {})


def client():
    if not configured():
        raise BridgeProblem("easyboss_contract_unverified")
    mcp_url = os.getenv("EASYBOSS_BRIDGE_MCP_URL")
    if mcp_url:
        from urllib.parse import urlsplit
        parsed = urlsplit(mcp_url)
        # HTTP only for the known Docker service; never forward credentials externally.
        if parsed.username or parsed.password or parsed.query or parsed.fragment or not (
            parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname == "easyboss-mcp")):
            raise BridgeProblem("invalid_mcp_url")
        result = httpx.AsyncClient(base_url=mcp_url, timeout=60, follow_redirects=False,
            headers={"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2024-11-05",
                     "Authorization": "Bearer " + os.environ["EASYBOSS_BRIDGE_TOKEN"]})
        result.bridge_mcp = True
        return result
    url = os.environ["EASYBOSS_BRIDGE_URL"].rstrip("/") + "/"
    if not url.startswith("https://"):
        raise BridgeProblem("https_required")
    return httpx.AsyncClient(base_url=url, headers={"Authorization": "Bearer " + os.environ["EASYBOSS_BRIDGE_TOKEN"]},
                             timeout=30, follow_redirects=False)


class NativeReadClient:
    """Verified-from-source native READ endpoints, using an externally provisioned session.

    Shop counts are NOT an authoritative shop directory. Package lists are NOT complete orders.
    Neither method can create ERP orders or accept/ship EasyBoss orders.
    """
    def __init__(self, session: httpx.AsyncClient):
        self.session = session

    async def _form(self, endpoint: str, fields: dict):
        try:
            response = await self.session.post("https://www.easyboss.com/api/order/package/" + endpoint, data=fields)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get("result") != "success":
                raise BridgeProblem("native_business_error")
            return data
        except (httpx.HTTPError, ValueError):
            raise BridgeProblem("native_read_failed") from None

    async def shop_counts(self, tab: str):
        data = await self._form("getShopAndOrderCountMap", {"appPackageTab": tab, "platform": "", "isShopGroup": "0"})
        if not isinstance(data.get("shopAndCountMap"), list):
            raise BridgeProblem("native_contract")
        return data["shopAndCountMap"]

    async def packages(self, tab: str):
        rows, seen = [], set()
        for page in range(1, 1001):
            data = await self._form("searchOrderPackageList", {"pageSize": "100", "page": str(page),
                "appPackageTab": tab, "sortField": "gmtOrderStart", "sortType": "asc"})
            batch, total = data.get("packageList"), data.get("total")
            if not isinstance(batch, list) or isinstance(total, bool):
                raise BridgeProblem("native_contract")
            try:
                total = int(total)
            except (ValueError, TypeError):
                raise BridgeProblem("native_contract") from None
            if total < 0 or len(batch) > 100 or (not batch and len(rows) < total):
                raise BridgeProblem("native_incomplete_page")
            for package in batch:
                key = str(package.get("opOrderPackageId") or "") if isinstance(package, dict) else ""
                if not key or key in seen:
                    raise BridgeProblem("native_unstable_pagination")
                seen.add(key)
            rows.extend(batch)
            if len(rows) >= total:
                if len(rows) != total:
                    raise BridgeProblem("native_unstable_pagination")
                return rows
        raise BridgeProblem("pagination_limit")
