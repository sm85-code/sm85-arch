"""Versioned startup migration and identical generated PostgreSQL SQL."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.sql.ddl import sort_tables

from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlUser  # noqa: F401
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_keu import KEU_MODELS

VERSION = "20261006_keu_initial"

# Released DDL/invariants are immutable; product/vendor changes use additive migrations.
INITIAL_PRODUCT_DDL = "CREATE TABLE keu_produk (\n\tid VARCHAR(64) NOT NULL, \n\tsku VARCHAR(128) NOT NULL, \n\tnama VARCHAR(255) NOT NULL, \n\tjenis VARCHAR(16) NOT NULL, \n\tbiaya_acuan NUMERIC(20, 2) DEFAULT '0' NOT NULL, \n\taktif BOOLEAN DEFAULT 'true' NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_keu_produk_jenis CHECK (jenis IN ('kayu', 'non_kayu')), \n\tCONSTRAINT ck_keu_produk_biaya CHECK (biaya_acuan >= 0), \n\tUNIQUE (sku)\n);"

INITIAL_VENDOR_DDL = "CREATE TABLE keu_vendor (\n\tid VARCHAR(64) NOT NULL, \n\tnama VARCHAR(255) NOT NULL, \n\tjenis VARCHAR(16) NOT NULL, \n\tkontak VARCHAR(255) DEFAULT '' NOT NULL, \n\taktif BOOLEAN DEFAULT 'true' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_keu_vendor_jenis CHECK (jenis IN ('tukang_kayu', 'supplier'))\n);"
INITIAL_VENDOR_SLOT_DDL = "CREATE TABLE keu_vendor_slot (\n\tjenis VARCHAR(16) NOT NULL, \n\tnomor INTEGER NOT NULL, \n\tvendor_id VARCHAR(64), \n\tPRIMARY KEY (jenis, nomor), \n\tCONSTRAINT uq_keu_vendor_slot_vendor UNIQUE (vendor_id), \n\tCONSTRAINT ck_keu_vendor_slot CHECK ((jenis = 'tukang_kayu' AND nomor BETWEEN 1 AND 5) OR (jenis = 'supplier' AND nomor BETWEEN 1 AND 3)), \n\tFOREIGN KEY(vendor_id) REFERENCES keu_vendor (id) ON DELETE RESTRICT\n);"

# Statement boundaries are explicit: asyncpg executes exactly one statement per call.

INITIAL_TRANSACTION_DDL = "CREATE TABLE keu_transaksi (\n\tid VARCHAR(64) NOT NULL, \n\tsaluran_id VARCHAR(64) NOT NULL, \n\tsumber_ref VARCHAR(255) NOT NULL, \n\takun_id VARCHAR(64) NOT NULL, \n\tkategori_id VARCHAR(64) NOT NULL, \n\tsettlement_id VARCHAR(64), \n\ttanggal DATE NOT NULL, \n\tjenis VARCHAR(16) NOT NULL, \n\tjumlah NUMERIC(20, 2) NOT NULL, \n\tstatus VARCHAR(16) DEFAULT 'draf' NOT NULL, \n\tketerangan TEXT DEFAULT '' NOT NULL, \n\tdibuat_oleh VARCHAR(64) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_keu_transaksi_sumber UNIQUE (saluran_id, sumber_ref), \n\tCONSTRAINT ck_keu_transaksi_jenis CHECK (jenis IN ('masuk', 'keluar')), \n\tCONSTRAINT ck_keu_transaksi_jumlah CHECK (jumlah > 0), \n\tCONSTRAINT ck_keu_transaksi_status CHECK (status IN ('draf', 'terkirim', 'dibatalkan')), \n\tFOREIGN KEY(saluran_id) REFERENCES keu_saluran (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(akun_id) REFERENCES keu_akun (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(kategori_id) REFERENCES bl_kategori (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(settlement_id) REFERENCES keu_settlement (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(dibuat_oleh) REFERENCES bl_users (id) ON DELETE RESTRICT\n);"

INITIAL_SETTLEMENT_DDL = "CREATE TABLE keu_settlement (\n\tid VARCHAR(64) NOT NULL, \n\tsaluran_id VARCHAR(64) NOT NULL, \n\tsumber_ref VARCHAR(255) NOT NULL, \n\ttanggal_cair DATE NOT NULL, \n\tbruto NUMERIC(20, 2) NOT NULL, \n\tpotongan NUMERIC(20, 2) NOT NULL, \n\tpenyesuaian NUMERIC(20, 2) DEFAULT '0' NOT NULL, \n\tneto NUMERIC(20, 2) NOT NULL, \n\trincian JSONB DEFAULT '{}' NOT NULL, \n\tstatus VARCHAR(16) DEFAULT 'draf' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_keu_settlement_sumber UNIQUE (saluran_id, sumber_ref), \n\tCONSTRAINT ck_keu_settlement_rekonsiliasi CHECK (neto = bruto - potongan + penyesuaian), \n\tCONSTRAINT ck_keu_settlement_status CHECK (status IN ('draf', 'terkirim', 'dibatalkan')), \n\tFOREIGN KEY(saluran_id) REFERENCES keu_saluran (id) ON DELETE RESTRICT\n);"
INVARIANTS = [
    """CREATE OR REPLACE FUNCTION keu_vendor_slot_check() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
DECLARE kind text;
BEGIN
 IF NEW.vendor_id IS NOT NULL THEN
  SELECT jenis INTO kind FROM keu_vendor WHERE id=NEW.vendor_id FOR SHARE;
  IF kind IS DISTINCT FROM NEW.jenis THEN RAISE EXCEPTION 'Vendor type does not match slot' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END;
$keu$""",
    """DROP TRIGGER IF EXISTS keu_vendor_slot_check ON keu_vendor_slot""",
    """CREATE TRIGGER keu_vendor_slot_check BEFORE INSERT OR UPDATE ON keu_vendor_slot FOR EACH ROW EXECUTE FUNCTION keu_vendor_slot_check()""",
    """CREATE OR REPLACE FUNCTION keu_vendor_type_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
BEGIN
 IF NEW.jenis IS DISTINCT FROM OLD.jenis AND (EXISTS (SELECT 1 FROM keu_vendor_slot WHERE vendor_id=OLD.id) OR EXISTS (SELECT 1 FROM keu_alokasi_vendor WHERE vendor_id=OLD.id)) THEN
  RAISE EXCEPTION 'Referenced vendor type is immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$keu$""",
    """DROP TRIGGER IF EXISTS keu_vendor_type_guard ON keu_vendor""",
    """CREATE TRIGGER keu_vendor_type_guard BEFORE UPDATE OF jenis ON keu_vendor FOR EACH ROW EXECUTE FUNCTION keu_vendor_type_guard()""",
    """CREATE OR REPLACE FUNCTION keu_allocation_check() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
DECLARE capacity integer; allocated bigint; product_kind text; vendor_kind text; enabled boolean;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Cancel allocation instead of deleting it' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' AND (NEW.item_id IS DISTINCT FROM OLD.item_id OR NEW.vendor_id IS DISTINCT FROM OLD.vendor_id) THEN
  RAISE EXCEPTION 'Allocation identity is immutable' USING ERRCODE='23514';
 END IF;
 SELECT i.qty,p.jenis INTO capacity,product_kind FROM keu_item i LEFT JOIN keu_produk p ON p.id=i.produk_id WHERE i.id=NEW.item_id FOR UPDATE OF i;
 IF NOT FOUND THEN RAISE EXCEPTION 'Item not found' USING ERRCODE='23503'; END IF;
 SELECT jenis,aktif INTO vendor_kind,enabled FROM keu_vendor WHERE id=NEW.vendor_id FOR SHARE;
 IF NOT NEW.dibatalkan AND (product_kind IS NULL OR enabled IS DISTINCT FROM TRUE OR NOT EXISTS (SELECT 1 FROM keu_vendor_slot WHERE vendor_id=NEW.vendor_id) OR (product_kind='kayu' AND vendor_kind IS DISTINCT FROM 'tukang_kayu') OR (product_kind='non_kayu' AND vendor_kind IS DISTINCT FROM 'supplier')) THEN
  RAISE EXCEPTION 'Choose an active vendor for a mapped product' USING ERRCODE='23514';
 END IF;
 SELECT COALESCE(sum(qty),0) INTO allocated FROM keu_alokasi_vendor WHERE item_id=NEW.item_id AND NOT dibatalkan AND id<>NEW.id;
 IF allocated + (CASE WHEN NEW.dibatalkan THEN 0 ELSE NEW.qty END) > capacity THEN RAISE EXCEPTION 'Allocation exceeds item quantity' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END;
$keu$""",
    """DROP TRIGGER IF EXISTS keu_allocation_check ON keu_alokasi_vendor""",
    """CREATE TRIGGER keu_allocation_check BEFORE INSERT OR UPDATE OR DELETE ON keu_alokasi_vendor FOR EACH ROW EXECUTE FUNCTION keu_allocation_check()""",
    """CREATE OR REPLACE FUNCTION keu_item_capacity_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
DECLARE allocated bigint;
BEGIN
 SELECT COALESCE(sum(qty),0) INTO allocated FROM keu_alokasi_vendor WHERE item_id=NEW.id AND NOT dibatalkan;
 IF allocated > NEW.qty OR (allocated>0 AND NEW.produk_id IS DISTINCT FROM OLD.produk_id) THEN RAISE EXCEPTION 'Cancel allocations before changing item capacity/product' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END;
$keu$""",
    """DROP TRIGGER IF EXISTS keu_item_capacity_guard ON keu_item""",
    """CREATE TRIGGER keu_item_capacity_guard BEFORE UPDATE OF qty, produk_id ON keu_item FOR EACH ROW EXECUTE FUNCTION keu_item_capacity_guard()""",
    """CREATE OR REPLACE FUNCTION keu_product_type_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
BEGIN
 IF NEW.jenis IS DISTINCT FROM OLD.jenis AND EXISTS (SELECT 1 FROM keu_item i JOIN keu_alokasi_vendor a ON a.item_id=i.id WHERE i.produk_id=OLD.id AND NOT a.dibatalkan) THEN RAISE EXCEPTION 'Allocated product type is immutable' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END;
$keu$""",
    """DROP TRIGGER IF EXISTS keu_product_type_guard ON keu_produk""",
    """CREATE TRIGGER keu_product_type_guard BEFORE UPDATE OF jenis ON keu_produk FOR EACH ROW EXECUTE FUNCTION keu_product_type_guard()""",
    """CREATE OR REPLACE FUNCTION keu_settlement_allocation_check() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
DECLARE sid text; state text; channel text; item_channel text;
BEGIN
 IF TG_OP='UPDATE' AND (NEW.settlement_id IS DISTINCT FROM OLD.settlement_id OR NEW.item_id IS DISTINCT FROM OLD.item_id) THEN RAISE EXCEPTION 'Allocation identity is immutable' USING ERRCODE='23514'; END IF;
 sid:=CASE WHEN TG_OP='DELETE' THEN OLD.settlement_id ELSE NEW.settlement_id END;
 SELECT status,saluran_id INTO state,channel FROM keu_settlement WHERE id=sid FOR UPDATE;
 IF state IS DISTINCT FROM 'draf' THEN RAISE EXCEPTION 'Only draft settlement allocations can change' USING ERRCODE='23514'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 SELECT p.saluran_id INTO item_channel FROM keu_item i JOIN keu_pesanan p ON p.id=i.pesanan_id WHERE i.id=NEW.item_id;
 IF item_channel IS DISTINCT FROM channel THEN RAISE EXCEPTION 'Settlement and item channels differ' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END;
$keu$""",
    """DROP TRIGGER IF EXISTS keu_settlement_allocation_check ON keu_alokasi_settlement""",
    """CREATE TRIGGER keu_settlement_allocation_check BEFORE INSERT OR UPDATE OR DELETE ON keu_alokasi_settlement FOR EACH ROW EXECUTE FUNCTION keu_settlement_allocation_check()""",
    """CREATE OR REPLACE FUNCTION keu_settlement_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
DECLARE allocated numeric;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Settlement cannot be deleted' USING ERRCODE='23514'; END IF;
 IF TG_OP='INSERT' AND NEW.status<>'draf' THEN RAISE EXCEPTION 'Settlement must start as draft' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' AND OLD.status<>'draf' AND NEW IS DISTINCT FROM OLD THEN RAISE EXCEPTION 'Posted settlement requires a separate correction' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' AND NEW.status='terkirim' AND OLD.status<>'terkirim' THEN
  SELECT sum(jumlah) INTO allocated FROM keu_alokasi_settlement WHERE settlement_id=NEW.id;
  IF allocated IS NULL OR allocated<>NEW.neto THEN RAISE EXCEPTION 'Settlement allocations do not reconcile' USING ERRCODE='23514'; END IF;
  IF EXISTS (SELECT 1 FROM bl_tutup_buku WHERE periode=to_char(NEW.tanggal_cair,'YYYY-MM') AND status='ditutup') THEN RAISE EXCEPTION 'Accounting period is closed' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END;
$keu$""",
    """DROP TRIGGER IF EXISTS keu_settlement_guard ON keu_settlement""",
    """CREATE TRIGGER keu_settlement_guard BEFORE INSERT OR UPDATE OR DELETE ON keu_settlement FOR EACH ROW EXECUTE FUNCTION keu_settlement_guard()""",
    """CREATE OR REPLACE FUNCTION keu_transaction_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Transaction cannot be deleted' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' AND OLD.status<>'draf' AND NEW IS DISTINCT FROM OLD THEN RAISE EXCEPTION 'Posted transaction requires a separate correction' USING ERRCODE='23514'; END IF;
 IF NEW.status='terkirim' AND EXISTS (SELECT 1 FROM bl_tutup_buku WHERE periode=to_char(NEW.tanggal,'YYYY-MM') AND status='ditutup') THEN RAISE EXCEPTION 'Accounting period is closed' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END;
$keu$""",
    """DROP TRIGGER IF EXISTS keu_transaction_guard ON keu_transaksi""",
    """CREATE TRIGGER keu_transaction_guard BEFORE INSERT OR UPDATE OR DELETE ON keu_transaksi FOR EACH ROW EXECUTE FUNCTION keu_transaction_guard()""",
]


def sql() -> str:
    dialect = postgresql.dialect()
    statements = ["BEGIN;", "SET LOCAL lock_timeout = '5s';", "SET LOCAL statement_timeout = '120s';",
                  "SELECT pg_advisory_xact_lock(61062026);",
                  "DO $$ BEGIN IF to_regclass('bl_users') IS NULL OR to_regclass('bl_kategori') IS NULL THEN RAISE EXCEPTION 'Target is not an initialized BUMI tenant'; END IF; END $$;",
                  "CREATE TABLE IF NOT EXISTS keu_schema_versions (version varchar(64) PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());",
                  f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM keu_schema_versions WHERE version = '{VERSION}') THEN RAISE EXCEPTION 'Migration already applied'; END IF; END $$;"]
    for table in sort_tables([model.__table__ for model in KEU_MODELS]):
        historical = {"keu_produk": INITIAL_PRODUCT_DDL, "keu_vendor": INITIAL_VENDOR_DDL, "keu_vendor_slot": INITIAL_VENDOR_SLOT_DDL, "keu_transaksi": INITIAL_TRANSACTION_DDL, "keu_settlement": INITIAL_SETTLEMENT_DDL}
        statements.append(historical.get(table.name, str(CreateTable(table).compile(dialect=dialect)).strip() + ";"))
        for index in sorted(table.indexes, key=lambda obj: obj.name):
            statements.append(str(CreateIndex(index).compile(dialect=dialect)).strip() + ";")
    # Historical artifact only; current startup never seeds a fixed set of slots.
    for jenis, count in (("tukang_kayu", 5), ("supplier", 3)):
        statements.append(f"INSERT INTO keu_vendor_slot (jenis, nomor) SELECT '{jenis}', n FROM generate_series(1, {count}) n;")
    statements.extend(statement.strip() + ";" for statement in INVARIANTS)
    statements.extend([f"INSERT INTO keu_schema_versions(version) VALUES ('{VERSION}');", "COMMIT;"])
    return "\n\n".join(statements) + "\n"


async def ensure_keu_schema(conn) -> None:
    if conn.dialect.name == "postgresql":
        await conn.execute(text("SELECT pg_advisory_xact_lock(61062026)"))
        await conn.execute(text("CREATE TABLE IF NOT EXISTS keu_schema_versions (version varchar(64) PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"))
        exists = (await conn.execute(text("SELECT 1 FROM keu_schema_versions WHERE version=:v"), {"v": VERSION})).first()
        if exists:
            from .keu_product_migration import ensure_product_schema
            from .keu_vendor_migration import ensure_vendor_schema
            await ensure_product_schema(conn)
            await ensure_vendor_schema(conn)
            from .keu_unpost_migration import ensure_unpost_schema
            await ensure_unpost_schema(conn)
            return
    # create_all is additive. Existing tenant tables must be initialized by the BUMI seeder first.
    await conn.run_sync(lambda sync: KEU_MODELS[0].metadata.create_all(sync, tables=[m.__table__ for m in KEU_MODELS]))
    if conn.dialect.name == "postgresql":
        for statement in INVARIANTS:
            await conn.execute(text(statement))
        await conn.execute(text("INSERT INTO keu_schema_versions(version) VALUES (:v)"), {"v": VERSION})

    from .keu_product_migration import ensure_product_schema
    from .keu_vendor_migration import ensure_vendor_schema
    await ensure_product_schema(conn)
    await ensure_vendor_schema(conn)
    from .keu_unpost_migration import ensure_unpost_schema
    await ensure_unpost_schema(conn)
