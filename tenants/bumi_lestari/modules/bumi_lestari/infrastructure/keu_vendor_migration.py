"""Additive dynamic vendor migration; historical vendor IDs and allocations stay intact."""
from sqlalchemy import text

VERSION = "20261008_keu_dynamic_vendors"

STATEMENTS = [
    "ALTER TABLE keu_vendor ADD COLUMN IF NOT EXISTS kode varchar(128)",
    "ALTER TABLE keu_vendor ADD COLUMN IF NOT EXISTS alamat text NOT NULL DEFAULT ''",
    "ALTER TABLE keu_vendor ADD COLUMN IF NOT EXISTS keterangan text NOT NULL DEFAULT ''",
    """UPDATE keu_vendor v SET kode=CASE
        WHEN EXISTS (SELECT 1 FROM keu_vendor_slot s WHERE s.vendor_id=v.id)
        THEN (SELECT (CASE WHEN s.jenis='tukang_kayu' THEN 'tk-' ELSE 'sup-' END) || s.nomor::text FROM keu_vendor_slot s WHERE s.vendor_id=v.id)
        ELSE 'VND-' || v.id END WHERE v.kode IS NULL""",
    "ALTER TABLE keu_vendor ALTER COLUMN kode SET NOT NULL",
    "ALTER TABLE keu_vendor DROP CONSTRAINT IF EXISTS keu_vendor_kode_key",
    "ALTER TABLE keu_vendor ADD CONSTRAINT keu_vendor_kode_key UNIQUE (kode)",
    "ALTER TABLE keu_vendor DROP CONSTRAINT IF EXISTS ck_keu_vendor_kode",
    "ALTER TABLE keu_vendor ADD CONSTRAINT ck_keu_vendor_kode CHECK (length(trim(kode)) > 0)",
    "ALTER TABLE keu_vendor_slot DROP CONSTRAINT IF EXISTS ck_keu_vendor_slot",
    "ALTER TABLE keu_vendor_slot ADD CONSTRAINT ck_keu_vendor_slot CHECK (jenis IN ('tukang_kayu','supplier') AND nomor >= 1)",
    """CREATE OR REPLACE FUNCTION keu_vendor_type_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
BEGIN
 IF NEW.jenis IS DISTINCT FROM OLD.jenis THEN
  IF EXISTS (SELECT 1 FROM keu_alokasi_vendor WHERE vendor_id=OLD.id) THEN
   RAISE EXCEPTION 'Referenced vendor type is immutable' USING ERRCODE='23514';
  END IF;
  UPDATE keu_vendor_slot SET vendor_id=NULL WHERE vendor_id=OLD.id;
 END IF;
 RETURN NEW;
END;
$keu$""",
]


def allocation_guard():
    from .keu_product_migration import allocation_guard as product_guard
    return product_guard().replace(" OR NOT EXISTS (SELECT 1 FROM keu_vendor_slot WHERE vendor_id=NEW.vendor_id)", "")


def sql():
    # Require product metadata first so this never overwrites its master/draft guards.
    from .keu_product_migration import VERSION as product_version
    statements = ["BEGIN", "SET LOCAL lock_timeout = '5s'", "SET LOCAL statement_timeout = '120s'",
                  "SELECT pg_advisory_xact_lock(61062026)",
                  "DO $$ BEGIN IF to_regclass('bl_users') IS NULL OR to_regclass('keu_vendor') IS NULL OR to_regclass('keu_schema_versions') IS NULL THEN RAISE EXCEPTION 'Target is not an initialized BUMI keu tenant'; END IF; END $$",
                  f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM keu_schema_versions WHERE version='{product_version}') THEN RAISE EXCEPTION 'Apply keu_product_metadata.sql first'; END IF; END $$",
                  *STATEMENTS, allocation_guard(),
                  f"INSERT INTO keu_schema_versions(version) VALUES ('{VERSION}') ON CONFLICT DO NOTHING", "COMMIT"]
    return "\n\n".join(statement.strip() + ";" for statement in statements) + "\n"


async def ensure_vendor_schema(conn):
    if conn.dialect.name != "postgresql":
        return  # SQLite tests use current metadata from an empty database.
    await conn.execute(text("SELECT pg_advisory_xact_lock(61062026)"))
    if (await conn.execute(text("SELECT 1 FROM keu_schema_versions WHERE version=:v"), {"v": VERSION})).first():
        return
    for statement in [*STATEMENTS, allocation_guard()]:
        await conn.execute(text(statement))
    await conn.execute(text("INSERT INTO keu_schema_versions(version) VALUES (:v)"), {"v": VERSION})
