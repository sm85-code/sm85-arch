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
    await session.execute(text("ALTER TABLE bl2_order ADD COLUMN IF NOT EXISTS tgl_pesan DATE"))
    await session.execute(text("ALTER TABLE bl2_order ADD COLUMN IF NOT EXISTS varian TEXT NOT NULL DEFAULT ''"))
    await session.execute(text("ALTER TABLE bl2_order ADD COLUMN IF NOT EXISTS qty INTEGER NOT NULL DEFAULT 1"))
    await session.execute(text("ALTER TABLE bl2_order ADD COLUMN IF NOT EXISTS keterangan TEXT NOT NULL DEFAULT ''"))
    await session.execute(text("ALTER TABLE bl2_order ADD COLUMN IF NOT EXISTS jenis_pesanan TEXT NOT NULL DEFAULT ''"))
    await session.execute(text("ALTER TABLE bl2_jenis ADD COLUMN IF NOT EXISTS harga_reseller NUMERIC NOT NULL DEFAULT 0"))
    await session.execute(text("ALTER TABLE bl2_jenis ADD COLUMN IF NOT EXISTS produk_id TEXT"))
    await session.execute(text("ALTER TABLE bl2_nilai ADD COLUMN IF NOT EXISTS harga_tukang NUMERIC NOT NULL DEFAULT 0"))
    await session.execute(text("ALTER TABLE bl2_nilai ADD COLUMN IF NOT EXISTS custom BOOLEAN NOT NULL DEFAULT FALSE"))
    await session.execute(text("ALTER TABLE bl2_jenis ADD COLUMN IF NOT EXISTS harga_tukang NUMERIC NOT NULL DEFAULT 0"))
    await session.execute(text("ALTER TABLE bl2_jenis ADD COLUMN IF NOT EXISTS custom BOOLEAN NOT NULL DEFAULT FALSE"))
    await session.execute(text("ALTER TABLE bl2_nilai ADD COLUMN IF NOT EXISTS harga_cat NUMERIC NOT NULL DEFAULT 0"))
    await session.execute(text("ALTER TABLE bl2_jenis ADD COLUMN IF NOT EXISTS harga_cat NUMERIC NOT NULL DEFAULT 0"))
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
            jenis_id TEXT,
            harga_tukang NUMERIC NOT NULL DEFAULT 0,
            custom BOOLEAN NOT NULL DEFAULT FALSE
        )
    """))
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS bl2_reseller (
            kode TEXT PRIMARY KEY,
            nama TEXT NOT NULL DEFAULT ''
        )
    """))
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS bl2_tukang (
            id TEXT PRIMARY KEY,
            nama TEXT NOT NULL DEFAULT ''
        )
    """))
    await session.execute(text("""
        INSERT INTO bl2_reseller (kode, nama) VALUES
        ('001', 'Mandala Wangi'), ('002', 'Chakra Digital Niaga'),
        ('003', 'Karya Raharja Store'), ('004', 'AZFA Digital Indonesia')
        ON CONFLICT (kode) DO UPDATE SET nama = EXCLUDED.nama WHERE bl2_reseller.nama = ''
    """))
    await session.execute(text("""
        INSERT INTO bl2_tukang (id, nama) VALUES
        ('t1', 'Ahmad Nur Alim'), ('t2', 'Ai Hendarso'), ('t3', 'Cahyono'),
        ('t4', 'Joko Wahyono'), ('t5', 'Suryaman')
        ON CONFLICT (id) DO UPDATE SET nama = EXCLUDED.nama WHERE bl2_tukang.nama = ''
    """))
    await session.commit()


@router.get("/order")
async def daftar_order(session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    baris = (await session.execute(text("""
        SELECT o.id, o.no_order, o.nama_barang, o.varian, o.qty, o.keterangan, o.pembeli, o.toko, o.sumber,
               o.tgl_pesan::text AS tgl_pesan, o.status, o.jenis_pesanan, j.nama AS jenis, j.kayu,
               CASE WHEN o.jenis_id IS NULL THEN 'Belum' ELSE 'Sudah' END AS status_peta
        FROM bl2_order o LEFT JOIN bl2_jenis j ON j.id = o.jenis_id
        ORDER BY o.tgl_pesan DESC NULLS LAST, o.no_order DESC
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
        INSERT INTO bl2_order (id, no_order, nama_barang, pembeli, toko, sumber, jenis_id, tgl_pesan, varian, qty, keterangan)
        VALUES (:id, :no, :nama, :pembeli, :toko, :sumber, :jenis, :tgl, :varian, :qty, :ket)
    """), {
        "id": oid,
        "no": str(payload.get("no_order") or ""),
        "nama": nama,
        "pembeli": str(payload.get("pembeli") or ""),
        "toko": str(payload.get("toko") or ""),
        "sumber": str(payload.get("sumber") or "manual"),
        "jenis": jenis,
        "tgl": payload.get("tgl_pesan") or None,
        "varian": str(payload.get("varian") or ""),
        "qty": int(payload.get("qty") or 1),
        "ket": str(payload.get("keterangan") or ""),
    })
    await session.commit()
    return {"id": oid}


@router.post("/order/impor")
async def impor_order(payload: dict, session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    n = 0
    for baris in payload.get("baris") or []:
        await tambah_order(baris, session, _)
        n += 1
    return {"masuk": n}


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
async def tarik(hari: int = 15, session: AsyncSession = _db(), _: BlUser = _guard()):
    """Tarik order ERP untuk toko yang masuk Bumi Lestari. Tidak mengirim status ke Shopee."""
    await _siap(session)
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import SessionLocal
    if SessionLocal is None:
        raise HTTPException(503, "Database ERP belum tersambung")
    async with SessionLocal() as erp:
        baris = (await erp.execute(text("""
            SELECT p.id_eksternal, i.id AS item_id, i.nama_produk, i.qty, p.nama_pembeli, a.nama_toko,
                   COALESCE(p.dipesan_at, p.created_at)::date AS tgl_pesan
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
            INSERT INTO bl2_order (id, no_order, nama_barang, pembeli, toko, sumber, sumber_ref, jenis_id, tgl_pesan, qty, varian)
            VALUES (:id, :no, :nama, :pembeli, :toko, 'erp', :ref, :jenis, :tgl, :qty, '')
        """), {
            "id": uuid.uuid4().hex, "no": r["id_eksternal"] or "", "nama": nama,
            "pembeli": r["nama_pembeli"] or "", "toko": r["nama_toko"] or "", "ref": ref, "jenis": jenis,
            "tgl": r["tgl_pesan"], "qty": r["qty"] or 1,
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
    nilai = (await session.execute(text("SELECT id, varian_id, nilai, jenis_id, harga_tukang::float AS harga_tukang, custom FROM bl2_nilai ORDER BY nilai"))).mappings().all()
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
        INSERT INTO bl2_jenis (id, nama, kayu, ukuran, harga_reseller, produk_id, harga_tukang, custom)
        VALUES (:id, :nama, :kayu, :ukuran, :harga, :produk, :tukang, :custom)
    """), {
        "id": jid,
        "nama": f"{induk['nama']} · {induk['varian']} · {nilai}",
        "kayu": induk["kayu"],
        "ukuran": nilai,
        "harga": payload.get("harga_reseller") or 0,
        "produk": induk["id"],
        "tukang": 0 if payload.get("custom") else (payload.get("harga_tukang") or 0),
        "custom": bool(payload.get("custom")),
    })
    await session.execute(text("INSERT INTO bl2_nilai (id, varian_id, nilai, jenis_id, harga_tukang, custom) VALUES (:id, :varian, :nilai, :jenis, :tukang, :custom)"), {
        "id": uuid.uuid4().hex, "varian": vid, "nilai": nilai, "jenis": jid,
        "tukang": 0 if payload.get("custom") else (payload.get("harga_tukang") or 0),
        "custom": bool(payload.get("custom")),
    })
    await session.commit()
    return {"id": jid}


@router.patch("/produk/{produk_id}")
async def ubah_produk(produk_id: str, payload: dict, session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    await session.execute(text("UPDATE bl2_produk SET nama = :nama, kayu = :kayu WHERE id = :id"), {
        "id": produk_id, "nama": str(payload.get("nama") or "").strip(), "kayu": bool(payload.get("kayu", True)),
    })
    await session.commit()
    return {"ok": True}


@router.delete("/produk/{produk_id}")
async def hapus_produk(produk_id: str, session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    await session.execute(text("DELETE FROM bl2_jenis WHERE produk_id = :id"), {"id": produk_id})
    await session.execute(text("DELETE FROM bl2_nilai WHERE varian_id IN (SELECT id FROM bl2_varian WHERE produk_id = :id)"), {"id": produk_id})
    await session.execute(text("DELETE FROM bl2_varian WHERE produk_id = :id"), {"id": produk_id})
    await session.execute(text("DELETE FROM bl2_produk WHERE id = :id"), {"id": produk_id})
    await session.commit()
    return {"ok": True}


@router.delete("/varian/{varian_id}")
async def hapus_varian(varian_id: str, session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    await session.execute(text("DELETE FROM bl2_jenis WHERE id IN (SELECT jenis_id FROM bl2_nilai WHERE varian_id = :id)"), {"id": varian_id})
    await session.execute(text("DELETE FROM bl2_nilai WHERE varian_id = :id"), {"id": varian_id})
    await session.execute(text("DELETE FROM bl2_varian WHERE id = :id"), {"id": varian_id})
    await session.commit()
    return {"ok": True}


@router.patch("/nilai/{nilai_id}")
async def ubah_nilai(nilai_id: str, payload: dict, session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    custom = bool(payload.get("custom"))
    harga = 0 if custom else (payload.get("harga_tukang") or 0)
    nilai = str(payload.get("nilai") or "").strip()
    await session.execute(text("UPDATE bl2_nilai SET nilai = :nilai, harga_tukang = :harga, custom = :custom WHERE id = :id"), {
        "id": nilai_id, "nilai": nilai, "harga": harga, "custom": custom,
    })
    await session.execute(text("""
        UPDATE bl2_jenis j SET ukuran = :nilai, harga_tukang = :harga, custom = :custom,
            nama = p.nama || ' · ' || v.nama || ' · ' || :nilai
        FROM bl2_nilai n
        JOIN bl2_varian v ON v.id = n.varian_id
        JOIN bl2_produk p ON p.id = v.produk_id
        WHERE n.id = :id AND j.id = n.jenis_id
    """), {"id": nilai_id, "nilai": nilai, "harga": harga, "custom": custom})
    await session.commit()
    return {"ok": True}


@router.delete("/nilai/{nilai_id}")
async def hapus_nilai(nilai_id: str, session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    await session.execute(text("DELETE FROM bl2_jenis WHERE id = (SELECT jenis_id FROM bl2_nilai WHERE id = :id)"), {"id": nilai_id})
    await session.execute(text("DELETE FROM bl2_nilai WHERE id = :id"), {"id": nilai_id})
    await session.commit()
    return {"ok": True}


@router.delete("/jenis/{jenis_id}")
async def hapus_jenis(jenis_id: str, session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    await session.execute(text("DELETE FROM bl2_jenis WHERE id = :id"), {"id": jenis_id})
    await session.commit()
    return {"ok": True}


@router.post("/impor")
async def impor(session: AsyncSession = _db(), _: BlUser = _guard()):
    """Isi katalog dari daftar harga. Aman diulang. Cat tidak ikut."""
    await _siap(session)
    import json
    from pathlib import Path
    data = json.loads(Path(__file__).with_name("katalog.json").read_text())
    produk = 0
    nilai = 0
    for p in data["produk"]:
        ada = (await session.execute(text("SELECT id FROM bl2_produk WHERE id = :id"), {"id": p["id"]})).scalar()
        if not ada:
            await session.execute(text("INSERT INTO bl2_produk (id, nama, kayu) VALUES (:id, :nama, :kayu)"), p)
            produk += 1
        vid = __import__("hashlib").md5(f"varian|{p['id']}|Ukuran".encode()).hexdigest()
        if not (await session.execute(text("SELECT id FROM bl2_varian WHERE id = :id"), {"id": vid})).scalar():
            await session.execute(text("INSERT INTO bl2_varian (id, produk_id, nama) VALUES (:id, :produk, 'Ukuran')"), {"id": vid, "produk": p["id"]})
    for n in data["nilai"]:
        if (await session.execute(text("SELECT id FROM bl2_nilai WHERE id = :id"), {"id": n["id"]})).scalar():
            continue
        await session.execute(text("""
            INSERT INTO bl2_jenis (id, nama, kayu, ukuran, harga_tukang, produk_id)
            VALUES (:jenis, :nama, TRUE, :ukuran, :harga, :produk)
        """), n)
        await session.execute(text("""
            INSERT INTO bl2_nilai (id, varian_id, nilai, jenis_id, harga_tukang, custom)
            VALUES (:id, :varian, :nilai, :jenis, :harga, FALSE)
        """), n)
        nilai += 1
    await session.commit()
    return {"produk": produk, "nilai": nilai}


TARIF_CAT = {
    "60x20x200": 80000, "80x20x200": 100000, "100x20x200": 120000, "120x20x200": 140000,
    "140x20x200": 160000, "150x20x200": 170000, "160x20x200": 180000, "180x20x200": 200000, "200x20x200": 220000,
    "60x10x200": 80000, "80x10x200": 90000, "100x10x200": 100000, "120x10x200": 120000,
    "140x10x200": 130000, "150x10x200": 140000, "160x10x200": 150000, "180x10x200": 160000, "200x10x200": 170000,
}


@router.post("/cat")
async def isi_cat(session: AsyncSession = _db(), _: BlUser = _guard()):
    """Pasang tarif cat partisi rak palang. Packing biasa sudah termasuk. Packing kayu tidak ikut."""
    await _siap(session)
    pas = 0
    for ukuran, harga in TARIF_CAT.items():
        hasil = await session.execute(text("""
            UPDATE bl2_nilai SET harga_cat = :harga
            WHERE replace(lower(nilai), ' ', '') LIKE '%' || :ukuran || '%'
        """), {"harga": harga, "ukuran": ukuran})
        await session.execute(text("""
            UPDATE bl2_jenis SET harga_cat = :harga
            WHERE replace(lower(ukuran), ' ', '') LIKE '%' || :ukuran || '%'
               OR replace(lower(nama), ' ', '') LIKE '%' || :ukuran || '%'
        """), {"harga": harga, "ukuran": ukuran})
        pas += hasil.rowcount or 0
        await session.execute(text("""
            UPDATE bl2_nilai SET custom = FALSE WHERE replace(lower(nilai), ' ', '') LIKE '%' || :ukuran || '%'
        """), {"ukuran": ukuran})
    await session.execute(text("""
        UPDATE bl2_nilai SET custom = TRUE, harga_cat = 0
        WHERE COALESCE(harga_cat, 0) = 0
    """))
    await session.commit()
    return {"nilai": pas, "catatan": "Termasuk packing biasa. Ukuran tanpa tarif ditandai custom."}


@router.get("/pihak")
async def pihak(session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    reseller = (await session.execute(text("SELECT kode, nama FROM bl2_reseller ORDER BY kode"))).mappings().all()
    tukang = (await session.execute(text("SELECT id, nama FROM bl2_tukang ORDER BY id"))).mappings().all()
    return {"reseller": [dict(r) for r in reseller], "tukang": [dict(r) for r in tukang]}


@router.patch("/pihak")
async def ubah_pihak(payload: dict, session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    for r in payload.get("reseller") or []:
        await session.execute(text("UPDATE bl2_reseller SET nama = :nama WHERE kode = :kode"), {"kode": r.get("kode"), "nama": r.get("nama") or ""})
    for t in payload.get("tukang") or []:
        await session.execute(text("UPDATE bl2_tukang SET nama = :nama WHERE id = :id"), {"id": t.get("id"), "nama": t.get("nama") or ""})
    await session.commit()
    return {"ok": True}


@router.patch("/order/{order_id}/jenis")
async def jenis_pesanan(order_id: str, payload: dict, session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    nilai = str(payload.get("jenis_pesanan") or "")
    if nilai not in ("Kayu", "Non-Kayu", ""):
        raise HTTPException(422, "Jenis pesanan hanya Kayu atau Non-Kayu")
    await session.execute(text("UPDATE bl2_order SET jenis_pesanan = :nilai WHERE id = :id"), {"nilai": nilai, "id": order_id})
    await session.commit()
    return {"ok": True}


@router.patch("/order/{order_id}")
async def ubah_order(order_id: str, payload: dict, session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    sumber = (await session.execute(text("SELECT sumber FROM bl2_order WHERE id = :id"), {"id": order_id})).scalar()
    if sumber == "erp":
        raise HTTPException(403, "Pesanan ERP tidak diubah di sini")
    await session.execute(text("""
        UPDATE bl2_order SET tgl_pesan = :tgl, toko = :toko, nama_barang = :nama, varian = :varian,
        qty = :qty, no_order = :no, keterangan = :ket WHERE id = :id
    """), {
        "id": order_id, "tgl": payload.get("tgl_pesan") or None, "toko": payload.get("toko") or "",
        "nama": payload.get("nama_barang") or "", "varian": payload.get("varian") or "",
        "qty": int(payload.get("qty") or 1), "no": payload.get("no_order") or "", "ket": payload.get("keterangan") or "",
    })
    await session.commit()
    return {"ok": True}


@router.delete("/order/{order_id}")
async def hapus_order(order_id: str, session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    sumber = (await session.execute(text("SELECT sumber FROM bl2_order WHERE id = :id"), {"id": order_id})).scalar()
    if sumber == "erp":
        raise HTTPException(403, "Pesanan ERP tidak dihapus")
    await session.execute(text("DELETE FROM bl2_order WHERE id = :id"), {"id": order_id})
    await session.commit()
    return {"ok": True}


@router.post("/order/massal")
async def massal(payload: dict, session: AsyncSession = _db(), _: BlUser = _guard()):
    await _siap(session)
    ids = payload.get("id") or []
    if payload.get("aksi") == "hapus":
        await session.execute(text("DELETE FROM bl2_order WHERE id = ANY(:ids) AND sumber <> 'erp'"), {"ids": ids})
    elif payload.get("aksi") == "jenis":
        nilai = str(payload.get("jenis_pesanan") or "")
        if nilai not in ("Kayu", "Non-Kayu"):
            raise HTTPException(422, "Jenis pesanan hanya Kayu atau Non-Kayu")
        await session.execute(text("UPDATE bl2_order SET jenis_pesanan = :nilai WHERE id = ANY(:ids)"), {"nilai": nilai, "ids": ids})
    await session.commit()
    return {"ok": True}
