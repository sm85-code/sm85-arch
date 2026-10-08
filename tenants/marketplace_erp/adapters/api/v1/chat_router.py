"""Account-scoped Chat reads and durable, non-replayed text-send receipts."""

import hashlib
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .workflow_router import account, staff
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee_chat as provider
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters.erp_shopee import ShopeeAPIError
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    PesanMarketplaceReceipt,
    Pesanan,
    KatalogShopee,
    UserMarketplaceErp,
)

router = APIRouter()


class SendIn(BaseModel):
    operation_id: UUID
    text: str = Field(min_length=1, max_length=1000)


def receipt_out(row):
    return {"operation_id": row.operation_id, "status": row.status, **json.loads(row.result_json)}


@router.get("/akun/{akun_id}/chat")
async def inbox(
    akun_id: str,
    cursor: str | None = Query(None, max_length=100),
    unread: bool = False,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(staff),
):
    akun = await account(session, user, akun_id, "chat")
    return await provider.inbox(session, akun, cursor, unread)


@router.get("/akun/{akun_id}/chat/{conversation_id}/pesan")
async def messages(
    akun_id: str,
    conversation_id: str,
    offset: str | None = Query(None, max_length=128),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(staff),
):
    akun = await account(session, user, akun_id, "chat")
    target = await provider.conversation(session, akun, conversation_id)
    history = await provider.messages(session, akun, conversation_id, offset)
    order_numbers, item_ids = set(), set()
    for message in history["messages"]:
        content = message.get("content")
        if isinstance(content, dict):
            if content.get("order_sn"):
                order_numbers.add(str(content["order_sn"]))
            if content.get("item_id"):
                item_ids.add(str(content["item_id"]))
    orders = (
        (
            await session.execute(
                select(Pesanan).where(Pesanan.akun_id == akun.id, Pesanan.id_eksternal.in_(order_numbers))
            )
        )
        .scalars()
        .all()
        if order_numbers
        else []
    )
    products = (
        (
            await session.execute(
                select(KatalogShopee).where(KatalogShopee.akun_id == akun.id, KatalogShopee.item_id.in_(item_ids))
            )
        )
        .scalars()
        .all()
        if item_ids
        else []
    )
    order_map = {row.id_eksternal: row.id for row in orders}
    product_map = {row.item_id: row.id for row in products}
    for message in history["messages"]:
        content = message.get("content")
        if isinstance(content, dict):
            message["context"] = {
                "order_id": order_map.get(str(content.get("order_sn"))),
                "katalog_id": product_map.get(str(content.get("item_id"))),
            }
    return {"conversation": target, **history}


@router.post("/akun/{akun_id}/chat/{conversation_id}/pesan")
async def send(
    akun_id: str,
    conversation_id: str,
    body: SendIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(staff),
):
    akun = await account(session, user, akun_id, "chat")
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "Pesan tidak boleh kosong")
    operation = str(body.operation_id)
    fingerprint = hashlib.sha256(json.dumps([conversation_id, text], ensure_ascii=False).encode()).hexdigest()
    stmt = select(PesanMarketplaceReceipt).where(
        PesanMarketplaceReceipt.akun_id == akun_id, PesanMarketplaceReceipt.operation_id == operation
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing:
        if existing.payload_hash != fingerprint:
            raise HTTPException(409, "ID pengiriman sudah dipakai untuk pesan berbeda")
        return receipt_out(existing)
    # Read-only validation and token refresh finish before claiming a durable send.
    target = await provider.conversation(session, akun, conversation_id)
    row = PesanMarketplaceReceipt(
        akun_id=akun_id,
        operation_id=operation,
        payload_hash=fingerprint,
        status="belum_pasti",
        result_json=json.dumps({"conversation_id": conversation_id}),
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is None or existing.payload_hash != fingerprint:
            raise HTTPException(409, "ID pengiriman sedang dipakai")
        return receipt_out(existing)
    try:
        result = await provider.send(session, akun, target, text)
    except ShopeeAPIError as exc:
        # A documented provider rejection is distinct from a lost/ambiguous response.
        row.status = "gagal"
        row.result_json = json.dumps({"conversation_id": conversation_id, "error": str(exc.detail)})
    except Exception:
        row.result_json = json.dumps(
            {
                "conversation_id": conversation_id,
                "error": "Hasil kirim belum pasti. Segarkan riwayat dan periksa pesan; pengiriman ini tidak diulang otomatis.",
            }
        )
    else:
        row.status = "terkirim"
        row.result_json = json.dumps({"conversation_id": conversation_id, "message": result})
    await session.commit()
    return receipt_out(row)


class ReadIn(BaseModel):
    message_id: str = Field(min_length=1, max_length=128)


@router.post("/akun/{akun_id}/chat/{conversation_id}/dibaca")
async def read(
    akun_id: str,
    conversation_id: str,
    body: ReadIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(staff),
):
    akun = await account(session, user, akun_id, "chat")
    await provider.conversation(session, akun, conversation_id)
    history = await provider.messages(session, akun, conversation_id)
    if body.message_id not in {str(m["message_id"]) for m in history["messages"]}:
        raise HTTPException(409, "Pesan terakhir berubah; segarkan percakapan terlebih dahulu")
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters.erp_shopee import signed_shop_request

    await signed_shop_request(
        session,
        akun,
        provider.BASE + "read_conversation",
        method="POST",
        body={"conversation_id": conversation_id, "last_read_message_id": body.message_id},
    )
    return {"ok": True}
