"""Documented listing mutations and Shop GMV Max. No local stock writes."""
import calendar
from datetime import date
from fastapi import HTTPException
from . import erp_shopee as provider, erp_shopee_workflows as workflows


def incomplete(path, data):
    raise provider.ShopeeAPIError(path, "unconfirmed_response", "Hasil belum terkonfirmasi. Periksa data Shopee sebelum mencoba ulang.", data.get("request_id"))


async def item(session, akun, item_id):
    response = await workflows.read(session, akun, "/api/v2/product/get_item_base_info", {"item_id_list": str(item_id)})
    row = next((r for r in response.get("item_list", []) if str(r.get("item_id")) == str(item_id)), None)
    if row is None:
        raise HTTPException(404, "Produk tidak tersedia di toko ini")
    return row


async def models(session, akun, item_id):
    response = await workflows.read(session, akun, "/api/v2/product/get_model_list", {"item_id": int(item_id)})
    rows = response.get("model")
    if not isinstance(rows, list):
        incomplete("get_model_list", response)
    return rows


async def update_item(session, akun, item_id, payload):
    current = await item(session, akun, item_id)
    if current.get("has_model") and (payload.weight is not None or payload.dimension is not None) and not payload.apply_to_all_models:
        raise HTTPException(409, "Perubahan berat/dimensi induk menimpa semua varian. Konfirmasikan apply_to_all_models atau edit per varian.")
    body = payload.model_dump(mode="json", exclude_none=True, exclude={"apply_to_all_models"})
    if payload.weight is not None:
        body["weight"] = float(payload.weight)
    if "image_ids" in body:
        body["image"] = {"image_id_list": body.pop("image_ids")}
    if payload.category_id is not None or payload.attribute_list is not None:
        category = payload.category_id or current.get("category_id")
        meta = await workflows.metadata(session, akun, category)
        if not any(c.get("category_id") == category and c.get("has_children") is False for c in meta["categories"]):
            raise HTTPException(422, "Pilih kategori terakhir Shopee")
        workflows.validate_attributes(payload.attribute_list or [], meta["attributes"])
    path = "/api/v2/product/update_item"
    data = await provider.signed_shop_request(session, akun, path, method="POST", body={"item_id": int(item_id), **body})
    if str((data.get("response") or {}).get("item_id")) != str(item_id):
        incomplete(path, data)
    return {"ok": True, "request_id": data.get("request_id"), "warnings": [str(data["warning"])] if data.get("warning") else []}


async def update_variants(session, akun, item_id, payload, tiers=False):
    existing = await models(session, akun, item_id)
    known = {int(r["model_id"]) for r in existing}
    requested = {m.model_id for m in (payload.model_list if tiers else payload.model)}
    if tiers:
        current_indices = {int(r["model_id"]): r.get("tier_index") for r in existing}
        if any(m.tier_index != current_indices[m.model_id] for m in payload.model_list if m.model_id in known):
            raise HTTPException(409, "Perubahan ini hanya untuk nama/standardisasi pilihan, bukan memindahkan identitas varian")
    if not requested.issubset(known) or (tiers and requested != known):
        raise HTTPException(409, "Pemetaan varian berubah. Muat ulang; perubahan pilihan harus menyertakan semua varian existing.")
    body = payload.model_dump(mode="json", exclude_none=True)
    if not tiers:
        for row in body["model"]:
            if "weight" in row:
                row["weight"] = float(row["weight"])
    path = "/api/v2/product/" + ("update_tier_variation" if tiers else "update_model")
    data = await provider.signed_shop_request(session, akun, path, method="POST", body={"item_id": int(item_id), **body})
    # These two APIs document an acknowledgement without response/item_id.
    if data.get("error") != "" or not data.get("request_id"):
        incomplete(path, data)
    return {"ok": True, "request_id": data["request_id"], "warnings": [str(data["warning"])] if data.get("warning") else []}


async def eligibility(session, akun):
    path = "/api/v2/ads/check_create_gms_product_campaign_eligibility"
    data = await provider.signed_shop_request(session, akun, path)
    r = data.get("response")
    if not isinstance(r, dict) or type(r.get("is_eligible")) is not bool:
        incomplete(path, data)
    return {"is_eligible": r["is_eligible"], "reason": r.get("reason"), "request_id": data.get("request_id")}


async def gmv_mutation(session, akun, payload, action):
    if action == "create":
        allowed = await eligibility(session, akun)
        if not allowed["is_eligible"]:
            raise HTTPException(409, f"Toko belum memenuhi syarat GMV Max: {allowed.get('reason') or 'tidak tersedia'}")
    if action == "items" and payload.edit_action == "add":
        response = await workflows.read(session, akun, "/api/v2/product/get_item_base_info", {"item_id_list": ','.join(map(str, payload.item_id_list))})
        if {int(r["item_id"]) for r in response.get("item_list", [])} != set(payload.item_id_list):
            raise HTTPException(422, "Produk tidak ditemukan pada toko kampanye")
    name = {"create": "create_gms_product_campaign", "edit": "edit_gms_product_campaign", "items": "edit_gms_item_product_campaign"}[action]
    body = payload.model_dump(mode="json", exclude_none=True)
    for key in ("start_date", "end_date"):
        if key in body:
            body[key] = getattr(payload, key).strftime("%d-%m-%Y")
    for key in ("daily_budget", "roas_target"):
        if key in body:
            body[key] = float(body[key])
    path = "/api/v2/ads/" + name
    data = await provider.signed_shop_request(session, akun, path, method="POST", body=body)
    ident = (data.get("response") or {}).get("campaign_id")
    if type(ident) is not int or ident <= 0 or (action != "create" and ident != payload.campaign_id):
        incomplete(path, data)
    return {"ok": True, "campaign_id": str(ident), "request_id": data.get("request_id"), "warnings": [str(data["warning"])] if data.get("warning") else []}


def shift_months(value, months):
    absolute = value.year * 12 + value.month - 1 + months
    year, month = divmod(absolute, 12)
    return date(year, month + 1, min(value.day, calendar.monthrange(year, month + 1)[1]))


async def gmv_performance(session, akun, campaign_id, start, end, offset=0, limit=50, per_item=False):
    from ...application.management_schemas import today
    now = today()
    if start > end or end > now or start < shift_months(now, -6) or end > shift_months(start, 3):
        raise HTTPException(422, "Periode maksimal tiga bulan, dalam enam bulan terakhir")
    body = {"start_date": start.strftime("%d-%m-%Y"), "end_date": end.strftime("%d-%m-%Y")}
    if campaign_id is not None:
        body["campaign_id"] = campaign_id
    if per_item:
        body.update(offset=offset, limit=limit)
    path = "/api/v2/ads/" + ("get_gms_item_performance" if per_item else "get_gms_campaign_performance")
    data = await provider.signed_shop_request(session, akun, path, method="POST", body=body)
    response = data.get("response")
    if not isinstance(response, dict) or type(response.get("campaign_id")) is not int or response["campaign_id"] <= 0 or (campaign_id is not None and str(response["campaign_id"]) != str(campaign_id)) or (per_item and (not isinstance(response.get("result_list"), list) or type(response.get("has_next_page")) is not bool or type(response.get("total")) is not int)) or (not per_item and not isinstance(response.get("report"), dict)):
        incomplete(path, data)
    return {**response, "campaign_id": str(response["campaign_id"]), "request_id": data.get("request_id")}
