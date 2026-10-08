"""Account-scoped Chat reads and durable, non-replayed text-send receipts."""

import hashlib
import json
from uuid import UUID
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .workflow_router import account, staff
from . import chat_context
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee_chat as provider
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters.erp_shopee import ShopeeAPIError
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    PesanMarketplaceReceipt,
    Pesanan,
    KatalogShopee,
    UserMarketplaceErp,
    PercakapanBelanja,
    AkunMarketplace,
)

router = APIRouter()


class SendIn(BaseModel):
    operation_id: UUID
    text: str = Field(default="", max_length=1000)
    message_type: Literal["text", "item", "order"] = "text"
    attachment_id: str | None = Field(default=None, max_length=64)


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
    result = await provider.inbox(session, akun, cursor, unread)
    hidden = set(
        (
            await session.execute(select(PercakapanBelanja.conversation_id).where(PercakapanBelanja.akun_id == akun.id))
        ).scalars()
    )
    result["conversations"] = [c for c in result["conversations"] if str(c["conversation_id"]) not in hidden]
    orders = await chat_context.buyer_orders(session, akun, result["conversations"])
    for c in result["conversations"]:
        c["kota"] = next(
            (
                json.loads(o.detail_json or "{}").get("kota")
                for o in orders
                if chat_context.buyer_matches(o, c) and json.loads(o.detail_json or "{}").get("kota")
            ),
            None,
        )
    return result


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
                select(Pesanan)
                .options(selectinload(Pesanan.items))
                .where(Pesanan.akun_id == akun.id, Pesanan.id_eksternal.in_(order_numbers))
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
    await chat_context.services.lengkapi_foto_item(session, orders)
    order_cards = {row.id_eksternal: chat_context.order_card(row) for row in orders}
    product_cards = {row.item_id: chat_context.product_card(row) for row in products}
    order_map = {row.id_eksternal: row.id for row in orders}
    product_map = {row.item_id: row.id for row in products}
    for message in history["messages"]:
        content = message.get("content")
        if isinstance(content, dict):
            message["context"] = {
                "order_id": order_map.get(str(content.get("order_sn"))),
                "katalog_id": product_map.get(str(content.get("item_id"))),
                "order": order_cards.get(str(content.get("order_sn"))),
                "product": product_cards.get(str(content.get("item_id"))),
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
    return await deliver(session, akun, conversation_id, body)


async def deliver(session, akun, conversation_id, body, target=None):
    akun_id = akun.id
    text = body.text.strip()
    if body.message_type == "text" and (not text or body.attachment_id):
        raise HTTPException(422, "Tulis balasan teks atau pilih satu lampiran")
    if body.message_type != "text" and (not body.attachment_id or text):
        raise HTTPException(422, "Pilih lampiran; teks dikirim sebagai pesan terpisah")
    operation = str(body.operation_id)
    payload = (
        [conversation_id, text]
        if body.message_type == "text"
        else [conversation_id, body.message_type, body.attachment_id]
    )
    fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
    stmt = select(PesanMarketplaceReceipt).where(
        PesanMarketplaceReceipt.akun_id == akun_id, PesanMarketplaceReceipt.operation_id == operation
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing:
        if existing.payload_hash != fingerprint:
            raise HTTPException(409, "ID pengiriman sudah dipakai untuk pesan berbeda")
        return receipt_out(existing)
    # Read-only validation and token refresh finish before claiming a durable send.
    target = target or await provider.conversation(session, akun, conversation_id)
    content = (
        await chat_context.attachment(session, akun, target, body.message_type, body.attachment_id)
        if body.message_type != "text"
        else None
    )
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
        result = (
            await provider.send(session, akun, target, text)
            if body.message_type == "text"
            else await provider.send_content(session, akun, target, body.message_type, content)
        )
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
    message_id: str | None = Field(default=None, min_length=1, max_length=128)


@router.post("/akun/{akun_id}/chat/{conversation_id}/dibaca")
async def read(
    akun_id: str,
    conversation_id: str,
    body: ReadIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(staff),
):
    akun = await account(session, user, akun_id, "chat")
    target = await provider.conversation(session, akun, conversation_id)
    message_id = body.message_id or target.get("latest_message_id")
    if not message_id:
        raise HTTPException(424, "Shopee belum menyediakan ID pesan terakhir. Sinkronkan percakapan dan coba lagi.")
    message_id = str(message_id)
    # The scoped conversation is authoritative even when get_message returns an empty page.
    if message_id != str(target.get("latest_message_id") or ""):
        history = await provider.messages(session, akun, conversation_id)
        if message_id not in {str(m["message_id"]) for m in history["messages"]}:
            raise HTTPException(409, "Pesan terakhir berubah; segarkan percakapan terlebih dahulu")
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters.erp_shopee import signed_shop_request

    await signed_shop_request(
        session,
        akun,
        provider.BASE + "read_conversation",
        method="POST",
        body={"conversation_id": conversation_id, "last_read_message_id": message_id},
    )
    return {"ok": True}


@router.get("/akun/{akun_id}/chat/{conversation_id}/konteks")
async def conversation_context(
    akun_id: str,
    conversation_id: str,
    q: str = Query("", max_length=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(staff),
):
    akun = await account(session, user, akun_id, "chat")
    target = await provider.conversation(session, akun, conversation_id)
    return await chat_context.context(session, akun, target, q, offset)


@router.get("/pesanan/{pesanan_id}/chat")
async def order_chat(
    pesanan_id: str,
    q: str = Query("", max_length=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(staff),
):
    row = await chat_context.get_order(session, pesanan_id)
    akun = await account(session, user, row.akun_id, "chat")
    target = await chat_context.order_target(session, akun, row)
    recent = await provider.inbox(session, akun)
    conversation = next((c for c in recent["conversations"] if str(c.get("to_id")) == str(target["to_id"])), target)
    return {
        "akun_id": akun.id,
        "conversation": conversation,
        "context": await chat_context.context(session, akun, target, q, offset),
        "order": chat_context.order_card(row),
    }


@router.post("/pesanan/{pesanan_id}/chat/pesan")
async def start_order_chat(
    pesanan_id: str,
    body: SendIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(staff),
):
    row = await chat_context.get_order(session, pesanan_id)
    akun = await account(session, user, row.akun_id, "chat")
    target = await chat_context.order_target(session, akun, row)
    return await deliver(session, akun, "order:" + row.id, body, target)


class BelanjaIn(BaseModel):
    belanja: bool


@router.get("/akun/{akun_id}/chat-belanja")
async def list_buying_chats(
    akun_id: str,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(staff),
):
    akun = await account(session, user, akun_id, "chat")
    rows = (
        (
            await session.execute(
                select(PercakapanBelanja)
                .where(PercakapanBelanja.akun_id == akun.id)
                .order_by(PercakapanBelanja.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [{"conversation_id": r.conversation_id, "nama": r.nama} for r in rows]


@router.post("/akun/{akun_id}/chat/{conversation_id}/belanja")
async def classify_buying_chat(
    akun_id: str,
    conversation_id: str,
    body: BelanjaIn,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    user: UserMarketplaceErp = Depends(staff),
):
    akun = await account(session, user, akun_id, "chat")
    target = await provider.conversation(session, akun, conversation_id) if body.belanja else None
    # Serialize classifications for this shop; different staff may update the same thread.
    await session.execute(select(AkunMarketplace.id).where(AkunMarketplace.id == akun.id).with_for_update())
    row = await session.get(PercakapanBelanja, (akun.id, conversation_id))
    if body.belanja and row is None:
        session.add(
            PercakapanBelanja(
                akun_id=akun.id,
                conversation_id=conversation_id,
                nama=str(target.get("to_name") or "Pembeli")[:255],
            )
        )
    elif not body.belanja and row is not None:
        await session.delete(row)
    await session.commit()
    return {"ok": True, "belanja": body.belanja}
