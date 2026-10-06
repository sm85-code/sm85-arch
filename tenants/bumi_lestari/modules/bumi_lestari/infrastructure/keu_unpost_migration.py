"""Add cancellation provenance and allow only immutable, audited posted-entry cancellation."""
from sqlalchemy import text

VERSION = "20261009_keu_unpost"

STATEMENTS = []
for table in ("keu_transaksi", "keu_settlement"):
    STATEMENTS.extend([
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS alasan_batal text NOT NULL DEFAULT ''",
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS dibatalkan_oleh varchar(64) REFERENCES bl_users(id) ON DELETE RESTRICT",
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS dibatalkan_at timestamptz",
    ])


def guard(table, function, date_column):
    is_settlement = table == "keu_settlement"
    declare = "DECLARE allocated numeric;" if is_settlement else ""
    insert_guard = "IF TG_OP='INSERT' AND NEW.status<>'draf' THEN RAISE EXCEPTION 'Settlement must start as draft' USING ERRCODE='23514'; END IF;" if is_settlement else ""
    settlement_cancel = "IF EXISTS (SELECT 1 FROM keu_transaksi WHERE settlement_id=OLD.id AND status<>'dibatalkan') THEN RAISE EXCEPTION 'Cancel linked transactions first' USING ERRCODE='23514'; END IF;" if is_settlement else ""
    reconcile = "SELECT sum(jumlah) INTO allocated FROM keu_alokasi_settlement WHERE settlement_id=NEW.id; IF allocated IS NULL OR allocated<>NEW.neto THEN RAISE EXCEPTION 'Settlement allocations do not reconcile' USING ERRCODE='23514'; END IF;" if is_settlement else ""
    linked = "" if is_settlement else "IF NEW.status='terkirim' AND EXISTS (SELECT 1 FROM keu_settlement WHERE id=NEW.settlement_id AND status='dibatalkan') THEN RAISE EXCEPTION 'Settlement is cancelled' USING ERRCODE='23514'; END IF;"
    return f"""CREATE OR REPLACE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
{declare}
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Financial history cannot be deleted' USING ERRCODE='23514'; END IF;
 {insert_guard}
 IF TG_OP='UPDATE' AND OLD.status='terkirim' AND NEW.status='dibatalkan' THEN
  IF (to_jsonb(NEW)-ARRAY['status','alasan_batal','dibatalkan_oleh','dibatalkan_at']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','alasan_batal','dibatalkan_oleh','dibatalkan_at']) THEN
   RAISE EXCEPTION 'Original posted entry is immutable' USING ERRCODE='23514';
  END IF;
  IF length(trim(NEW.alasan_batal)) NOT BETWEEN 3 AND 2000 OR NEW.dibatalkan_at IS NULL OR NOT EXISTS
   (SELECT 1 FROM bl_users WHERE id=NEW.dibatalkan_oleh AND role IN ('owner','admin') AND aktif AND NOT must_change_password) THEN
   RAISE EXCEPTION 'Cancellation requires reason and authorized actor' USING ERRCODE='23514';
  END IF;
  IF EXISTS (SELECT 1 FROM bl_tutup_buku WHERE periode=to_char(OLD.{date_column},'YYYY-MM') AND status='ditutup') THEN
   RAISE EXCEPTION 'Accounting period is closed' USING ERRCODE='23514';
  END IF;
  {settlement_cancel}
  RETURN NEW;
 END IF;
 IF TG_OP='UPDATE' AND OLD.status<>'draf' AND NEW IS DISTINCT FROM OLD THEN RAISE EXCEPTION 'Posted or cancelled history is immutable' USING ERRCODE='23514'; END IF;
 IF NEW.status='dibatalkan' AND (TG_OP='INSERT' OR OLD.status='draf') THEN RAISE EXCEPTION 'Only posted entries can be unposted' USING ERRCODE='23514'; END IF;
 IF (TG_OP='INSERT' OR OLD.status='draf') AND (NEW.alasan_batal<>'' OR NEW.dibatalkan_oleh IS NOT NULL OR NEW.dibatalkan_at IS NOT NULL) THEN RAISE EXCEPTION 'Cancellation metadata is reserved for unpost' USING ERRCODE='23514'; END IF;
 {linked}
 IF NEW.status='terkirim' AND (TG_OP='INSERT' OR OLD.status<>'terkirim') THEN
  {reconcile}
  IF EXISTS (SELECT 1 FROM bl_tutup_buku WHERE periode=to_char(NEW.{date_column},'YYYY-MM') AND status='ditutup') THEN RAISE EXCEPTION 'Accounting period is closed' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END;
$keu$"""


def statements():
    return [*STATEMENTS, guard("keu_transaksi", "keu_transaction_guard", "tanggal"),
            guard("keu_settlement", "keu_settlement_guard", "tanggal_cair")]


def sql():
    from .keu_vendor_migration import VERSION as vendor_version
    parts = ["BEGIN", "SET LOCAL lock_timeout = '5s'", "SET LOCAL statement_timeout = '120s'",
             "SELECT pg_advisory_xact_lock(61062026)",
             "DO $$ BEGIN IF to_regclass('bl_users') IS NULL OR to_regclass('keu_transaksi') IS NULL OR to_regclass('keu_schema_versions') IS NULL THEN RAISE EXCEPTION 'Target is not an initialized BUMI keu tenant'; END IF; END $$",
             f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM keu_schema_versions WHERE version='{vendor_version}') THEN RAISE EXCEPTION 'Apply keu_dynamic_vendors.sql first'; END IF; END $$",
             *statements(), f"INSERT INTO keu_schema_versions(version) VALUES ('{VERSION}') ON CONFLICT DO NOTHING", "COMMIT"]
    return "\n\n".join(part.strip() + ";" for part in parts) + "\n"


async def ensure_unpost_schema(conn):
    if conn.dialect.name != "postgresql":
        return
    await conn.execute(text("SELECT pg_advisory_xact_lock(61062026)"))
    if (await conn.execute(text("SELECT 1 FROM keu_schema_versions WHERE version=:v"), {"v": VERSION})).first():
        return
    for statement in statements():
        await conn.execute(text(statement))
    await conn.execute(text("INSERT INTO keu_schema_versions(version) VALUES (:v)"), {"v": VERSION})
