"""Keep catalogue, master reference and Store quantities independent by default."""
from fastapi import HTTPException
from ..infrastructure.models import PengaturanStok


async def get_settings(session):
    setting = await session.get(PengaturanStok, "global")
    enabled = bool(setting and setting.gudang_aktif)
    return {"mode": "gudang_erp" if enabled else "per_toko", "gudang_aktif": enabled,
            "stok_master": "referensi", "stok_store": "mandiri"}


async def require_warehouse(session):
    if not (await get_settings(session))["gudang_aktif"]:
        raise HTTPException(409, "Gudang ERP belum diaktifkan. Stok marketplace dikelola per toko; stok master hanya referensi.")
