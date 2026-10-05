"""Tiga kasus uji. Tidak mengirim apa pun ke Shopee."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import require_roles_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser

uji = APIRouter(prefix="/baru")


def _db():
    return Depends(get_db_bumi_lestari)


def _guard():
    return Depends(require_roles_bumi_lestari("owner", "admin"))


KASUS = [
    {"id": "uji-shopee", "no": "UJI-SHOPEE", "nama": "Partisi Rak Palang 80x20x200", "pembeli": "Pembeli Shopee", "sumber": "shopee", "status": "selesai", "tukang": "Ahmad Nur Alim", "pendapatan": 500000, "barang": 375000, "cat": 100000, "packing": 0, "admin": 25000, "proses": 0},
    {"id": "uji-mandala", "no": "UJI-MANDALA", "nama": "Partisi Rak Palang 80x20x200", "pembeli": "Mandala Wangi", "sumber": "reseller", "status": "selesai", "tukang": "Ai Hendarso", "pendapatan": 485000, "barang": 375000, "cat": 100000, "packing": 0, "admin": 0, "proses": 10000},
    {"id": "uji-web", "no": "UJI-WEB", "nama": "Batu gamping 500 gram", "pembeli": "Pembeli toko web", "sumber": "web", "status": "selesai", "tukang": "", "pendapatan": 150000, "barang": 90000, "cat": 0, "packing": 5000, "admin": 0, "proses": 0},
]


async def _kolom(session: AsyncSession) -> None:
    for kolom in ("harga_barang", "harga_cat", "packing_kayu", "pendapatan", "biaya_admin", "biaya_proses"):
        await session.execute(text(f"ALTER TABLE bl2_order ADD COLUMN IF NOT EXISTS {kolom} NUMERIC NOT NULL DEFAULT 0"))
    await session.execute(text("ALTER TABLE bl2_order ADD COLUMN IF NOT EXISTS tukang TEXT NOT NULL DEFAULT ''"))


@uji.post("/uji")
async def isi_uji(session: AsyncSession = _db(), _: BlUser = _guard()):
    await _kolom(session)
    for r in KASUS:
        await session.execute(text("""
            INSERT INTO bl2_order (id, no_order, nama_barang, pembeli, sumber, status, tukang, sumber_ref, harga_barang, harga_cat, packing_kayu, pendapatan, biaya_admin, biaya_proses)
            VALUES (:id, :no, :nama, :pembeli, :sumber, :status, :tukang, :id, :barang, :cat, :packing, :pendapatan, :admin, :proses)
            ON CONFLICT (id) DO UPDATE SET nama_barang = EXCLUDED.nama_barang, pendapatan = EXCLUDED.pendapatan,
              harga_barang = EXCLUDED.harga_barang, harga_cat = EXCLUDED.harga_cat, packing_kayu = EXCLUDED.packing_kayu,
              biaya_admin = EXCLUDED.biaya_admin, biaya_proses = EXCLUDED.biaya_proses, status = EXCLUDED.status
        """), r)
    await session.commit()
    return {"kasus": 3}


@uji.get("/laporan")
async def laporan(session: AsyncSession = _db(), _: BlUser = _guard()):
    await isi_uji(session)
    await _kolom(session)
    baris = (await session.execute(text("""
        SELECT id, no_order, nama_barang, pembeli, sumber, pendapatan::float AS pendapatan,
               harga_barang::float AS barang, harga_cat::float AS cat, packing_kayu::float AS packing,
               biaya_admin::float AS admin, biaya_proses::float AS proses
        FROM bl2_order WHERE id LIKE 'uji-%' ORDER BY id
    """))).mappings().all()
    hasil = []
    for r in baris:
        biaya = r["barang"] + r["cat"] + r["packing"] + r["admin"]
        hasil.append({**dict(r), "laba_kotor": r["pendapatan"] - biaya})
    return {
        "periode": "Senin–Sabtu, cut-off Selasa",
        "kasus": hasil,
        "pendapatan": sum(x["pendapatan"] for x in hasil),
        "laba_kotor": sum(x["laba_kotor"] for x in hasil),
        "catatan": "Operasional dan gaji belum dipotong. Bagi hasil setelah gaji minggu keempat.",
    }


@uji.delete("/uji")
async def hapus_uji(session: AsyncSession = _db(), _: BlUser = _guard()):
    await session.execute(text("DELETE FROM bl2_order WHERE id LIKE 'uji-%'"))
    await session.commit()
    return {"ok": True}


@uji.delete("/order/{order_id}")
async def hapus_order(order_id: str, session: AsyncSession = _db(), _: BlUser = _guard()):
    await session.execute(text("DELETE FROM bl2_order WHERE id = :id"), {"id": order_id})
    await session.commit()
    return {"ok": True}
