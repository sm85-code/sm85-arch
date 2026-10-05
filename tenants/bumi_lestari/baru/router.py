"""Tenant Bumi Lestari baru. Tidak memakai rute /order/{id} lama."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.auth import require_roles_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import get_db_bumi_lestari
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser

router = APIRouter(prefix="/baru")


def _db():
    return Depends(get_db_bumi_lestari)


def _guard():
    return Depends(require_roles_bumi_lestari("owner", "admin"))


async def _siap(session: AsyncSession) -> None:
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS bl2_jenis (
            id TEXT PRIMARY KEY,
            nama TEXT NOT NULL,
            kayu BOOLEAN NOT NULL DEFAULT TRUE,
            ukuran TEXT NOT NULL DEFAULT ''
        )
    """))
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS bl2_order (
            id TEXT PRIMARY KEY,
            no_order TEXT NOT NULL DEFAULT '',
            nama_barang TEXT NOT NULL DEFAULT '',
            pembeli TEXT NOT NULL DEFAULT '',
            sumber TEXT NOT NULL DEFAULT 'manual',
            jenis_id TEXT,
            status TEXT NOT NULL DEFAULT 'dipesan'
        )
    """))
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS bl2_peta (
            nama TEXT PRIMARY KEY,
            jenis_id TEXT NOT NULL
        )
    """))
    await session.commit()


@router.get("/order")
async def daftar_order(session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    baris = (await session.execute(text("""
        SELECT o.id, o.no_order, o.nama_barang, o.pembeli, o.sumber, o.status, j.nama AS jenis, j.kayu
        FROM bl2_order o LEFT JOIN bl2_jenis j ON j.id = o.jenis_id
        ORDER BY o.no_order DESC
    """))).mappings().all()
    return [dict(r) for r in baris]


@router.post("/order")
async def tambah_order(payload: dict, session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    import uuid
    nama = str(payload.get("nama_barang") or "").strip()
    jenis = (await session.execute(text("SELECT jenis_id FROM bl2_peta WHERE nama = :n"), {"n": nama})).scalar()
    oid = uuid.uuid4().hex
    await session.execute(text("""
        INSERT INTO bl2_order (id, no_order, nama_barang, pembeli, sumber, jenis_id)
        VALUES (:id, :no, :nama, :pembeli, :sumber, :jenis)
    """), {
        "id": oid,
        "no": str(payload.get("no_order") or ""),
        "nama": nama,
        "pembeli": str(payload.get("pembeli") or ""),
        "sumber": str(payload.get("sumber") or "manual"),
        "jenis": jenis,
    })
    await session.commit()
    return {"id": oid}


@router.get("/jenis")
async def jenis(session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    baris = (await session.execute(text("SELECT id, nama, kayu, ukuran FROM bl2_jenis ORDER BY nama"))).mappings().all()
    return [dict(r) for r in baris]


@router.post("/jenis")
async def tambah_jenis(payload: dict, session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    import uuid
    jid = uuid.uuid4().hex
    await session.execute(text("INSERT INTO bl2_jenis (id, nama, kayu, ukuran) VALUES (:id, :nama, :kayu, :ukuran)"), {
        "id": jid,
        "nama": str(payload.get("nama") or "").strip(),
        "kayu": bool(payload.get("kayu", True)),
        "ukuran": str(payload.get("ukuran") or ""),
    })
    await session.commit()
    return {"id": jid}


@router.get("/belum-peta")
async def belum(session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    baris = (await session.execute(text("""
        SELECT nama_barang AS nama, COUNT(*) AS jumlah FROM bl2_order
        WHERE jenis_id IS NULL AND nama_barang <> ''
        GROUP BY nama_barang ORDER BY nama_barang
    """))).mappings().all()
    return [dict(r) for r in baris]


@router.post("/peta")
async def peta(payload: dict, session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    nama = str(payload.get("nama") or "").strip()
    jenis_id = str(payload.get("jenis_id") or "")
    if not nama or not jenis_id:
        raise HTTPException(422, "Nama dan jenis wajib")
    await session.execute(text("""
        INSERT INTO bl2_peta (nama, jenis_id) VALUES (:nama, :jenis)
        ON CONFLICT (nama) DO UPDATE SET jenis_id = EXCLUDED.jenis_id
    """), {"nama": nama, "jenis": jenis_id})
    await session.execute(text("UPDATE bl2_order SET jenis_id = :jenis WHERE nama_barang = :nama"), {"nama": nama, "jenis": jenis_id})
    await session.commit()
    return {"nama": nama}


@router.get("/produksi")
async def produksi(session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    kayu = (await session.execute(text("""
        SELECT COUNT(*) FROM bl2_order o JOIN bl2_jenis j ON j.id = o.jenis_id WHERE j.kayu
    """))).scalar()
    non = (await session.execute(text("""
        SELECT COUNT(*) FROM bl2_order o JOIN bl2_jenis j ON j.id = o.jenis_id WHERE NOT j.kayu
    """))).scalar()
    return {"kayu": kayu or 0, "non_kayu": non or 0}
