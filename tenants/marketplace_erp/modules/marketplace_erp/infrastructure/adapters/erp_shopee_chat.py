"""Seller-buyer Chat v2. The browser never chooses a recipient independently of a conversation.

Official references: documents module=109, ids 671–674 and 679; FAQ 137 lists Seller Chat.
SDK cross-checks: EcomPHP/shopee-php Chat.php and easycb/easycb-go model_chat.go.
"""

import time

from fastapi import HTTPException
from . import erp_shopee as api

BASE = "/api/v2/sellerchat/"


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


async def upload_photo(session, akun, content):
    """Chat image upload (doc 683); upload alone never sends a message."""
    import asyncio
    from urllib.parse import urlencode, urlsplit
    import requests
    from shared.egress import proxies_for
    if not api.live_sync_enabled() or not api.partner_configured():
        raise api.ShopeeNotConfigured()
    if not content or len(content) > 10 * 1024 * 1024:
        raise HTTPException(422, "Foto maksimal 10 MB.")
    if content.startswith(b"\xff\xd8\xff"):
        mime, ext = "image/jpeg", "jpg"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        mime, ext = "image/png", "png"
    else:
        raise HTTPException(422, "Gunakan foto JPG/JPEG/PNG.")
    await api.pastikan_token_segar(session, akun)
    if not akun.access_token or not akun.id_toko_eksternal:
        raise api.ShopeeNotConfigured()
    path = BASE + "upload_image"
    timestamp = int(time.time())
    query = {"partner_id": api._partner_id_int(), "timestamp": timestamp,
             "access_token": akun.access_token, "shop_id": int(akun.id_toko_eksternal),
             "sign": api.sign_request(path, timestamp, access_token=akun.access_token, shop_id=str(akun.id_toko_eksternal))}
    def upload():
        try:
            response = requests.post(api._host() + path + "?" + urlencode(query),
                                     files={"file": ("foto." + ext, content, mime)}, timeout=30,
                                     allow_redirects=False, proxies=proxies_for("SHOPEE_PROXY_URL"))
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise HTTPException(502, "Unggah foto belum berhasil. Pesan belum dikirim.") from exc
        if not response.ok or not isinstance(data, dict):
            raise HTTPException(502, "Unggah foto ditolak Shopee. Pesan belum dikirim.")
        if data.get("error"):
            raise api.ShopeeAPIError(path, str(data["error"]), str(data.get("message") or ""), data.get("request_id"))
        url = (data.get("response") or {}).get("url")
        parsed = urlsplit(url) if isinstance(url, str) else None
        if not parsed or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise HTTPException(424, "Shopee belum menyediakan URL foto. Pesan belum dikirim.")
        return url
    return await asyncio.to_thread(upload)
