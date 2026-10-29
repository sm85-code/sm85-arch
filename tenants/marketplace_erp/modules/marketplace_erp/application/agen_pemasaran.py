"""Read routes and guarded writes for vouchers, flash sales, follow prizes, video upload, and listing issues.

Writes use the ads-agent guard: dry-run by default, owner approval ``ya``, STOP, and the audit log.
They do not change ERP orders. Shopee is called only after those checks pass.
"""
from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import agen_guardrail
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee

# (method, path). Reads never change Shopee. Writes stay behind the guard.
BACA = {
    "voucher_daftar": ("GET", "/api/v2/voucher/get_voucher_list"),
    "voucher_detail": ("GET", "/api/v2/voucher/get_voucher"),
    "flash_daftar": ("GET", "/api/v2/shop_flash_sale/get_shop_flash_sale_list"),
    "flash_detail": ("GET", "/api/v2/shop_flash_sale/get_shop_flash_sale"),
    "flash_slot": ("GET", "/api/v2/shop_flash_sale/get_time_slot_id"),
    "flash_barang": ("GET", "/api/v2/shop_flash_sale/get_shop_flash_sale_items"),
    "hadiah_daftar": ("GET", "/api/v2/follow_prize/get_follow_prize_list"),
    "hadiah_detail": ("GET", "/api/v2/follow_prize/get_follow_prize_detail"),
    "video_hasil": ("GET", "/api/v2/media_space/get_video_upload_result"),
    "listing_bermasalah": ("GET", "/api/v2/account_health/get_listings_with_issues"),
}
TULIS = {
    "voucher_buat": ("POST", "/api/v2/voucher/add_voucher"),
    "voucher_ubah": ("POST", "/api/v2/voucher/update_voucher"),
    "voucher_akhiri": ("POST", "/api/v2/voucher/end_voucher"),
    "voucher_hapus": ("POST", "/api/v2/voucher/delete_voucher"),
    "flash_buat": ("POST", "/api/v2/shop_flash_sale/create_shop_flash_sale"),
    "flash_ubah": ("POST", "/api/v2/shop_flash_sale/update_shop_flash_sale"),
    "flash_hapus": ("POST", "/api/v2/shop_flash_sale/delete_shop_flash_sale"),
    "flash_tambah_barang": ("POST", "/api/v2/shop_flash_sale/add_shop_flash_sale_items"),
    "flash_ubah_barang": ("POST", "/api/v2/shop_flash_sale/update_shop_flash_sale_items"),
    "flash_hapus_barang": ("POST", "/api/v2/shop_flash_sale/delete_shop_flash_sale_items"),
    "hadiah_buat": ("POST", "/api/v2/follow_prize/add_follow_prize"),
    "hadiah_ubah": ("POST", "/api/v2/follow_prize/update_follow_prize"),
    "hadiah_akhiri": ("POST", "/api/v2/follow_prize/end_follow_prize"),
    "hadiah_hapus": ("POST", "/api/v2/follow_prize/delete_follow_prize"),
    "video_mulai": ("POST", "/api/v2/media_space/init_video_upload"),
    "video_selesai": ("POST", "/api/v2/media_space/complete_video_upload"),
}
MAKS = 30


class TulisIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aksi: str
    payload: dict = Field(default_factory=dict)
    alasan: str = Field("", max_length=500)
    kering: bool = True
    persetujuan: str | None = None
    request_id: str | None = Field(None, max_length=64)


def daftar_aksi() -> dict:
    return {
        "baca": [{"aksi": k, "method": v[0], "path": v[1]} for k, v in BACA.items()],
        "tulis": [{"aksi": k, "method": v[0], "path": v[1]} for k, v in TULIS.items()],
    }


def ringkas(data: dict) -> dict:
    resp = data.get("response") if isinstance(data, dict) else None
    dipotong = False
    if isinstance(resp, list) and len(resp) > MAKS:
        resp, dipotong = resp[:MAKS], True
    elif isinstance(resp, dict):
        keluar = {}
        for kunci, nilai in list(resp.items())[:12]:
            if isinstance(nilai, list) and len(nilai) > MAKS:
                keluar[kunci] = nilai[:MAKS]
                dipotong = True
            elif isinstance(nilai, (dict, list)):
                keluar[kunci] = nilai if not isinstance(nilai, dict) else {k: nilai[k] for k in list(nilai)[:12]}
            else:
                keluar[kunci] = nilai
        resp = keluar
    return {"request_id": data.get("request_id") if isinstance(data, dict) else None, "response": resp, "dipotong": dipotong}


def _payload(data: dict) -> dict:
    if len(data) > 40:
        raise HTTPException(status_code=422, detail="Body pemasaran terlalu besar")
    return data


async def baca(session, akun, aksi: str, params: dict | None = None) -> dict:
    if getattr(akun, "platform", None) != "shopee":
        raise HTTPException(status_code=409, detail="API ini khusus toko Shopee.")
    if aksi not in BACA:
        raise HTTPException(status_code=404, detail="Bacaan pemasaran tidak dikenal")
    _, path = BACA[aksi]
    data = await erp_shopee.signed_shop_request(session, akun, path, method="GET", params=params or None, read_only=True)
    return {"aksi": aksi, **ringkas(data if isinstance(data, dict) else {})}


async def tulis(session: AsyncSession, akun, body: TulisIn, pengguna) -> dict:
    if getattr(akun, "platform", None) != "shopee":
        raise HTTPException(status_code=409, detail="API ini khusus toko Shopee.")
    if body.aksi not in TULIS:
        raise HTTPException(status_code=422, detail="Aksi tulis pemasaran tidak dikenal")
    ada = await agen_guardrail._sudah(session, akun.id, body.request_id)
    if ada is not None:
        ulang = agen_guardrail._muat(ada.sesudah_json)
        ulang["audit_id"] = ada.id
        ulang["diulang"] = True
        return ulang
    alasan = await agen_guardrail.tulisan_diblokir(session, akun.id)
    if alasan:
        await agen_guardrail._catat(
            session, akun_id=akun.id, aksi=body.aksi, pengguna=pengguna, sebelum={}, sesudah={"ditolak": alasan},
            alasan=body.alasan, request_id=body.request_id, persetujuan=body.persetujuan, kering=body.kering, hasil="ditolak",
        )
        await session.commit()
        raise HTTPException(status_code=409, detail=alasan)
    payload = _payload(body.payload)
    jawaban = {"aksi": body.aksi, "kering": body.kering, "diterapkan": False, "payload": payload}
    if body.kering or body.persetujuan != "ya":
        if not body.kering and body.persetujuan != "ya":
            await agen_guardrail._catat(
                session, akun_id=akun.id, aksi=body.aksi, pengguna=pengguna, sebelum={}, sesudah=jawaban,
                alasan=body.alasan, request_id=body.request_id, persetujuan=body.persetujuan, kering=False, hasil="ditolak",
            )
            await session.commit()
            raise HTTPException(status_code=409, detail="Butuh persetujuan pemilik (ya)")
        row = await agen_guardrail._catat(
            session, akun_id=akun.id, aksi=body.aksi, pengguna=pengguna, sebelum={}, sesudah=jawaban,
            alasan=body.alasan, request_id=body.request_id, persetujuan=body.persetujuan, kering=True, hasil="kering",
        )
        await session.commit()
        jawaban["audit_id"] = row.id
        jawaban["butuh_persetujuan"] = body.persetujuan != "ya"
        return jawaban
    _, path = TULIS[body.aksi]
    try:
        data = await erp_shopee.signed_shop_request(session, akun, path, method="POST", body=payload)
    except HTTPException as exc:
        await agen_guardrail._catat(
            session, akun_id=akun.id, aksi=body.aksi, pengguna=pengguna, sebelum={}, sesudah={"ditolak": str(exc.detail)},
            alasan=body.alasan, request_id=body.request_id, persetujuan=body.persetujuan, kering=False, hasil="gagal",
        )
        await session.commit()
        raise
    jawaban["diterapkan"] = True
    jawaban["kering"] = False
    jawaban["shopee"] = ringkas(data if isinstance(data, dict) else {})
    row = await agen_guardrail._catat(
        session, akun_id=akun.id, aksi=body.aksi, pengguna=pengguna, sebelum={}, sesudah=jawaban,
        alasan=body.alasan, request_id=body.request_id, persetujuan=body.persetujuan, kering=False, hasil="diterapkan",
    )
    await session.commit()
    jawaban["audit_id"] = row.id
    return jawaban
