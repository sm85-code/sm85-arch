"""Bounded uploads with documented public/shop signatures; never follows redirects or fetches user URLs."""

import asyncio
import time
from urllib.parse import urlencode

import requests
from fastapi import HTTPException
from shared.egress import proxies_for
from . import erp_shopee as shopee

MAX_IMAGE = 10 * 1024 * 1024


async def upload(session, akun, content: bytes, *, nomor_retur: str | None = None):
    if not shopee.live_sync_enabled() or not shopee.partner_configured():
        raise shopee.ShopeeNotConfigured()
    if not content or len(content) > MAX_IMAGE:
        raise HTTPException(status_code=422, detail="Foto maksimal 10 MB.")
    if content.startswith(b"\xff\xd8\xff"):
        ext, mime = "jpg", "image/jpeg"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        ext, mime = "png", "image/png"
    else:
        raise HTTPException(status_code=422, detail="Gunakan foto JPG/JPEG/PNG.")
    path = "/api/v2/returns/convert_image" if nomor_retur else "/api/v2/media_space/upload_image"
    ts = int(time.time())
    query = {"partner_id": shopee._partner_id_int(), "timestamp": ts}
    if nomor_retur:
        if not akun.id_toko_eksternal or not akun.access_token:
            raise shopee.ShopeeNotConfigured()
        await shopee.pastikan_token_segar(session, akun)
        query.update(access_token=akun.access_token, shop_id=int(akun.id_toko_eksternal))
        query["sign"] = shopee.sign_request(
            path, ts, access_token=akun.access_token, shop_id=str(akun.id_toko_eksternal)
        )
    else:
        query["sign"] = shopee.sign_request(path, ts)

    def send():
        response = requests.post(
            shopee._host() + path + "?" + urlencode(query),
            data={"return_sn": nomor_retur} if nomor_retur else {"scene": "normal"},
            files={("upload_image" if nomor_retur else "image"): ("foto." + ext, content, mime)},
            timeout=30,
            allow_redirects=False,
            proxies=proxies_for("SHOPEE_PROXY_URL"),
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="Respons upload Shopee bukan JSON.") from exc
        if not isinstance(data, dict) or not response.ok:
            raise HTTPException(status_code=502, detail="Upload foto gagal di Shopee.")
        if str(data.get("error") or "").strip():
            raise shopee.ShopeeAPIError(
                path, str(data["error"]), str(data.get("message") or ""), data.get("request_id")
            )
        return data

    try:
        data = await asyncio.to_thread(send)
    except requests.RequestException as exc:
        raise shopee.ShopeeAPIError(
            path, "upload_connection_failed", "Koneksi upload gagal. Coba unggah kembali.", None
        ) from exc
    r = data.get("response") or {}
    if nomor_retur:
        if not isinstance(r.get("url"), str) or not r["url"].startswith("https://"):
            raise shopee.ShopeeAPIError(
                path, "unconfirmed_upload", "URL bukti belum terkonfirmasi.", data.get("request_id")
            )
        return {"url": r["url"], "request_id": data.get("request_id")}
    image = r.get("image_info") or {}
    if not image:
        entries = r.get("image_info_list") or []
        image = (entries[0].get("image_info") or {}) if len(entries) == 1 else {}
    if not isinstance(image.get("image_id"), str) or not image["image_id"]:
        raise shopee.ShopeeAPIError(path, "unconfirmed_upload", "ID foto belum terkonfirmasi.", data.get("request_id"))
    return {"image_id": image["image_id"], "request_id": data.get("request_id")}
