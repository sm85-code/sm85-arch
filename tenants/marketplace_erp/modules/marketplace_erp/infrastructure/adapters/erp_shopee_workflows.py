"""Strict Shopee projections and publication payloads. No local inventory mutation."""

from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from fastapi import HTTPException

from . import erp_shopee as shopee
from .erp_shopee_returns import rentang_retur


def reject(message):
    raise HTTPException(status_code=422, detail=message)


async def read(session, akun, path, params=None):
    data = await shopee.signed_shop_request(session, akun, path, params=params)
    response = data.get("response")
    if not isinstance(response, dict):
        raise shopee.ShopeeAPIError(
            path, "incomplete_response", "Respons Shopee tidak lengkap.", data.get("request_id")
        )
    return response


async def metadata(session, akun, category_id=None):
    categories = await read(session, akun, "/api/v2/product/get_category", {"language": "id"})
    channels = await read(session, akun, "/api/v2/logistics/get_channel_list")
    if not isinstance(categories.get("category_list"), list) or not isinstance(
        channels.get("logistics_channel_list"), list
    ):
        raise shopee.ShopeeAPIError("metadata", "incomplete_response", "Kategori/jasa kirim belum lengkap.", None)
    result = {
        "categories": categories["category_list"],
        "channels": channels["logistics_channel_list"],
        "attributes": [],
        "limits": {},
    }
    if category_id:
        limits = await read(session, akun, "/api/v2/product/get_item_limit", {"category_id": category_id})
        attrs = await read(
            session, akun, "/api/v2/product/get_attribute_tree", {"category_id_list": [category_id], "language": "id"}
        )
        entry = next((r for r in attrs.get("list", []) if r.get("category_id") == category_id), None)
        if entry is None or not isinstance(entry.get("attribute_tree"), list):
            raise shopee.ShopeeAPIError("metadata", "incomplete_response", "Atribut kategori belum lengkap.", None)
        result.update(limits=limits, attributes=entry["attribute_tree"])
    return result


def validate_publication(payload, meta):
    if not any(
        c.get("category_id") == payload.category_id and c.get("has_children") is False for c in meta["categories"]
    ):
        reject("Pilih kategori terakhir yang tersedia di toko tujuan.")
    channels = {c["logistics_channel_id"]: c for c in meta["channels"]}
    if len({logistic.logistic_id for logistic in payload.logistic_info}) != len(payload.logistic_info):
        reject("Jasa kirim tidak boleh duplikat.")
    for logistic in payload.logistic_info:
        channel = channels.get(logistic.logistic_id)
        if not channel or (logistic.enabled and channel.get("enabled") is not True):
            reject("Jasa kirim belum aktif/tersedia pada toko tujuan.")
        if logistic.enabled and channel.get("fee_type") == "CUSTOM_PRICE" and logistic.shipping_fee is None:
            reject("Isi ongkos untuk jasa kirim dengan biaya khusus.")
        if logistic.enabled and channel.get("fee_type") == "SIZE_SELECTION":
            sizes = {str(size["size_id"]) for size in channel.get("size_list", [])}
            if logistic.size_id is None or str(logistic.size_id) not in sizes:
                reject("Pilih ukuran jasa kirim yang tersedia di toko tujuan.")
    if any(
        c.get("force_enable")
        and c["logistics_channel_id"]
        not in {logistic.logistic_id for logistic in payload.logistic_info if logistic.enabled}
        for c in meta["channels"]
    ):
        reject("Jasa kirim wajib dari Shopee harus diaktifkan.")
    selected = {a.attribute_id: a for a in payload.attribute_list}
    allowed = set()

    def walk(nodes):
        for node in nodes:
            ident = node["attribute_id"]
            allowed.add(ident)
            chosen = selected.get(ident)
            if node.get("mandatory") and not chosen:
                reject(f"Atribut {node.get('name', ident)} wajib diisi.")
            if not chosen:
                continue
            info = node.get("attribute_info") or {}
            values = {v["value_id"]: v for v in node.get("attribute_value_list", [])}
            max_count = info.get("max_value_count") or (50 if info.get("input_type") in (4, 5) else 1)
            if len(chosen.attribute_value_list) > max_count:
                reject(f"Terlalu banyak pilihan atribut {node.get('name', ident)}.")
            if len({(v.value_id, v.original_value_name) for v in chosen.attribute_value_list}) != len(
                chosen.attribute_value_list
            ):
                reject("Pilihan atribut tidak boleh duplikat.")
            for val in chosen.attribute_value_list:
                custom = val.value_id == 0 and info.get("input_type") in (2, 3, 5)
                ref = None if custom else values.get(val.value_id)
                if ref is None and (val.value_id != 0 or info.get("input_type") not in (2, 3, 5)):
                    reject("Pilihan atribut tidak tersedia pada kategori tujuan.")
                if ref and val.original_value_name != ref.get("name"):
                    reject("Nama pilihan atribut tidak sesuai metadata tujuan.")
                units = info.get("attribute_unit_list") or []
                if units and val.value_unit not in units:
                    reject("Satuan atribut wajib sesuai pilihan yang tersedia.")
                if ref:
                    walk(ref.get("child_attribute_list") or [])

    walk(meta["attributes"])
    if set(selected) - allowed:
        reject("Atribut tidak sesuai kategori atau pilihan induknya.")
    limits = meta["limits"]
    chart = limits.get("size_chart_limit") or {}
    if chart.get("size_chart_mandatory") and not payload.size_chart and not payload.size_chart_id:
        reject("Kategori ini mewajibkan panduan ukuran. Unggah foto panduan ukuran atau pilih ID template Shopee.")
    if payload.size_chart and chart.get("support_image_size_chart") is False:
        reject("Kategori ini tidak mendukung foto panduan ukuran; gunakan template.")
    if payload.size_chart_id and chart.get("support_template_size_chart") is False:
        reject("Kategori ini tidak mendukung template ukuran; gunakan foto.")

    def bound(value, key, label):
        rule = limits.get(key) or {}
        if rule.get("min_limit") is not None and value < Decimal(str(rule["min_limit"])):
            reject(f"{label} kurang dari batas Shopee ({rule['min_limit']}).")
        if rule.get("max_limit") is not None and value > Decimal(str(rule["max_limit"])):
            reject(f"{label} melebihi batas Shopee ({rule['max_limit']}).")

    bound(len(payload.nama), "item_name_length_limit", "Panjang nama")
    bound(len(payload.deskripsi), "item_description_length_limit", "Panjang deskripsi")
    bound(len(payload.image_ids), "item_image_count_limit", "Jumlah foto")
    for t in payload.tiers:
        bound(len(t.name), "tier_variation_name_length_limit", "Nama variasi")
        for option in t.options:
            bound(len(option.option), "tier_variation_option_length_limit", "Pilihan variasi")
    for model in [payload, *payload.models]:
        bound(model.price, "price_limit", "Harga")
        bound(model.stock, "stock_limit", "Stok")
        preorder = model.pre_order or payload.pre_order
        dts = limits.get("dts_limit") or {}
        if preorder.is_pre_order:
            rule = dts.get("days_to_ship_limit") or {}
            if rule.get("min_limit") is not None and preorder.days_to_ship < rule["min_limit"]:
                reject("Hari preorder di bawah batas kategori.")
            if rule.get("max_limit") is not None and preorder.days_to_ship > rule["max_limit"]:
                reject("Hari preorder melebihi batas kategori.")
        elif (
            dts.get("non_pre_order_days_to_ship") is not None
            and preorder.days_to_ship != dts["non_pre_order_days_to_ship"]
        ):
            reject(f"Produk ready stock menggunakan {dts['non_pre_order_days_to_ship']} hari kirim.")
        gtin = (limits.get("gtin_limit") or {}).get("gtin_validation_rule")
        if gtin in ("Mandatory", "Flexible") and not model.gtin_code:
            reject("Isi GTIN sesuai ketentuan kategori; 00 hanya jika diperbolehkan.")
        if gtin == "Mandatory" and model.gtin_code == "00":
            reject("Kategori ini mewajibkan GTIN yang valid.")


def stock(value, location_id):
    return [{"stock": value, **({"location_id": location_id} if location_id else {})}]


def parent_body(p):
    return {
        "item_name": p.nama,
        "description": p.deskripsi,
        "item_sku": p.sku,
        "original_price": float(p.price),
        "seller_stock": stock(p.stock, p.location_id),
        "weight": float(p.weight),
        "dimension": p.dimension.model_dump(),
        "pre_order": p.pre_order.model_dump(),
        "item_status": "UNLIST",
        "category_id": p.category_id,
        "condition": p.condition,
        "image": {"image_id_list": p.image_ids},
        "attribute_list": [a.model_dump(exclude_none=True) for a in p.attribute_list],
        "logistic_info": [
            logistic.model_dump(mode="json", exclude_none=True)
            | ({"shipping_fee": float(logistic.shipping_fee)} if logistic.shipping_fee is not None else {})
            for logistic in p.logistic_info
        ],
        "brand": {"brand_id": p.brand_id, "original_brand_name": p.brand_name},
        "item_dangerous": p.item_dangerous,
        **(
            {
                "size_chart_info": {
                    **({"size_chart": p.size_chart} if p.size_chart else {}),
                    **({"size_chart_id": p.size_chart_id} if p.size_chart_id else {}),
                }
            }
            if p.size_chart or p.size_chart_id
            else {}
        ),
        **({"gtin_code": p.gtin_code} if p.gtin_code else {}),
    }


def variant_body(p, item_id):
    models = []
    for m in p.models:
        row = {
            "tier_index": m.tier_index,
            "model_sku": m.sku,
            "original_price": float(m.price),
            "seller_stock": stock(m.stock, p.location_id),
        }
        for key in ("weight", "dimension", "pre_order", "gtin_code"):
            value = getattr(m, key)
            if value is not None:
                row[key] = (
                    value.model_dump()
                    if hasattr(value, "model_dump")
                    else float(value)
                    if isinstance(value, Decimal)
                    else value
                )
        models.append(row)
    return {
        "item_id": item_id,
        "standardise_tier_variation": [
            {
                "variation_id": 0,
                "variation_group_id": 0,
                "variation_name": t.name,
                "variation_option_list": [
                    {
                        "variation_option_id": 0,
                        "variation_option_name": o.option,
                        **({"image_id": o.image_id} if o.image_id else {}),
                    }
                    for o in t.options
                ],
            }
            for t in p.tiers
        ],
        "model": models,
    }


async def copy_draft(session, akun, item_id):
    response = await read(session, akun, "/api/v2/product/get_item_base_info", {"item_id_list": str(item_id)})
    item = next((r for r in response.get("item_list", []) if str(r.get("item_id")) == str(item_id)), None)
    if not item:
        raise HTTPException(status_code=404, detail="Produk sumber tidak tersedia di toko ini.")
    prices = item.get("price_info") or []
    price = prices[0].get("original_price") if prices else None
    desc = item.get("description") or "\n".join(
        f.get("text", "")
        for f in ((item.get("description_info") or {}).get("extended_description") or {}).get("field_list", [])
    )
    draft = {
        "nama": item.get("item_name", ""),
        "deskripsi": desc,
        "sku": item.get("item_sku") or "",
        "category_id": item.get("category_id"),
        "price": str(price) if price is not None else "",
        "stock": 0,
        "weight": str(item.get("weight") or ""),
        "dimension": item.get("dimension") or {},
        "pre_order": item.get("pre_order") or {"is_pre_order": False, "days_to_ship": 2},
        "condition": item.get("condition") or "NEW",
        "image_ids": (item.get("image") or {}).get("image_id_list") or [],
        "attribute_list": [
            {
                "attribute_id": a["attribute_id"],
                "attribute_value_list": [
                    {k: v for k, v in value.items() if k in {"value_id", "original_value_name", "value_unit"}}
                    for value in a.get("attribute_value_list", [])
                ],
            }
            for a in item.get("attribute_list", [])
        ],
        "brand_id": (item.get("brand") or {}).get("brand_id") or 0,
        "brand_name": (item.get("brand") or {}).get("original_brand_name") or "No Brand",
        "gtin_code": item.get("gtin_code"),
        "item_dangerous": item.get("item_dangerous") or 0,
        "tiers": [],
        "models": [],
    }
    if item.get("has_model"):
        variants = await read(session, akun, "/api/v2/product/get_model_list", {"item_id": item_id})
        if not variants.get("model") or not variants.get("tier_variation"):
            raise HTTPException(status_code=424, detail="Varian sumber belum lengkap. Salinan tidak dibuat.")
        draft["tiers"] = [
            {
                "name": t["name"],
                "options": [
                    {"option": o["option"], "image_id": (o.get("image") or {}).get("image_id")}
                    for o in t["option_list"]
                ],
            }
            for t in variants["tier_variation"]
        ]
        for model in variants["model"]:
            prices = model.get("price_info") or []
            draft["models"].append(
                {
                    "tier_index": model["tier_index"],
                    "sku": model.get("model_sku") or "",
                    "stock": 0,
                    "price": str(prices[0]["original_price"]) if prices else "",
                    **{
                        k: model[k]
                        for k in ("weight", "dimension", "pre_order", "gtin_code")
                        if model.get(k) is not None
                    },
                }
            )
    return draft


async def dispute_reasons(session, akun, sn):
    response = await read(session, akun, "/api/v2/returns/get_return_dispute_reason", {"return_sn": sn})
    reasons = response.get("dispute_reason_list")
    if not isinstance(reasons, list) or any(
        not isinstance(r, dict)
        or type(r.get("dispute_reason")) is not int
        or not isinstance(r.get("evidence_module_list"), list)
        for r in reasons
    ):
        raise shopee.ShopeeAPIError("returns", "incomplete_response", "Persyaratan sengketa belum lengkap.", None)
    return {"reasons": reasons}


def evidence_url(url):
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return False
    host = p.hostname or ""
    return (
        p.scheme == "https"
        and not p.username
        and not p.password
        and port in (None, 443)
        and any(
            host == h or host.endswith("." + h)
            for h in ("scsusercontent.com", "shopee.com", "shopee.co.id", "shopee.sg", "shopeemobile.com")
        )
    )


async def dispute(session, akun, sn, payload):
    reasons = (await dispute_reasons(session, akun, sn))["reasons"]
    reason = next((r for r in reasons if r["dispute_reason"] == payload.reason_id), None)
    if reason is None:
        reject("Alasan sengketa tidak tersedia lagi. Muat ulang persyaratan.")
    modules = {r["module_index"]: r for r in reason["evidence_module_list"]}
    evidence = {r.module_index: r for r in payload.evidence}
    if len(evidence) != len(payload.evidence) or set(evidence) - set(modules):
        reject("Modul bukti tidak sesuai persyaratan sengketa.")
    images = []
    for index, module in modules.items():
        urls = evidence[index].urls if index in evidence else []
        if module.get("is_required") and not urls:
            reject(f"Bukti untuk bagian {index} wajib diunggah.")
        if any(not evidence_url(url) for url in urls):
            reject("Gunakan URL bukti dari upload Shopee.")
        if urls:
            images.append({"module_index": index, "requirement": module["requirement"], "image_url": urls})
    path = "/api/v2/returns/dispute"
    data = await shopee.signed_shop_request(
        session,
        akun,
        path,
        method="POST",
        body={
            "return_sn": sn,
            "email": str(payload.email),
            "dispute_reason_id": payload.reason_id,
            "dispute_text_reason": payload.text,
            "image_list": images,
        },
    )
    if (data.get("response") or {}).get("return_sn") != sn:
        raise shopee.ShopeeAPIError(
            path,
            "unconfirmed_response",
            "Sengketa belum terkonfirmasi. Segarkan sebelum mengirim ulang.",
            data.get("request_id"),
        )
    return {"ok": True, "request_id": data.get("request_id"), "warnings": []}


async def wallet(session, akun, dari, sampai, offset=0):
    start, end = rentang_retur(dari, sampai)
    path = "/api/v2/payment/get_wallet_transaction_list"
    data = await read(
        session, akun, path, {"page_no": offset, "page_size": 40, "create_time_from": start, "create_time_to": end}
    )
    rows = data.get("transaction_list")
    try:
        if not isinstance(rows, list) or type(data.get("more")) is not bool or (data["more"] and not rows):
            raise ValueError("incomplete page")
        result = []
        for row in rows:
            if (
                not isinstance(row, dict)
                or not row.get("status")
                or row.get("transaction_type") is None
                or type(row.get("create_time")) is not int
            ):
                raise ValueError("incomplete transaction")
            if (
                not isinstance(row["status"], str)
                or not isinstance(row["transaction_type"], (str, int))
                or isinstance(row["transaction_type"], bool)
            ):
                raise ValueError("invalid identity")
            entry = {
                k: row.get(k)
                for k in (
                    "status",
                    "transaction_type",
                    "create_time",
                    "order_sn",
                    "refund_sn",
                    "withdrawal_type",
                    "description",
                    "money_flow",
                    "reason",
                )
            }
            entry["transaction_type"] = str(row["transaction_type"])
            for k in ("withdrawal_id", "root_withdrawal_id"):
                entry[k] = str(row[k]) if row.get(k) is not None else None
            for key in ("amount", "current_balance", "transaction_fee"):
                value = Decimal(str(row[key])) if row.get(key) is not None else None
                if value is not None and not value.is_finite():
                    raise ValueError("invalid amount")
                if key == "amount" and value is None:
                    raise ValueError("missing amount")
                entry[key] = str(value) if value is not None else None
            result.append(entry)
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise shopee.ShopeeAPIError(
            path, "incomplete_response", "Transaksi dana belum lengkap. Muat ulang.", None
        ) from exc
    return {
        "items": result,
        "offset": offset,
        "next_offset": offset + len(rows),
        "ada_lagi": data["more"],
        "nama_toko": akun.nama_toko,
    }
