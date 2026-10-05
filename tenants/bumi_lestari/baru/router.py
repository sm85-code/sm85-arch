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
            ukuran TEXT NOT NULL DEFAULT '',
            harga_reseller NUMERIC NOT NULL DEFAULT 0
        )
    """))
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS bl2_order (
            id TEXT PRIMARY KEY,
            no_order TEXT NOT NULL DEFAULT '',
            nama_barang TEXT NOT NULL DEFAULT '',
            pembeli TEXT NOT NULL DEFAULT '',
            toko TEXT NOT NULL DEFAULT '',
            sumber TEXT NOT NULL DEFAULT 'manual',
            sumber_ref TEXT,
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
    await session.execute(text("ALTER TABLE bl2_order ADD COLUMN IF NOT EXISTS toko TEXT NOT NULL DEFAULT ''"))
    await session.execute(text("ALTER TABLE bl2_order ADD COLUMN IF NOT EXISTS sumber_ref TEXT"))
    await session.execute(text("ALTER TABLE bl2_jenis ADD COLUMN IF NOT EXISTS harga_reseller NUMERIC NOT NULL DEFAULT 0"))
    await session.execute(text("ALTER TABLE bl2_jenis ADD COLUMN IF NOT EXISTS produk_id TEXT"))
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS bl2_produk (
            id TEXT PRIMARY KEY,
            nama TEXT NOT NULL,
            kayu BOOLEAN NOT NULL DEFAULT TRUE
        )
    """))
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS bl2_varian (
            id TEXT PRIMARY KEY,
            produk_id TEXT NOT NULL,
            nama TEXT NOT NULL
        )
    """))
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS bl2_nilai (
            id TEXT PRIMARY KEY,
            varian_id TEXT NOT NULL,
            nilai TEXT NOT NULL,
            jenis_id TEXT
        )
    """))
    await session.commit()


@router.get("/order")
async def daftar_order(session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    baris = (await session.execute(text("""
        SELECT o.id, o.no_order, o.nama_barang, o.pembeli, o.toko, o.sumber, o.status,
               j.nama AS jenis, j.kayu
        FROM bl2_order o LEFT JOIN bl2_jenis j ON j.id = o.jenis_id
        ORDER BY o.no_order DESC
    """))).mappings().all()
    return [{**dict(r), "kayu": bool(r["kayu"]) if r["kayu"] is not None else None} for r in baris]


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
    baris = (await session.execute(text("SELECT id, nama, kayu, ukuran, harga_reseller::float AS harga_reseller FROM bl2_jenis ORDER BY nama"))).mappings().all()
    return [dict(r) for r in baris]


@router.post("/jenis")
async def tambah_jenis(payload: dict, session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    import uuid
    jid = uuid.uuid4().hex
    await session.execute(text("INSERT INTO bl2_jenis (id, nama, kayu, ukuran, harga_reseller) VALUES (:id, :nama, :kayu, :ukuran, :harga)"), {
        "id": jid,
        "nama": str(payload.get("nama") or "").strip(),
        "kayu": bool(payload.get("kayu", True)),
        "ukuran": str(payload.get("ukuran") or ""),
        "harga": payload.get("harga_reseller") or 0,
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


TOKO = (
    "MAJAPAHIT STORE IND",
    "BUMI TANI IND",
    "BUMI LESTARI INDONESIA",
    "RESTU BUMI IND",
    "CAHAYA LANGIT IND",
    "AZFA Furniture Official",
)


@router.post("/tarik")
async def tarik(hari: int = 30, session: AsyncSession = _db(), _: BlUser = _guard()):
    """Tarik order ERP untuk toko yang masuk Bumi Lestari. Tidak mengirim status ke Shopee."""
    await _siap(session)
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import SessionLocal
    if SessionLocal is None:
        raise HTTPException(503, "Database ERP belum tersambung")
    async with SessionLocal() as erp:
        baris = (await erp.execute(text("""
            SELECT p.id_eksternal, i.id AS item_id, i.nama_produk, p.nama_pembeli, a.nama_toko
            FROM mpe_pesanan p
            JOIN mpe_akun_marketplace a ON a.id = p.akun_id
            LEFT JOIN mpe_item_pesanan i ON i.pesanan_id = p.id
            WHERE a.nama_toko = ANY(:toko)
              AND COALESCE(p.dipesan_at, p.created_at) >= NOW() - (:hari || ' days')::interval
        """), {"toko": list(TOKO), "hari": str(hari)})).mappings().all()
    import uuid
    baru = 0
    for r in baris:
        ref = f"erp:{r['item_id'] or r['id_eksternal']}"
        ada = (await session.execute(text("SELECT id FROM bl2_order WHERE sumber_ref = :ref"), {"ref": ref})).scalar()
        if ada:
            continue
        nama = (r["nama_produk"] or "").strip()
        jenis = (await session.execute(text("SELECT jenis_id FROM bl2_peta WHERE nama = :n"), {"n": nama})).scalar()
        await session.execute(text("""
            INSERT INTO bl2_order (id, no_order, nama_barang, pembeli, toko, sumber, sumber_ref, jenis_id)
            VALUES (:id, :no, :nama, :pembeli, :toko, 'erp', :ref, :jenis)
        """), {
            "id": uuid.uuid4().hex, "no": r["id_eksternal"] or "", "nama": nama,
            "pembeli": r["nama_pembeli"] or "", "toko": r["nama_toko"] or "", "ref": ref, "jenis": jenis,
        })
        baru += 1
    await session.commit()
    return {"order": baru, "toko": list(TOKO)}


@router.post("/status")
async def ubah_status(payload: dict, session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    await session.execute(text("UPDATE bl2_order SET status = :status WHERE id = :id"), {
        "status": str(payload.get("status") or "dipesan"),
        "id": str(payload.get("id") or ""),
    })
    await session.commit()
    return {"ok": True}


@router.get("/produk")
async def daftar_produk(session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    produk = (await session.execute(text("SELECT id, nama, kayu FROM bl2_produk ORDER BY nama"))).mappings().all()
    varian = (await session.execute(text("SELECT id, produk_id, nama FROM bl2_varian ORDER BY nama"))).mappings().all()
    nilai = (await session.execute(text("SELECT id, varian_id, nilai, jenis_id FROM bl2_nilai ORDER BY nilai"))).mappings().all()
    hasil = []
    for p in produk:
        sumbu = []
        for v in varian:
            if v["produk_id"] != p["id"]:
                continue
            sumbu.append({**dict(v), "nilai": [dict(n) for n in nilai if n["varian_id"] == v["id"]]})
        hasil.append({**dict(p), "kayu": bool(p["kayu"]), "varian": sumbu})
    return hasil


@router.post("/produk")
async def tambah_produk(payload: dict, session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    import uuid
    pid = uuid.uuid4().hex
    await session.execute(text("INSERT INTO bl2_produk (id, nama, kayu) VALUES (:id, :nama, :kayu)"), {
        "id": pid, "nama": str(payload.get("nama") or "").strip(), "kayu": bool(payload.get("kayu", True)),
    })
    await session.commit()
    return {"id": pid}


@router.post("/varian")
async def tambah_varian(payload: dict, session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    import uuid
    vid = uuid.uuid4().hex
    await session.execute(text("INSERT INTO bl2_varian (id, produk_id, nama) VALUES (:id, :produk, :nama)"), {
        "id": vid, "produk": str(payload.get("produk_id") or ""), "nama": str(payload.get("nama") or "").strip(),
    })
    await session.commit()
    return {"id": vid}


@router.post("/nilai")
async def tambah_nilai(payload: dict, session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    import uuid
    vid = str(payload.get("varian_id") or "")
    nilai = str(payload.get("nilai") or "").strip()
    induk = (await session.execute(text("""
        SELECT p.id, p.nama, p.kayu, v.nama AS varian
        FROM bl2_varian v JOIN bl2_produk p ON p.id = v.produk_id WHERE v.id = :id
    """), {"id": vid})).mappings().first()
    if not induk or not nilai:
        raise HTTPException(422, "Varian dan nilai wajib")
    jid = uuid.uuid4().hex
    await session.execute(text("""
        INSERT INTO bl2_jenis (id, nama, kayu, ukuran, harga_reseller, produk_id)
        VALUES (:id, :nama, :kayu, :ukuran, :harga, :produk)
    """), {
        "id": jid,
        "nama": f"{induk['nama']} · {induk['varian']} · {nilai}",
        "kayu": induk["kayu"],
        "ukuran": nilai,
        "harga": payload.get("harga_reseller") or 0,
        "produk": induk["id"],
    })
    await session.execute(text("INSERT INTO bl2_nilai (id, varian_id, nilai, jenis_id) VALUES (:id, :varian, :nilai, :jenis)"), {
        "id": uuid.uuid4().hex, "varian": vid, "nilai": nilai, "jenis": jid,
    })
    await session.commit()
    return {"id": jid}
