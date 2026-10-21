"""Manual orders for production: typed in the Pesanan menu, read by Pemetaan Order like an order from a marketplace.

The order of an own shop or a reseller (a row of Toko Internal & Reseller) is kept as a Pesanan of platform "manual", with
no marketplace account, no stock reservation and no sales figures. What production needs (kind, seller code, who ordered,
dates, reference, a note per item) lives in the order's detail. A reseller order code is formed when its items are mapped
(SKU, ship-by date, seller code and a running number), so until then it shows "Menunggu SKU".
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    PLATFORM_MANUAL,
    ItemPesanan,
    Pesanan,
    TokoPenjualan,
)


class ItemManual(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    produk: str = Field(min_length=1, max_length=255)
    varian: str = Field(default="", max_length=255)
    qty: int = Field(ge=1, le=100000)
    note: str = Field(default="", max_length=2000)


class PesananManualIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    toko_id: str = Field(min_length=1, max_length=64)
    kode_pesanan: str | None = Field(default=None, max_length=200)  # own shop: typed; reseller: formed when mapped
    pemesan: str = Field(default="", max_length=200)
    ordered_at: datetime
    deadline: datetime
    reference: str = Field(default="", max_length=200)
    items: list[ItemManual] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def tanggal(self):
        if self.ordered_at.tzinfo is None or self.deadline.tzinfo is None:
            raise ValueError("Tanggal harus memiliki zona waktu")
        if self.deadline < self.ordered_at:
            raise ValueError("Batas kirim tidak boleh sebelum tanggal pesanan")
        return self


async def buat(session: AsyncSession, payload: PesananManualIn) -> Pesanan:
    toko = await session.get(TokoPenjualan, payload.toko_id)
    if toko is None or not toko.aktif:
        raise HTTPException(422, "Pilih toko internal atau reseller yang aktif")
    reseller = toko.jenis == "reseller"
    if reseller:
        if not toko.kode:
            raise HTTPException(422, "Toko reseller ini belum punya kode penjual; isi di Kelola Toko")
        id_eksternal = "RSL-" + datetime.now(timezone.utc).strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:8].upper()
    else:
        id_eksternal = (payload.kode_pesanan or "").strip()
        if not id_eksternal:
            raise HTTPException(422, "Isi kode pesanan internal")
        ada = await session.scalar(select(Pesanan.id).where(Pesanan.platform == PLATFORM_MANUAL, Pesanan.id_eksternal == id_eksternal))
        if ada:
            raise HTTPException(409, "Kode pesanan ini sudah dipakai")
    pesanan = Pesanan(
        platform=PLATFORM_MANUAL,
        id_eksternal=id_eksternal,
        akun_id=None,
        status="to_ship",
        nama_pembeli=payload.pemesan or toko.nama,
        total=0,
        dipesan_at=payload.ordered_at,
        detail_json="{}",
    )
    session.add(pesanan)
    await session.flush()
    catatan: dict[str, str] = {}
    for item in payload.items:
        baris = ItemPesanan(
            pesanan_id=pesanan.id, nama_produk=item.produk, model_name=item.varian, qty=item.qty, harga_satuan=0, subtotal=0
        )
        session.add(baris)
        await session.flush()
        if item.note:
            catatan[baris.id] = item.note
    pesanan.detail_json = json.dumps(
        {
            "ship_by_date": int(payload.deadline.timestamp()),
            "manual": {
                "jenis": toko.jenis,
                "toko_id": toko.id,
                "toko_nama": toko.nama,
                "platform": toko.platform,
                "seller_code": toko.kode or "" if reseller else "",
                "reference": payload.reference,
                "catatan_item": catatan,
            },
        }
    )
    await session.flush()
    return pesanan
