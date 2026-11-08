"""Official EasyBoss label retrieval. No shipment or print-log mutation."""

import base64
import json

PATH = "/api/order/waybill/getPackageIdAndWaybillPdfBase64Map"
STATES = {
    "tiktok": {"AWAITING_COLLECTION", "IN_TRANSIT", "DELIVERED", "COMPLETED"},
    "lazada": {"ready_to_ship", "shipped", "delivered"},
}


def read_label(post, platform, shop_id, no_pesanan, package_id):
    def fail():
        return json.dumps({"error": "label_unavailable_or_identity_changed"})

    if platform not in STATES or not all(
        isinstance(x, str) and 0 < len(x) <= 200 for x in [shop_id, no_pesanan, package_id]
    ):
        return fail()
    data = post(
        "/api/order/package/searchOrderPackageList",
        {"pageSize": 2, "page": 1, "appPackageTab": "all", "platformOrderSns": no_pesanan},
    )
    rows = data.get("packageList") or []
    if data.get("total") != 1 or len(rows) != 1:
        return fail()
    p = rows[0]
    o = p.get("orderInfo") or {}
    identity = (
        str(o.get("platform")),
        str(o.get("shopId")),
        str(o.get("platformOrderSn")),
        str(p.get("opOrderPackageId")),
    )
    if identity != (platform, shop_id, no_pesanan, package_id) or p.get("isSplit") != 0:
        return fail()
    if (
        o.get("platformOrderStatus") not in STATES[platform]
        or any(p.get(k) for k in ["isOnHoldOrder", "isPendingStatus", "cancelOrder", "returnOrder"])
        or str(p.get("isIsolation")) == "1"
        or o.get("gmtRefund")
        or o.get("opOrderReturns")
    ):
        return fail()
    data = post(PATH, {"opOrderPackageIds": [package_id], "type": "requestPrintWaybill"})
    labels = data.get("opPackageIdAndWaybillPdfDataBase64Map") or {}
    if data.get("errors") or set(labels) != {package_id}:
        return fail()
    value = labels[package_id]
    try:
        if not isinstance(value, str) or len(value) > 6_000_000:
            return fail()
        pdf = base64.b64decode(value, validate=True)
        if not pdf.startswith(b"%PDF-"):
            return fail()
    except (ValueError, TypeError):
        return fail()
    return json.dumps(
        {
            "platform": platform,
            "shop_id": shop_id,
            "order_id": no_pesanan,
            "package_id": package_id,
            "pdf_base64": value,
        }
    )
