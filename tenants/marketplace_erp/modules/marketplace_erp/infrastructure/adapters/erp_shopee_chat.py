"""Seller-buyer Chat v2. The browser never chooses a recipient independently of a conversation.

Official references: documents module=109, ids 671–674 and 679; FAQ 137 lists Seller Chat.
SDK cross-checks: EcomPHP/shopee-php Chat.php and easycb/easycb-go model_chat.go.
"""

import time
import asyncio

from fastapi import HTTPException
from . import erp_shopee as api

BASE = "/api/v2/sellerchat/"
_ROLE_CACHE = {}  # Small, short-lived cache keyed by shop/conversation/latest message.


def seller_message(message, shop_id):
    """Only a seller-to-user or user-to-seller exchange proves seller role.

    Positive shop IDs on both sides are ambiguous shop-to-shop exchanges and
    must not be guessed from a username, unread count, or an existing order.
    """
    source, target = message.get("from_shop_id"), message.get("to_shop_id")
    if source is None or target is None:
        return None
    return (str(source) == str(shop_id) and str(target) == "0") or (str(target) == str(shop_id) and str(source) == "0")


async def seller_history(akun, conversation_id):
    # Refresh/commit tokens once on this request's session before parallel reads.
    # Parallel calls below never share an AsyncSession.
    data = await api._call_shop_api(
        access_token=str(akun.access_token),
        shop_id=str(akun.id_toko_eksternal),
        api_path=BASE + "get_message",
        params={"conversation_id": conversation_id, "page_size": 20},
        timeout=8.0,
    )
    resp = data.get("response")
    if not isinstance(resp, dict) or not isinstance(resp.get("messages"), list):
        raise HTTPException(424, "Format riwayat pesan Shopee tidak lengkap")
    if any(str(m.get("conversation_id")) != conversation_id for m in resp["messages"]):
        raise HTTPException(424, "Identitas pesan Shopee tidak sesuai percakapan")
    return resp["messages"]


async def inbox(session, akun, cursor=None, unread=False):
    params = {
        "direction": "older",
        "type": "unread" if unread else "all",
        "page_size": 20,
        "next_timestamp_nano": cursor or str(time.time_ns()),
    }
    data = await api.signed_shop_request(session, akun, BASE + "get_conversation_list", params=params)
    resp = data.get("response")
    if not isinstance(resp, dict) or not isinstance(resp.get("conversations"), list):
        raise HTTPException(424, "Format daftar percakapan Shopee tidak lengkap")
    # Shopee accounts can also buy from other shops. Only this seller shop belongs in ERP.
    if any(not c.get("shop_id") for c in resp["conversations"]):
        raise HTTPException(424, "Shopee tidak menyertakan identitas toko percakapan")
    resp["conversations"] = [c for c in resp["conversations"] if str(c["shop_id"]) == str(akun.id_toko_eksternal)]
    semaphore = asyncio.Semaphore(4)

    async def verify(c):
        key = (str(akun.id_toko_eksternal), str(c["conversation_id"]), str(c.get("latest_message_id") or ""))
        cached = _ROLE_CACHE.get(key)
        if key[2] and cached and cached[0] > time.monotonic():
            latest = cached[1]
        else:
            async with semaphore:
                history = await seller_history(akun, str(c["conversation_id"]))
            approved = [m for m in history if seller_message(m, akun.id_toko_eksternal) is True]
            latest = max(approved, key=lambda m: int(m.get("created_timestamp") or 0)) if approved else None
            if key[2]:
                if len(_ROLE_CACHE) >= 512:
                    _ROLE_CACHE.clear()
                _ROLE_CACHE[key] = (time.monotonic() + 120, latest)
        if latest is None:
            return None
        # Reuse role evidence only: unread count and inbox metadata must stay fresh.
        if str(c.get("latest_message_id")) != str(latest.get("message_id")):
            c = {
                **c,
                "latest_message_id": latest.get("message_id"),
                "latest_message_content": latest.get("content"),
                "latest_message_type": latest.get("message_type"),
                "latest_message_from_id": latest.get("from_id"),
                "last_message_timestamp": latest.get("created_timestamp"),
                "unread_count": None,
            }
        return c

    try:
        verified = await asyncio.wait_for(asyncio.gather(*(verify(c) for c in resp["conversations"])), timeout=20)
    except TimeoutError:
        raise HTTPException(504, "Verifikasi peran Chat terlalu lama. Sinkronkan satu toko dan coba lagi.")
    hidden = sum(c is None for c in verified)
    resp["conversations"] = [c for c in verified if c is not None]
    resp["role_unverified_count"] = hidden
    for c in resp["conversations"]:
        sender, buyer = c.get("latest_message_from_id"), c.get("to_id")
        c["needs_reply"] = str(sender) == str(buyer) if sender and buyer else None
    return resp


async def conversation(session, akun, conversation_id):
    data = await api.signed_shop_request(
        session, akun, BASE + "get_one_conversation", params={"conversation_id": conversation_id}
    )
    resp = data.get("response") or {}
    if str(resp.get("conversation_id")) != conversation_id or not resp.get("to_id"):
        raise HTTPException(424, "Identitas percakapan Shopee belum dapat dipastikan")
    if not resp.get("shop_id"):
        raise HTTPException(424, "Shopee tidak menyertakan identitas toko percakapan")
    if str(resp["shop_id"]) != str(akun.id_toko_eksternal):
        raise HTTPException(403, "Percakapan berasal dari toko lain")
    history = await seller_history(akun, conversation_id)
    if not any(seller_message(m, akun.id_toko_eksternal) is True for m in history):
        raise HTTPException(403, "Percakapan belum terverifikasi sebagai chat pembeli ke toko penjual")
    return resp


async def messages(session, akun, conversation_id, offset=None):
    params = {"conversation_id": conversation_id, "page_size": 20}
    if offset:
        params["offset"] = offset
    data = await api.signed_shop_request(session, akun, BASE + "get_message", params=params)
    resp = data.get("response")
    if not isinstance(resp, dict) or not isinstance(resp.get("messages"), list):
        raise HTTPException(424, "Format riwayat pesan Shopee tidak lengkap")
    unique = {}
    for message in resp["messages"]:
        if str(message.get("conversation_id")) != conversation_id or not message.get("message_id"):
            raise HTTPException(424, "Identitas pesan Shopee tidak sesuai percakapan")
        role = seller_message(message, akun.id_toko_eksternal)
        if role is False or (role is None and message.get("message_type") not in {"notification", "system"}):
            continue
        unique[str(message["message_id"])] = message
    return {**resp, "messages": list(unique.values())}


async def send(session, akun, target, text):
    return await send_content(session, akun, target, "text", {"text": text})


async def send_content(session, akun, target, message_type, content):
    data = await api.signed_shop_request(
        session,
        akun,
        BASE + "send_message",
        method="POST",
        body={"to_id": int(target["to_id"]), "message_type": message_type, "content": content},
    )
    resp = data.get("response") or {}
    if not resp.get("message_id") or str(resp.get("to_id")) != str(target["to_id"]):
        raise HTTPException(502, "Hasil kirim belum pasti; periksa riwayat sebelum mengirim lagi")
    return resp
