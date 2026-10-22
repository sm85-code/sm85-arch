"""Local-seller commerce operations, using the copied official endpoint schemas."""
from decimal import Decimal, InvalidOperation
import logging
import re
import time
from uuid import uuid4
from datetime import datetime, timezone
from fastapi import HTTPException

from . import erp_shopee as provider, erp_shopee_management as management, erp_shopee_workflows as workflows


_log = logging.getLogger(__name__)


async def acknowledged(session, akun, path, body):
    data = await provider.signed_shop_request(session, akun, path, method="POST", body=body)
    if data.get("error") != "" or not data.get("request_id"):
        management.incomplete(path, data)
    return {"ok": True, "request_id": data["request_id"], "warnings": [str(data["warning"])] if data.get("warning") else []}


async def delete_product(session, akun, item_id):
    await management.item(session, akun, item_id)
    return await acknowledged(session, akun, "/api/v2/product/delete_item", {"item_id": int(item_id)})


async def delete_model(session, akun, item_id, model_id):
    models = await management.models(session, akun, item_id)
    if not any(str(m.get("model_id")) == str(model_id) for m in models):
        raise HTTPException(409, "Varian tidak ditemukan pada produk ini. Muat ulang produk.")
    if len(models) <= 1:
        raise HTTPException(409, "Varian terakhir tidak dapat dihapus. Hapus produk jika diperlukan.")
    return await acknowledged(session, akun, "/api/v2/product/delete_model", {"item_id": int(item_id), "model_id": model_id})


def confirmed_models(rows, payload):
    if not isinstance(rows, list) or len(rows) != len(payload.models):
        return False
    if any(not isinstance(r, dict) or not isinstance(r.get("tier_index"), list) or not str(r.get("model_id", "")).isdigit() or int(r["model_id"]) <= 0 for r in rows):
        return False
    return len({str(r["model_id"]) for r in rows}) == len(rows) and {tuple(r["tier_index"]) for r in rows} == {tuple(m.tier_index) for m in payload.models}


async def add_models(session, akun, item_id, payload):
    current = await workflows.read(session, akun, "/api/v2/product/get_model_list", {"item_id": int(item_id)})
    tiers, models = current.get("tier_variation"), current.get("model")
    if not isinstance(tiers, list) or not tiers or not isinstance(models, list):
        raise HTTPException(409, "Produk belum mempunyai pilihan varian. Atur pilihan varian terlebih dahulu.")
    if any(not isinstance(t, dict) or not isinstance(t.get("option_list"), list) for t in tiers) or any(not isinstance(m, dict) or not isinstance(m.get("tier_index"), list) for m in models):
        management.incomplete("get_model_list", current)
    known = {tuple(m["tier_index"]) for m in models}
    for model in payload.models:
        if len(model.tier_index) != len(tiers) or any(i >= len(t.get("option_list", [])) for i, t in zip(model.tier_index, tiers)):
            raise HTTPException(422, "Pilih kombinasi varian yang tersedia pada produk")
        if tuple(model.tier_index) in known:
            raise HTTPException(409, "Kombinasi varian sudah ada. Edit varian tersebut.")
    rows = []
    for model in payload.models:
        row = {"tier_index": model.tier_index, "model_sku": model.sku, "original_price": float(model.price), "seller_stock": [{"stock": model.stock}]}
        for key in ("weight", "dimension", "pre_order", "gtin_code"):
            value = getattr(model, key)
            if value is not None:
                row[key] = float(value) if key == "weight" else value if key == "gtin_code" else value.model_dump(mode="json")
        rows.append(row)
    path = "/api/v2/product/add_model"
    data = await provider.signed_shop_request(session, akun, path, method="POST", body={"item_id": int(item_id), "model_list": rows})
    returned = (data.get("response") or {}).get("model")
    if not confirmed_models(returned, payload):
        management.incomplete(path, data)
    return {"ok": True, "request_id": data.get("request_id"), "warnings": [str(data["warning"])] if data.get("warning") else []}


async def init_models(session, akun, item_id, payload):
    current = await management.item(session, akun, item_id)
    if current.get("has_model") is not False:
        raise HTTPException(409, "Produk sudah mempunyai varian. Muat ulang dan edit varian existing.")
    path = "/api/v2/product/init_tier_variation"
    data = await provider.signed_shop_request(session, akun, path, method="POST", body=workflows.variant_body(payload, int(item_id)))
    rows = (data.get("response") or {}).get("model")
    if not confirmed_models(rows, payload):
        management.incomplete(path, data)
    return {"ok": True, "request_id": data.get("request_id"), "warnings": [str(data["warning"])] if data.get("warning") else []}


async def product_details(session, akun, item_id, kind):
    path, field = {
        "promosi": ("/api/v2/product/get_item_promotion", "success_list"),
        "pelanggaran": ("/api/v2/product/get_item_violation_info", "item_list"),
    }[kind]
    result = await workflows.read(session, akun, path, {"item_id_list": str(item_id)})
    rows = result.get(field)
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        management.incomplete(path, result)
    row = next((r for r in rows if str(r.get("item_id")) == str(item_id)), None)
    if kind == "promosi" and row is not None and row.get("promotion") is None:
        row = {**row, "promotion": []}  # Shopee omits the key for an item with no promotion
    if row is None or row.get("fail_error") or (kind == "promosi" and not isinstance(row.get("promotion"), list)):
        management.incomplete(path, {"request_id": None})
    if kind == "promosi":
        if any(not isinstance(p, dict) or not isinstance(p.get("promotion_price_info", []), list) or any(not isinstance(v, dict) for v in p.get("promotion_price_info", [])) for p in row["promotion"]):
            management.incomplete(path, result)
    elif any(not isinstance(row.get(key, []), list) or any(not isinstance(v, dict) for v in row.get(key, [])) for key in ("item_status_details", "deboost_details")):
        management.incomplete(path, result)
    return row


async def income(session, akun, start, end, status, cursor, size, *, tolerate_unlisted=False):
    from ...application.management_schemas import today
    if start > end or end > today():
        raise HTTPException(422, "Periode pendapatan tidak valid")
    if status == 1 and (start >= end or (end - start).days >= 14):
        raise HTTPException(422, "Pendapatan dirilis memerlukan periode lebih dari satu hari dan maksimal 14 hari")
    path = "/api/v2/payment/get_income_detail"
    started = time.monotonic()
    try:
        data = await provider.signed_shop_request(session, akun, path, params={"date_from": start.isoformat(), "date_to": end.isoformat(), "income_status": status, "cursor": cursor, "page_size": size}, timeout=30.0)
    except HTTPException as exc:
        if isinstance(exc, provider.ShopeeNotConfigured) or exc.status_code not in (502, 503, 504):
            raise
        # Keep a provider failure distinguishable from an HTML gateway timeout.
        # Fixed metadata only: no signed URL, credentials, cursor or income rows.
        report = {"diagnostic_id": str(uuid4()), "time": datetime.now(timezone.utc).isoformat(), "endpoint": path,
                  "account_id": getattr(akun, "id", None), "income_status": status,
                  "upstream_status": exc.status_code, "elapsed_ms": round((time.monotonic() - started) * 1000),
                  "error": exc.error if isinstance(exc, provider.ShopeeAPIError) else "income_provider_unavailable",
                  "request_id": exc.request_id if isinstance(exc, provider.ShopeeAPIError) else None,
                  "message": exc.message if isinstance(exc, provider.ShopeeAPIError) else "Koneksi pendapatan Shopee tidak selesai tepat waktu atau tidak tersedia.",
                  "failures": []}
        _log.warning("shopee_income phase=request_failed diagnostic=%s", report)
        raise HTTPException(424, detail={
            "message": (f"Pendapatan ditolak Shopee: {exc.message}" if isinstance(exc, provider.ShopeeAPIError) else "Pendapatan Shopee belum merespons melalui koneksi/proxy toko. Mutasi Saldo memakai endpoint berbeda. Coba lagi; jika berulang, salin rincian untuk pemeriksaan."),
            "diagnostic": report,
        }) from exc
    # The reference sample is top-level; accept the same contract inside the
    # usual Shopee response wrapper as well. Never turn missing data into [].
    wrapped = data.get("response")
    envelope = data.get("income_detail_list")
    if envelope is None and isinstance(wrapped, dict):
        envelope = wrapped.get("income_detail_list")
        if envelope is None and ("list" in wrapped or "income_detail_list_item" in wrapped):
            envelope = wrapped

    # Some local shops return an empty successful response when nothing is pending.
    # Confirm with the independent overview before normalizing it to an empty page.
    # This adapter is shared by Menunggu cair and the weekly financial report.
    overview_pending = None
    if status == 2 and not cursor and wrapped == {} and envelope is None and not data.get("error"):
        try:
            pending = (await income_overview(session, akun)).get("pending_amount")
        except HTTPException:
            pending = None
        overview_pending = pending
        if pending is not None and Decimal(str(pending)) == 0:
            return {"items": [], "next_cursor": "", "ada_lagi": False, "pagination_known": True,
                    "warnings": [], "request_id": data.get("request_id")}
        if tolerate_unlisted and pending is not None and Decimal(str(pending)) > 0:
            # The overview can lag the order list for a few minutes after an order is released (the wallet already shows the
            # credit). In the menu that is a note, not an error; the financial report keeps the strict behavior because it must account for every rupiah.
            amount = f"{int(Decimal(str(pending))):,}".replace(",", ".")
            return {"items": [], "next_cursor": "", "ada_lagi": False, "pagination_known": True,
                    "warnings": [f"Ringkasan Shopee masih mencatat dana menunggu cair sekitar Rp {amount}, tetapi daftar rinciannya kosong. Biasanya karena pesanan baru saja cair; muat ulang beberapa menit lagi."],
                    "request_id": data.get("request_id")}

    def reject(reason):
        # Report fixed paths and schema-like wrapper field names only. Never
        # include field values, financial records, cursors or credentials.
        fields = ("income_detail_list", "response", "error")
        top_types = {key: type(data.get(key)).__name__ for key in fields}
        envelope_types = {key: type(envelope.get(key)).__name__ for key in ("list", "income_detail_list_item", "next_page")} if isinstance(envelope, dict) else {}
        wrapped_type = {key: type(wrapped.get(key)).__name__ for key in ("income_detail_list", "list", "income_detail_list_item", "next_page")} if isinstance(wrapped, dict) else "missing"
        def schema_fields(value):
            if not isinstance(value, dict):
                return {}
            return {key: type(item).__name__ for key, item in list(value.items())[:50]
                    if isinstance(key, str) and re.fullmatch(r"[a-z]+(?:_[a-z]+)*", key)
                    and not any(secret in key for secret in ("token", "secret", "password", "cookie", "authorization", "sign"))}
        response_fields = schema_fields(wrapped)
        _log.warning("shopee_income phase=contract_failed request_id=%s reason=%s top_types=%s envelope_types=%s wrapped_income_type=%s", data.get("request_id"), reason, top_types, envelope_types, wrapped_type)
        report = {"diagnostic_id": str(uuid4()), "time": datetime.now(timezone.utc).isoformat(),
                  "endpoint": path, "account_id": getattr(akun, "id", None), "income_status": status,
                  "error": "income_response_incomplete", "reason": reason,
                  "elapsed_ms": round((time.monotonic() - started) * 1000),
                  "top_types": top_types, "envelope_types": envelope_types,
                  "response_types": wrapped_type, "response_fields": response_fields,
                  "response_field_count": len(wrapped) if isinstance(wrapped, dict) else None,
                  "nested_data_fields": schema_fields(wrapped.get("data")) if isinstance(wrapped, dict) else {},
                  "request_id": None, "failures": []}
        failure = provider.ShopeeAPIError(path, "incomplete_response", "Respons pendapatan Shopee tidak lengkap.")
        failure.status_code = 424
        message = "Respons pendapatan Shopee berhasil diterima, tetapi format datanya belum lengkap. Salin rincian untuk pemeriksaan."
        try:
            if overview_pending is not None and Decimal(str(overview_pending)) > 0:
                # The empty list is not "nothing pending": Shopee's own overview says money is waiting.
                amount = f"{int(Decimal(str(overview_pending))):,}".replace(",", ".")
                message = (f"Shopee mencatat dana menunggu cair sekitar Rp {amount} untuk toko ini, tetapi rincian pesanannya "
                           "belum dikirim Shopee (daftar kosong). Coba lagi nanti; toko lain tetap ditampilkan.")
        except (InvalidOperation, ValueError):
            pass
        failure.detail = {"message": message, "diagnostic": report}
        raise failure

    # An explicit empty list is a successful zero-row response, not a missing
    # envelope. Pagination remains unknown when Shopee omits its metadata.
    if isinstance(envelope, list) and not envelope:
        envelope = {"list": []}
    if not isinstance(envelope, dict):
        reject("income_envelope")
    # Some gateways put pagination beside income_detail_list, not inside it.
    # Use only an explicitly returned cursor; never guess one from an order id.
    if "next_page" in envelope:
        page = envelope["next_page"]
    elif isinstance(wrapped, dict) and "next_page" in wrapped:
        page = wrapped["next_page"]
    else:
        page = data.get("next_page")
    if page is not None and not isinstance(page, dict):
        reject("pagination_envelope")
    # The endpoint's real IDR sample uses `list`; its field table calls this
    # `income_detail_list_item`. Accept both documented forms without guessing values.
    rows = envelope.get("list", envelope.get("income_detail_list_item"))
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        reject("income_rows")
    for row in rows:
        for key in ("estimated_escrow_amount", "released_amount", "to_release_amount"):
            value = row.get(key)
            if value is not None:
                try:
                    if isinstance(value, bool) or not Decimal(str(value)).is_finite():
                        raise InvalidOperation
                except (InvalidOperation, ValueError):
                    reject("monetary_value")
    known = isinstance(page, dict) and page.get("cursor") is not None
    following = page["cursor"] if known else ""
    if not isinstance(following, str) or (following and following == cursor):
        reject("pagination_cursor")
    warnings = []
    if not known:
        warnings.append("Shopee tidak menyertakan informasi halaman berikutnya. Kelengkapan daftar belum dapat dipastikan.")
        _log.warning("shopee_income phase=pagination_unavailable request_id=%s row_count=%d", data.get("request_id"), len(rows))
    return {"items": rows, "next_cursor": following, "ada_lagi": bool(following) if known else None, "pagination_known": known, "warnings": warnings, "request_id": data.get("request_id")}


async def income_overview(session, akun):
    path = "/api/v2/payment/get_income_overview"
    # Omitting income_status requests all current local-shop income components.
    data = await provider.signed_shop_request(session, akun, path)
    response = data.get("response")
    totals = data.get("total_income")
    if totals is None and isinstance(response, dict):
        totals = response.get("total_income")
    if not isinstance(totals, dict) or not any(k in totals for k in ("pending_amount", "released_amount")):
        management.incomplete(path, data)
    for key in ("pending_amount", "released_amount"):
        value = totals.get(key)
        if value is not None:
            try:
                if isinstance(value, bool) or not Decimal(str(value)).is_finite():
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                management.incomplete(path, data)
    return {"pending_amount": totals.get("pending_amount"), "released_amount": totals.get("released_amount"), "request_id": data.get("request_id")}


async def order_income(session, akun, order_sn):
    response = await workflows.read(session, akun, "/api/v2/payment/get_escrow_detail", {"order_sn": order_sn})
    if str(response.get("order_sn")) != order_sn or not isinstance(response.get("order_income"), dict):
        management.incomplete("get_escrow_detail", response)
    return response


async def tracking(session, akun, order_sn, package_number=None):
    params = {"order_sn": order_sn}
    if package_number:
        params["package_number"] = package_number
    response = await workflows.read(session, akun, "/api/v2/logistics/get_tracking_info", params)
    if response.get("order_sn") != order_sn or not isinstance(response.get("tracking_info"), list) or any(not isinstance(r, dict) or type(r.get("update_time")) is not int or not isinstance(r.get("description"), str) for r in response.get("tracking_info", [])):
        management.incomplete("get_tracking_info", response)
    return response


async def settings_read(session, akun, section):
    path, field = {
        "profil": ("/api/v2/shop/get_profile", "shop_name"),
        "libur": ("/api/v2/shop/get_shop_holiday_mode", "holiday_mode_on"),
        "jasa-kirim": ("/api/v2/logistics/get_channel_list", "logistics_channel_list"),
        "alamat": ("/api/v2/logistics/get_address_list", "address_list"),
    }[section]
    response = await workflows.read(session, akun, path)
    valid = (
        isinstance(response.get(field), str) if section == "profil"
        else type(response.get(field)) is bool if section == "libur"
        else isinstance(response.get(field), list)
    )
    if section == "jasa-kirim" and valid:
        valid = all(isinstance(c, dict) and type(c.get("logistics_channel_id")) is int and isinstance(c.get("logistics_channel_name"), str) and type(c.get("enabled")) is bool and type(c.get("cod_enabled")) is bool for c in response[field])
    if section == "alamat" and valid:
        valid = all(isinstance(a, dict) and type(a.get("address_id")) is int and isinstance(a.get("address_type", []), list) and all(isinstance(t, str) for t in a.get("address_type", [])) for a in response[field])
    if not valid:
        management.incomplete(path, response)
    return response


async def update_channel(session, akun, channel_id, payload):
    current = await settings_read(session, akun, "jasa-kirim")
    channels = current.get("logistics_channel_list", [])
    channel = next((c for c in channels if c.get("logistics_channel_id") == channel_id), None)
    if channel is None:
        raise HTTPException(422, "Jasa kirim tidak tersedia pada toko ini")
    if payload.enabled is False and channel.get("force_enable"):
        raise HTTPException(409, "Jasa kirim ini diwajibkan oleh Shopee")
    driver = payload.auto_call_driver_setting
    if driver and driver.auto_call_driver_enabled:
        setting = channel.get("auto_call_driver_setting") or {}
        limits = setting.get("preparation_time_limit") or {}
        if not setting.get("auto_call_driver_eligible") or not limits.get("min_preparation_time", 0) <= driver.preparation_time <= limits.get("max_preparation_time", -1):
            raise HTTPException(422, "Penjemputan otomatis/waktu persiapan tidak tersedia untuk jasa kirim ini")
    body = {"logistics_channel_id": channel_id, **payload.model_dump(mode="json", exclude_none=True)}
    return await acknowledged(session, akun, "/api/v2/logistics/update_channel", body)


async def hourly_ads(session, akun, day, campaign_id=None):
    from ...application.management_schemas import today
    if day > today():
        raise HTTPException(422, "Tanggal performa tidak boleh di masa depan")
    params = {"performance_date": day.strftime("%d-%m-%Y")}
    path = "/api/v2/ads/get_all_cpc_ads_hourly_performance"
    if campaign_id is not None:
        path = "/api/v2/ads/get_product_campaign_hourly_performance"
        params["campaign_id_list"] = str(campaign_id)
    data = await provider.signed_shop_request(session, akun, path, params=params)
    response = data.get("response")
    if not isinstance(response, list) or any(not isinstance(r, dict) for r in response):
        management.incomplete(path, data)
    rows = response
    if campaign_id is not None:
        if any(not isinstance(r.get("campaign_list"), list) for r in response):
            management.incomplete(path, data)
        campaigns = [c for r in response for c in r.get("campaign_list", [])]
        if any(not isinstance(c, dict) or str(c.get("campaign_id")) != str(campaign_id) or not isinstance(c.get("metrics_list"), list) for c in campaigns):
            management.incomplete(path, data)
        rows = [m for c in campaigns for m in c.get("metrics_list", [])]
    if any(not isinstance(r, dict) or type(r.get("hour")) is not int or not 0 <= r["hour"] <= 23 for r in rows):
        management.incomplete(path, data)
    return {"items": sorted(rows, key=lambda r: r["hour"]), "request_id": data.get("request_id"), "campaign_id": str(campaign_id) if campaign_id else None}


async def address_config(session, akun, payload):
    current = await settings_read(session, akun, "alamat")
    if not any(a.get("address_id") == payload.address_id for a in current["address_list"]):
        raise HTTPException(422, "Alamat harus berasal dari toko yang dipilih")
    body = {"address_type_config": {"address_id": payload.address_id, "address_type": payload.address_type}}
    if payload.show_pickup_address is not None:
        body["show_pickup_address"] = payload.show_pickup_address
    return await acknowledged(session, akun, "/api/v2/logistics/set_address_config", body)


async def income_orders(session, akun, numbers):
    """Read product/variant and pickup snapshots in one shop-scoped batch, without importing orders."""
    response = await workflows.read(session, akun, "/api/v2/order/get_order_detail", {
        "order_sn_list": ",".join(numbers),
        "response_optional_fields": "item_list,package_list,pickup_done_time",
    })
    rows = response.get("order_list")
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        management.incomplete("get_order_detail", response)
    wanted = set(numbers)
    result = []
    for row in rows:
        if row.get("order_sn") not in wanted:
            continue
        items, packages = row.get("item_list"), row.get("package_list", [])
        if not isinstance(items, list) or any(not isinstance(i, dict) for i in items) or not isinstance(packages, list) or any(not isinstance(i, dict) for i in packages):
            management.incomplete("get_order_detail", response)
        result.append({
            "order_sn": row["order_sn"], "order_status": row.get("order_status"),
            "pickup_done_time": row.get("pickup_done_time"),
            "items": [{"name": i.get("item_name"), "variant": i.get("model_name")} for i in items],
            "packages": [{"package_number": p.get("package_number"), "logistics_status": p.get("logistics_status")} for p in packages],
        })
    missing = len(wanted - {r["order_sn"] for r in result})
    return {"items": result, "warnings": [f"Rincian {missing} pesanan tidak tersedia dari Shopee."] if missing else [], "request_id": response.get("request_id")}
