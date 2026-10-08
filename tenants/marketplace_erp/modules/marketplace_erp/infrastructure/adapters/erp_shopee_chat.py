"""Seller-buyer Chat v2. The browser never chooses a recipient independently of a conversation.

Official references: documents module=109, ids 671–674 and 679; FAQ 137 lists Seller Chat.
SDK cross-checks: EcomPHP/shopee-php Chat.php and easycb/easycb-go model_chat.go.
"""

from fastapi import HTTPException
from . import erp_shopee as api

BASE = "/api/v2/sellerchat/"


async def inbox(session, akun, cursor=None, unread=False):
    params = {"direction": "latest", "type": "unread" if unread else "all", "page_size": 20}
    if cursor:
        params["next_timestamp_nano"] = cursor
    data = await api.signed_shop_request(session, akun, BASE + "get_conversation_list", params=params)
    resp = data.get("response")
    if not isinstance(resp, dict) or not isinstance(resp.get("conversations"), list):
        raise HTTPException(424, "Format daftar percakapan Shopee tidak lengkap")
    return resp


async def conversation(session, akun, conversation_id):
    data = await api.signed_shop_request(
        session, akun, BASE + "get_one_conversation", params={"conversation_id": conversation_id}
    )
    resp = data.get("response") or {}
    if str(resp.get("conversation_id")) != conversation_id or not resp.get("to_id"):
        raise HTTPException(424, "Identitas percakapan Shopee belum dapat dipastikan")
    if resp.get("shop_id") is not None and str(resp["shop_id"]) != str(akun.id_toko_eksternal):
        raise HTTPException(403, "Percakapan berasal dari toko lain")
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
