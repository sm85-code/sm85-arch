"""Read-only insights. Preserve missing values and provider periods."""

from datetime import datetime, timezone
import math
from . import erp_shopee as provider


def number(value):
    return value is None or (type(value) in (int, float) and math.isfinite(value))


async def fetch(session, akun, path, field, params=None):
    data = await provider.signed_shop_request(session, akun, path, params=params)
    response = data.get("response")
    if not isinstance(response, dict) or not isinstance(response.get(field), list):
        raise provider.ShopeeAPIError(
            path, "incomplete_response", "Statistik Shopee belum lengkap.", data.get("request_id")
        )
    return data, response


async def shop_performance(session, akun):
    data, response = await fetch(session, akun, "/api/v2/account_health/get_shop_performance", "metric_list")
    rows = response["metric_list"]
    if any(
        not isinstance(r, dict)
        or not isinstance(r.get("metric_name"), str)
        or type(r.get("metric_id")) is not int
        or not number(r.get("current_period"))
        or not number(r.get("last_period"))
        or type(r.get("unit")) is not int
        or (
            r.get("target") is not None
            and (
                not isinstance(r["target"], dict)
                or not isinstance(r["target"].get("comparator"), str)
                or not number(r["target"].get("value"))
            )
        )
        for r in rows
    ):
        raise provider.ShopeeAPIError(
            "get_shop_performance", "incomplete_response", "Indikator performa tidak lengkap.", data.get("request_id")
        )
    return {
        "metrics": rows,
        "overall_performance": response.get("overall_performance"),
        "request_id": data.get("request_id"),
        "diambil_at": datetime.now(timezone.utc).isoformat(),
    }


async def product_stats(session, akun, item_id):
    data, response = await fetch(
        session, akun, "/api/v2/product/get_item_extra_info", "item_list", {"item_id_list": str(item_id)}
    )
    row = next(
        (r for r in response["item_list"] if isinstance(r, dict) and str(r.get("item_id")) == str(item_id)), None
    )
    if row is None or any(not number(row.get(k)) for k in ("sale", "views", "likes", "rating_star", "comment_count")):
        raise provider.ShopeeAPIError(
            "get_item_extra_info",
            "incomplete_response",
            "Statistik produk yang diminta belum tersedia.",
            data.get("request_id"),
        )
    return {
        "item_id": str(item_id),
        **{k: row.get(k) for k in ("sale", "views", "likes", "rating_star", "comment_count")},
        "periode_views_hari": 30,
        "penjualan_kumulatif": True,
        "request_id": data.get("request_id"),
        "diambil_at": datetime.now(timezone.utc).isoformat(),
    }
