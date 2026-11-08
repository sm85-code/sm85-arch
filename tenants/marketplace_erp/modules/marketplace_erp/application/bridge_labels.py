"""Official labels for existing bridge identities; source shipment state is read only."""

import base64
import io
import json
from fastapi import HTTPException
from pypdf import PdfReader
from sqlalchemy import select
from ..infrastructure.models import BridgeOrder
from ..infrastructure.adapters import easyboss as eb

PRINTABLE = {
    "tiktokshop": {"AWAITING_COLLECTION", "IN_TRANSIT", "DELIVERED", "COMPLETED"},
    "lazada": {"ready_to_ship", "shipped", "delivered"},
}


def source(order):
    if order is None:
        return None
    try:
        data = json.loads(order.detail_json or "{}")
        value = data.get("easyboss") if isinstance(data, dict) else None
        return value if isinstance(value, dict) and value.get("platform") in PRINTABLE else None
    except (ValueError, TypeError):
        return None


async def download(session, order):
    original = source(order)
    if not original:
        raise HTTPException(409, "Pesanan bukan impor EasyBoss.")
    receipt = await session.scalar(select(BridgeOrder).where(BridgeOrder.pesanan_id == order.id))
    if not receipt or (receipt.platform, receipt.shop_id, receipt.order_id) != (
        original.get("platform"),
        original.get("shop_id"),
        original.get("order_id"),
    ):
        raise HTTPException(409, "Identitas pesanan EasyBoss tidak cocok.")
    if receipt.source_status not in PRINTABLE.get(receipt.platform, set()):
        raise HTTPException(409, "Proses pesanan di EasyBoss terlebih dahulu sebelum mencetak resi.")
    packages = original.get("package_ids") or []
    if len(packages) != 1:
        raise HTTPException(409, "Resi pesanan dengan beberapa paket perlu diperiksa di EasyBoss.")
    native = "tiktok" if receipt.platform == "tiktokshop" else receipt.platform
    try:
        async with eb.client() as client:
            data = await eb.Gateway(client).call(
                "eb_resi_pdf",
                {
                    "platform": native,
                    "shop_id": receipt.shop_id,
                    "no_pesanan": receipt.order_id,
                    "package_id": packages[0],
                },
            )
        if any(
            data.get(k) != v
            for k, v in {
                "platform": native,
                "shop_id": receipt.shop_id,
                "order_id": receipt.order_id,
                "package_id": packages[0],
            }.items()
        ):
            raise ValueError()
        value = data["pdf_base64"]
        if not isinstance(value, str) or len(value) > 6_000_000:
            raise ValueError()
        pdf = base64.b64decode(value, validate=True)
        if not pdf.startswith(b"%PDF-"):
            raise ValueError()
        reader = PdfReader(io.BytesIO(pdf))
        if reader.is_encrypted or not 0 < len(reader.pages) <= 20:
            raise ValueError()
        return pdf
    except Exception:
        raise HTTPException(
            424, "Resi resmi belum tersedia atau status pesanan berubah. Sinkronkan dan coba lagi."
        ) from None
