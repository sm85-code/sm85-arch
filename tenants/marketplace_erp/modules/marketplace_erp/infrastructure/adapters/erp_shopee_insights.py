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


async def diagnosis(session, akun, item_ids):
    path = "/api/v2/product/get_item_content_diagnosis_result"
    data = await provider.signed_shop_request(session, akun, path, method="POST", body={"item_id_list": item_ids})
    response = data.get("response")
    if not isinstance(response, dict) or not isinstance(response.get("success_item_list", []), list) or not isinstance(response.get("failure_item_list", []), list):
        raise provider.ShopeeAPIError(path, "incomplete_response", "Diagnosis Shopee belum lengkap.", data.get("request_id"))
    successes, failures = response.get("success_item_list", []), response.get("failure_item_list", [])
    seen = set()
    for r in successes + failures:
        if not isinstance(r, dict) or type(r.get("item_id")) is not int or r["item_id"] not in item_ids or r["item_id"] in seen:
            raise provider.ShopeeAPIError(path, "incomplete_response", "Identitas diagnosis tidak sesuai.", data.get("request_id"))
        seen.add(r["item_id"])
    for r in successes:
        r.setdefault("unfinished_task", [])
        if type(r.get("quality_level")) is not int or r.get("quality_level") not in (0, 1, 2, 3) or not isinstance(r.get("unfinished_task"), list) or any(not isinstance(t, dict) or type(t.get("issue_type")) is not int or not isinstance(t.get("suggestion"), str) for t in r["unfinished_task"]):
            raise provider.ShopeeAPIError(path, "incomplete_response", "Tingkat kualitas produk belum lengkap.", data.get("request_id"))
    if seen != set(item_ids):
        raise provider.ShopeeAPIError(path, "incomplete_response", "Sebagian produk tidak mempunyai hasil diagnosis.", data.get("request_id"))
    return {"success_item_list": [{**r, "item_id": str(r["item_id"])} for r in successes], "failure_item_list": [{**r, "item_id": str(r["item_id"])} for r in failures], "request_id": data.get("request_id"), "diambil_at": datetime.now(timezone.utc).isoformat()}


async def penalties(session, akun, page=1, size=25):
    data, response = await fetch(session, akun, "/api/v2/account_health/get_penalty_point_history", "penalty_point_list", {"page_no": page, "page_size": size})
    total = response.get("total_count")
    if type(total) is not int or total < 0:
        raise provider.ShopeeAPIError("get_penalty_point_history", "incomplete_response", "Jumlah riwayat penalti belum tersedia.", data.get("request_id"))
    rows = response["penalty_point_list"]
    if any(not isinstance(r, dict) or type(r.get("issue_time")) is not int or not number(r.get("latest_point_num")) or not number(r.get("original_point_num")) for r in rows):
        raise provider.ShopeeAPIError("get_penalty_point_history", "incomplete_response", "Riwayat penalti tidak lengkap.", data.get("request_id"))
    return {"items": [{**r, "reference_id": str(r["reference_id"]) if r.get("reference_id") is not None else None} for r in rows], "total": total, "halaman": page, "ada_lagi": page * size < total, "request_id": data.get("request_id")}
