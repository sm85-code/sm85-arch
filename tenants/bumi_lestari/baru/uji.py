"""Data percobaan dan ubah/hapus order. Tidak menyentuh katalog impor."""
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


@uji.post("/uji")
async def isi_uji(session: AsyncSession = _db(), _: BlUser = _guard()):
    await session.execute(text("ALTER TABLE bl2_order ADD COLUMN IF NOT EXISTS harga_barang NUMERIC NOT NULL DEFAULT 0"))
    await session.execute(text("ALTER TABLE bl2_order ADD COLUMN IF NOT EXISTS harga_cat NUMERIC NOT NULL DEFAULT 0"))
    await session.execute(text("ALTER TABLE bl2_order ADD COLUMN IF NOT EXISTS packing_kayu NUMERIC NOT NULL DEFAULT 0"))
    await session.execute(text("ALTER TABLE bl2_order ADD COLUMN IF NOT EXISTS tukang TEXT NOT NULL DEFAULT ''"))
    contoh = [
        ("uji-1", "UJI-001", "Partisi Rak Palang 80x20x200", "Mandala Wangi", "reseller", "selesai", 375000, 100000, 0, "Ahmad Nur Alim"),
        ("uji-2", "UJI-002", "Partisi Rak Tengah 80x20x200", "Chakra Digital Niaga", "reseller", "dicat", 450000, 160000, 0, "Ai Hendarso"),
        ("uji-3", "UJI-003", "Kandang Kelinci 120x50x90", "Shopee", "erp", "dipesan", 800000, 0, 0, "Cahyono"),
        ("uji-4", "UJI-004", "Atap daun rumbia", "Shopee", "erp", "dipesan", 0, 0, 0, ""),
    ]
    for row in contoh:
        await session.execute(text("""
            INSERT INTO bl2_order (id, no_order, nama_barang, pembeli, sumber, status, harga_barang, harga_cat, packing_kayu, tukang, sumber_ref)
            VALUES (:id, :no, :nama, :pembeli, :sumber, :status, :barang, :cat, :packing, :tukang, :id)
            ON CONFLICT (id) DO UPDATE SET
              nama_barang = EXCLUDED.nama_barang, pembeli = EXCLUDED.pembeli, status = EXCLUDED.status,
              harga_barang = EXCLUDED.harga_barang, harga_cat = EXCLUDED.harga_cat,
              packing_kayu = EXCLUDED.packing_kayu, tukang = EXCLUDED.tukang
        """), {
            "id": row[0], "no": row[1], "nama": row[2], "pembeli": row[3], "sumber": row[4],
            "status": row[5], "barang": row[6], "cat": row[7], "packing": row[8], "tukang": row[9],
        })
    await session.commit()
    return {"order": len(contoh)}


@uji.delete("/uji")
async def hapus_uji(session: AsyncSession = _db(), _: BlUser = _guard()):
    await session.execute(text("DELETE FROM bl2_order WHERE id LIKE 'uji-%' OR sumber_ref LIKE 'uji-%'"))
    await session.commit()
    return {"ok": True}


@uji.patch("/order/{order_id}")
async def ubah_order(order_id: str, payload: dict, session: AsyncSession = _db(), _: BlUser = _guard()):
    await session.execute(text("""
        UPDATE bl2_order SET
          nama_barang = COALESCE(:nama, nama_barang),
          status = COALESCE(:status, status),
          pembeli = COALESCE(:pembeli, pembeli),
          harga_barang = COALESCE(:barang, harga_barang),
          harga_cat = COALESCE(:cat, harga_cat),
          packing_kayu = COALESCE(:packing, packing_kayu),
          tukang = COALESCE(:tukang, tukang)
        WHERE id = :id
    """), {
        "id": order_id,
        "nama": payload.get("nama_barang"),
        "status": payload.get("status"),
        "pembeli": payload.get("pembeli"),
        "barang": payload.get("harga_barang"),
        "cat": payload.get("harga_cat"),
        "packing": payload.get("packing_kayu"),
        "tukang": payload.get("tukang"),
    })
    await session.commit()
    return {"ok": True}


@uji.delete("/order/{order_id}")
async def hapus_order(order_id: str, session: AsyncSession = _db(), _: BlUser = _guard()):
    await session.execute(text("DELETE FROM bl2_order WHERE id = :id"), {"id": order_id})
    await session.commit()
    return {"ok": True}


@uji.delete("/peta")
async def hapus_peta(nama: str, session: AsyncSession = _db(), _: BlUser = _guard()):
    await session.execute(text("DELETE FROM bl2_peta WHERE nama = :nama"), {"nama": nama})
    await session.execute(text("UPDATE bl2_order SET jenis_id = NULL WHERE nama_barang = :nama"), {"nama": nama})
    await session.commit()
    return {"ok": True}
