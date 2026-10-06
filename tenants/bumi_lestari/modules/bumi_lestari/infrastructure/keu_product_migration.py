"""Additive PostgreSQL product metadata migration, guarded by the BUMI startup lock."""
from sqlalchemy import text

VERSION = "20261007_keu_product_metadata"
STATEMENTS = [
    "ALTER TABLE keu_produk ADD COLUMN IF NOT EXISTS sku_induk varchar(128)",
    "ALTER TABLE keu_produk ADD COLUMN IF NOT EXISTS nama_asli varchar(255) NOT NULL DEFAULT ''",
    "ALTER TABLE keu_produk ADD COLUMN IF NOT EXISTS gambar_url varchar(2048) NOT NULL DEFAULT ''",
    "ALTER TABLE keu_produk ADD COLUMN IF NOT EXISTS varian_list jsonb NOT NULL DEFAULT '[]'::jsonb",
    "ALTER TABLE keu_produk ADD COLUMN IF NOT EXISTS harga_jual numeric(20,2) NOT NULL DEFAULT 0",
    "ALTER TABLE keu_produk ADD COLUMN IF NOT EXISTS status varchar(16) NOT NULL DEFAULT 'master'",
    "ALTER TABLE keu_produk ALTER COLUMN jenis DROP NOT NULL",
    "UPDATE keu_produk SET nama_asli=nama WHERE nama_asli=''",
    "ALTER TABLE keu_produk DROP CONSTRAINT IF EXISTS ck_keu_produk_harga",
    "ALTER TABLE keu_produk ADD CONSTRAINT ck_keu_produk_harga CHECK (harga_jual >= 0)",
    "ALTER TABLE keu_produk DROP CONSTRAINT IF EXISTS ck_keu_produk_status",
    "ALTER TABLE keu_produk ADD CONSTRAINT ck_keu_produk_status CHECK (status IN ('draf','master') AND (status <> 'master' OR jenis IS NOT NULL))",
    "ALTER TABLE keu_produk DROP CONSTRAINT IF EXISTS ck_keu_produk_varian",
    "ALTER TABLE keu_produk ADD CONSTRAINT ck_keu_produk_varian CHECK (jsonb_typeof(varian_list)='array')",
]


def allocation_guard():
    from .keu_migration import INVARIANTS
    return INVARIANTS[6].replace(
        "enabled boolean;", "enabled boolean; product_state text; product_enabled boolean;"
    ).replace(
        "SELECT i.qty,p.jenis INTO capacity,product_kind", "SELECT i.qty,p.jenis,p.status,p.aktif INTO capacity,product_kind,product_state,product_enabled"
    ).replace(
        "product_kind IS NULL OR enabled", "product_kind IS NULL OR product_state IS DISTINCT FROM 'master' OR product_enabled IS DISTINCT FROM TRUE OR enabled"
    )


def sql():
    statements = ["BEGIN", "SET LOCAL lock_timeout = '5s'", "SET LOCAL statement_timeout = '120s'",
                  "SELECT pg_advisory_xact_lock(61062026)",
                  "DO $$ BEGIN IF to_regclass('bl_users') IS NULL OR to_regclass('keu_produk') IS NULL THEN RAISE EXCEPTION 'Target is not an initialized BUMI keu tenant'; END IF; END $$",
                  "CREATE TABLE IF NOT EXISTS keu_schema_versions (version varchar(64) PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())",
                  *STATEMENTS, allocation_guard(),
                  f"INSERT INTO keu_schema_versions(version) VALUES ('{VERSION}') ON CONFLICT DO NOTHING", "COMMIT"]
    return "\n\n".join(statement.strip() + ";" for statement in statements) + "\n"


async def ensure_product_schema(conn):
    if conn.dialect.name != "postgresql":
        return  # SQLite test databases are created directly from current metadata.
    await conn.execute(text("SELECT pg_advisory_xact_lock(61062026)"))
    if (await conn.execute(text("SELECT 1 FROM keu_schema_versions WHERE version=:v"), {"v": VERSION})).first():
        return
    for statement in [*STATEMENTS, allocation_guard()]:
        await conn.execute(text(statement))
    await conn.execute(text("INSERT INTO keu_schema_versions(version) VALUES (:v)"), {"v": VERSION})
