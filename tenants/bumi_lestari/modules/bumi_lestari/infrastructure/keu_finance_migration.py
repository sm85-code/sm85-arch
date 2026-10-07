"""Additive ledger/FIFO DDL. No historical financial posting is performed at startup."""
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.sql.ddl import sort_tables

from .models import BlUser  # noqa: F401
from .models_keu_finance import FINANCE_MODELS

VERSION = "20261010_keu_complete_finance"

GUARDS = [
    """CREATE OR REPLACE FUNCTION keu_journal_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
BEGIN
 PERFORM pg_advisory_xact_lock(61062027);
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Financial journals cannot be deleted' USING ERRCODE='23514'; END IF;
 IF TG_OP='INSERT' THEN
  IF NEW.status<>'draf' OR NOT EXISTS(SELECT 1 FROM bl_users WHERE id=NEW.dibuat_oleh AND aktif AND NOT must_change_password AND role IN ('owner','admin')) THEN RAISE EXCEPTION 'Journal must start as authorized draft' USING ERRCODE='23514'; END IF;
  RETURN NEW;
 END IF;
 IF OLD.status='terkirim' AND NEW.status='dibatalkan' THEN
  IF OLD.jenis='pembukaan' OR (to_jsonb(NEW)-ARRAY['status','alasan_batal','dibatalkan_oleh','dibatalkan_at']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','alasan_batal','dibatalkan_oleh','dibatalkan_at']) THEN RAISE EXCEPTION 'Original journal is immutable' USING ERRCODE='23514'; END IF;
  IF NOT EXISTS(SELECT 1 FROM bl_users WHERE id=NEW.dibatalkan_oleh AND aktif AND NOT must_change_password AND role IN ('owner','admin')) THEN RAISE EXCEPTION 'Unauthorized cancellation actor' USING ERRCODE='23514'; END IF;
  IF EXISTS(SELECT 1 FROM bl_tutup_buku WHERE periode=to_char(OLD.tanggal,'YYYY-MM') AND status='ditutup') THEN RAISE EXCEPTION 'Accounting period is closed' USING ERRCODE='23514'; END IF;
  IF EXISTS(SELECT 1 FROM keu_stok_mutasi r JOIN keu_stok_pemakaian u ON u.masuk_id=r.id JOIN keu_stok_mutasi o ON o.id=u.keluar_id JOIN keu_jurnal j ON j.id=o.jurnal_id WHERE r.jurnal_id=OLD.id AND j.status='terkirim') THEN RAISE EXCEPTION 'Consumed stock receipt cannot be cancelled' USING ERRCODE='23514'; END IF;
  IF EXISTS(SELECT 1 FROM keu_stok_mutasi s JOIN keu_stok_mutasi later ON later.produk_id=s.produk_id JOIN keu_jurnal j ON j.id=later.jurnal_id WHERE s.jurnal_id=OLD.id AND s.jenis='keluar' AND later.jenis='keluar' AND j.status='terkirim' AND (j.tanggal,j.created_at)>(OLD.tanggal,OLD.created_at)) THEN RAISE EXCEPTION 'Cancel subsequent FIFO uses first' USING ERRCODE='23514'; END IF;
  IF EXISTS(SELECT b.vendor_id FROM keu_jurnal_baris b JOIN keu_coa a ON a.id=b.coa_id WHERE b.jurnal_id=OLD.id AND a.kelompok='utang_vendor' GROUP BY b.vendor_id HAVING sum(b.kredit-b.debet)>COALESCE((SELECT sum(v.kredit-v.debet) FROM keu_jurnal_baris v JOIN keu_jurnal j ON j.id=v.jurnal_id JOIN keu_coa va ON va.id=v.coa_id WHERE v.vendor_id=b.vendor_id AND va.kelompok='utang_vendor' AND j.status='terkirim'),0)) THEN RAISE EXCEPTION 'Cancel vendor payments before cancelling the debt' USING ERRCODE='23514'; END IF;
  RETURN NEW;
 END IF;
 IF OLD.status<>'draf' AND NEW IS DISTINCT FROM OLD THEN RAISE EXCEPTION 'Posted/cancelled journal is immutable' USING ERRCODE='23514'; END IF;
 IF NEW.status='dibatalkan' THEN RAISE EXCEPTION 'Only posted journals can be cancelled' USING ERRCODE='23514'; END IF;
 IF NEW.status='terkirim' AND OLD.status='draf' AND EXISTS(SELECT 1 FROM bl_tutup_buku WHERE periode=to_char(NEW.tanggal,'YYYY-MM') AND status='ditutup') AND NOT EXISTS(SELECT 1 FROM keu_buku b JOIN bl_users u ON u.id=NEW.dibuat_oleh WHERE b.id='utama' AND b.status='migrasi' AND u.role='owner' AND u.aktif AND NOT u.must_change_password) THEN RAISE EXCEPTION 'Accounting period is closed' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END;
$keu$""",
    """DROP TRIGGER IF EXISTS keu_journal_guard ON keu_jurnal""",
    """CREATE TRIGGER keu_journal_guard BEFORE INSERT OR UPDATE OR DELETE ON keu_jurnal FOR EACH ROW EXECUTE FUNCTION keu_journal_guard()""",
    """CREATE OR REPLACE FUNCTION keu_journal_line_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
DECLARE jid text; state text; kind text; grp text; cash_id text;
BEGIN
 PERFORM pg_advisory_xact_lock(61062027);
 jid:=CASE WHEN TG_OP='DELETE' THEN OLD.jurnal_id ELSE NEW.jurnal_id END;
 SELECT status,jenis INTO state,kind FROM keu_jurnal WHERE id=jid FOR UPDATE;
 IF state IS DISTINCT FROM 'draf' THEN RAISE EXCEPTION 'Posted journal lines are immutable' USING ERRCODE='23514'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 IF TG_OP='UPDATE' AND NEW.jurnal_id<>OLD.jurnal_id THEN RAISE EXCEPTION 'Journal identity is immutable' USING ERRCODE='23514'; END IF;
 SELECT kelompok,kas_akun_id INTO grp,cash_id FROM keu_coa WHERE id=NEW.coa_id;
 IF cash_id IS NULL AND NEW.arus IS NOT NULL OR cash_id IS NOT NULL AND NEW.arus IS NULL AND kind<>'pembukaan' THEN RAISE EXCEPTION 'Cash flow classification mismatch' USING ERRCODE='23514'; END IF;
 IF grp='utang_vendor' AND NEW.vendor_id IS NULL OR grp<>'utang_vendor' AND NEW.vendor_id IS NOT NULL THEN RAISE EXCEPTION 'Vendor subledger is required only for vendor debt' USING ERRCODE='23514'; END IF;
 IF grp='persediaan' AND NEW.produk_id IS NULL THEN RAISE EXCEPTION 'Inventory requires product subledger' USING ERRCODE='23514'; END IF;
 IF grp IN ('piutang_pengiriman','piutang_escrow') AND NEW.pesanan_id IS NULL THEN RAISE EXCEPTION 'Receivable requires order subledger' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END;
$keu$""",
    """DROP TRIGGER IF EXISTS keu_journal_line_guard ON keu_jurnal_baris""",
    """CREATE TRIGGER keu_journal_line_guard BEFORE INSERT OR UPDATE OR DELETE ON keu_jurnal_baris FOR EACH ROW EXECUTE FUNCTION keu_journal_line_guard()""",
    """CREATE OR REPLACE FUNCTION keu_journal_balance_check() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
DECLARE j keu_jurnal%ROWTYPE; n bigint; d numeric; c numeric; stock keu_stok_mutasi%ROWTYPE; inventory_value numeric;
BEGIN
 SELECT * INTO j FROM keu_jurnal WHERE id=NEW.id;
 IF j.status<>'terkirim' THEN RETURN NULL; END IF;
 SELECT count(*),COALESCE(sum(debet),0),COALESCE(sum(kredit),0) INTO n,d,c FROM keu_jurnal_baris WHERE jurnal_id=j.id;
 SELECT * INTO stock FROM keu_stok_mutasi WHERE jurnal_id=j.id;
 IF d<>c OR (n<2 AND NOT(j.jenis='stok_keluar' AND stock.id IS NOT NULL AND stock.nilai=0)) THEN RAISE EXCEPTION 'Posted journal must be balanced' USING ERRCODE='23514'; END IF;
 IF j.jenis IN ('stok_masuk','stok_keluar') THEN
  SELECT COALESCE(sum(b.debet-b.kredit),0) INTO inventory_value FROM keu_jurnal_baris b JOIN keu_coa a ON a.id=b.coa_id WHERE b.jurnal_id=j.id AND a.kelompok='persediaan' AND b.produk_id=stock.produk_id;
  IF stock.id IS NULL OR inventory_value<>(CASE WHEN stock.jenis='masuk' THEN stock.nilai ELSE -stock.nilai END) THEN RAISE EXCEPTION 'Stock and inventory journal do not reconcile' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NULL;
END;
$keu$""",
    """DROP TRIGGER IF EXISTS keu_journal_balance_check ON keu_jurnal""",
    """CREATE CONSTRAINT TRIGGER keu_journal_balance_check AFTER INSERT OR UPDATE ON keu_jurnal DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION keu_journal_balance_check()""",
    """CREATE OR REPLACE FUNCTION keu_finance_master_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
BEGIN
 IF TG_TABLE_NAME='keu_buku' THEN
  IF TG_OP='DELETE' OR OLD.status<>'migrasi' OR NEW.status<>'aktif' OR (to_jsonb(NEW)-'status') IS DISTINCT FROM (to_jsonb(OLD)-'status') THEN RAISE EXCEPTION 'Accounting policy is immutable after activation' USING ERRCODE='23514'; END IF;
 ELSE
  IF OLD.sistem OR EXISTS(SELECT 1 FROM keu_jurnal_baris WHERE coa_id=OLD.id) THEN
   IF TG_OP='DELETE' OR (to_jsonb(NEW)-ARRAY['nama','aktif']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['nama','aktif']) OR OLD.sistem AND NOT NEW.aktif THEN RAISE EXCEPTION 'Referenced/system chart identity is immutable' USING ERRCODE='23514'; END IF;
  END IF;
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END;
$keu$""",
    """DROP TRIGGER IF EXISTS keu_finance_master_guard ON keu_coa""",
    """CREATE TRIGGER keu_finance_master_guard BEFORE UPDATE OR DELETE ON keu_coa FOR EACH ROW EXECUTE FUNCTION keu_finance_master_guard()""",
    """DROP TRIGGER IF EXISTS keu_finance_master_guard ON keu_buku""",
    """CREATE TRIGGER keu_finance_master_guard BEFORE UPDATE OR DELETE ON keu_buku FOR EACH ROW EXECUTE FUNCTION keu_finance_master_guard()""",
    """CREATE OR REPLACE FUNCTION keu_stock_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
DECLARE stock keu_stok_mutasi%ROWTYPE; capacity bigint; consumed bigint; kind text; cost numeric; expected numeric;
BEGIN
 PERFORM pg_advisory_xact_lock(61062027);
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'Stock history is immutable; unpost the journal instead' USING ERRCODE='23514'; END IF;
 IF TG_TABLE_NAME='keu_stok_mutasi' THEN
  SELECT jenis INTO kind FROM keu_jurnal WHERE id=NEW.jurnal_id;
  IF kind IS DISTINCT FROM ('stok_' || NEW.jenis) THEN RAISE EXCEPTION 'Stock journal type mismatch' USING ERRCODE='23514'; END IF;
  IF NEW.jenis='keluar' THEN
   SELECT i.qty INTO capacity FROM keu_item i WHERE i.id=NEW.item_id AND i.produk_id=NEW.produk_id FOR UPDATE;
   IF NOT FOUND THEN RAISE EXCEPTION 'Stock fulfillment item mismatch' USING ERRCODE='23514'; END IF;
   SELECT COALESCE(sum(qty),0) INTO consumed FROM keu_alokasi_vendor WHERE item_id=NEW.item_id AND NOT dibatalkan;
   SELECT consumed+COALESCE(sum(s.qty),0) INTO consumed FROM keu_stok_mutasi s JOIN keu_jurnal j ON j.id=s.jurnal_id WHERE s.item_id=NEW.item_id AND s.jenis='keluar' AND j.status='terkirim';
   IF consumed+NEW.qty>capacity THEN RAISE EXCEPTION 'Stock and vendor allocation exceed item quantity' USING ERRCODE='23514'; END IF;
  END IF;
 ELSE
  SELECT * INTO stock FROM keu_stok_mutasi WHERE id=NEW.masuk_id FOR UPDATE;
  IF stock.jenis IS DISTINCT FROM 'masuk' OR NOT EXISTS(SELECT 1 FROM keu_stok_mutasi o JOIN keu_jurnal jo ON jo.id=o.jurnal_id JOIN keu_jurnal ji ON ji.id=stock.jurnal_id WHERE o.id=NEW.keluar_id AND o.jenis='keluar' AND o.produk_id=stock.produk_id AND jo.tanggal>=ji.tanggal AND jo.status='terkirim' AND ji.status='terkirim') THEN RAISE EXCEPTION 'Invalid FIFO source batch' USING ERRCODE='23514'; END IF;
  SELECT COALESCE(sum(u.qty),0),COALESCE(sum(u.nilai),0) INTO consumed,cost FROM keu_stok_pemakaian u JOIN keu_stok_mutasi o ON o.id=u.keluar_id JOIN keu_jurnal j ON j.id=o.jurnal_id WHERE u.masuk_id=stock.id AND j.status='terkirim';
  IF consumed+NEW.qty>stock.qty THEN RAISE EXCEPTION 'FIFO batch capacity exceeded' USING ERRCODE='23514'; END IF;
  IF EXISTS(SELECT 1 FROM keu_stok_mutasi earlier JOIN keu_jurnal ej ON ej.id=earlier.jurnal_id JOIN keu_jurnal sj ON sj.id=stock.jurnal_id JOIN keu_stok_mutasi outrow ON outrow.id=NEW.keluar_id JOIN keu_jurnal oj ON oj.id=outrow.jurnal_id
   WHERE earlier.jenis='masuk' AND earlier.produk_id=stock.produk_id AND ej.status='terkirim' AND ej.tanggal<=oj.tanggal
   AND (ej.tanggal,ej.created_at,earlier.id)<(sj.tanggal,sj.created_at,stock.id)
   AND earlier.qty>COALESCE((SELECT sum(u.qty) FROM keu_stok_pemakaian u JOIN keu_stok_mutasi o ON o.id=u.keluar_id JOIN keu_jurnal j ON j.id=o.jurnal_id WHERE u.masuk_id=earlier.id AND j.status='terkirim'),0)) THEN RAISE EXCEPTION 'Consume the oldest available FIFO batch first' USING ERRCODE='23514'; END IF;
  expected:=CASE WHEN consumed+NEW.qty=stock.qty THEN stock.nilai-cost ELSE round((stock.nilai-cost)*NEW.qty/(stock.qty-consumed),2) END;
  IF NEW.nilai<>expected THEN RAISE EXCEPTION 'FIFO carrying cost mismatch' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END;
$keu$""",
    """DROP TRIGGER IF EXISTS keu_stock_guard ON keu_stok_mutasi""",
    """CREATE TRIGGER keu_stock_guard BEFORE INSERT OR UPDATE OR DELETE ON keu_stok_mutasi FOR EACH ROW EXECUTE FUNCTION keu_stock_guard()""",
    """DROP TRIGGER IF EXISTS keu_stock_guard ON keu_stok_pemakaian""",
    """CREATE TRIGGER keu_stock_guard BEFORE INSERT OR UPDATE OR DELETE ON keu_stok_pemakaian FOR EACH ROW EXECUTE FUNCTION keu_stock_guard()""",
    """CREATE OR REPLACE FUNCTION keu_stock_usage_check() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
DECLARE jid text; movement keu_stok_mutasi%ROWTYPE; n bigint; value numeric;
BEGIN
 SELECT * INTO movement FROM keu_stok_mutasi WHERE id=NEW.id;
 IF movement.jenis='keluar' THEN
  SELECT COALESCE(sum(qty),0),COALESCE(sum(nilai),0) INTO n,value FROM keu_stok_pemakaian WHERE keluar_id=movement.id;
  IF n<>movement.qty OR value<>movement.nilai THEN RAISE EXCEPTION 'FIFO allocations do not reconcile with stock movement' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NULL;
END;
$keu$""",
    """DROP TRIGGER IF EXISTS keu_stock_usage_check ON keu_stok_mutasi""",
    """CREATE CONSTRAINT TRIGGER keu_stock_usage_check AFTER INSERT ON keu_stok_mutasi DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION keu_stock_usage_check()""",
]


def allocation_guard():
    from .keu_vendor_migration import allocation_guard as prior
    return prior().replace("IF allocated + (CASE", "SELECT allocated+COALESCE(sum(s.qty),0) INTO allocated FROM keu_stok_mutasi s JOIN keu_jurnal j ON j.id=s.jurnal_id WHERE s.item_id=NEW.item_id AND s.jenis='keluar' AND j.status='terkirim';\n IF allocated + (CASE")


def statements():
    dialect = postgresql.dialect()
    parts = []
    for table in sort_tables([model.__table__ for model in FINANCE_MODELS]):
        parts.append(str(CreateTable(table, if_not_exists=True).compile(dialect=dialect)).strip())
        for index in sorted(table.indexes, key=lambda i: i.name):
            parts.append(str(CreateIndex(index, if_not_exists=True).compile(dialect=dialect)).strip())
    from .keu_migration import INVARIANTS
    capacity = next(stmt for stmt in INVARIANTS if stmt.startswith("CREATE OR REPLACE FUNCTION keu_item_capacity_guard"))
    capacity = capacity.replace(" IF allocated > NEW.qty", " SELECT allocated+COALESCE(sum(s.qty),0) INTO allocated FROM keu_stok_mutasi s JOIN keu_jurnal j ON j.id=s.jurnal_id WHERE s.item_id=NEW.id AND s.jenis='keluar' AND j.status='terkirim';\n IF allocated > NEW.qty")
    return [*parts, *GUARDS, allocation_guard(), capacity]


def sql():
    from .keu_unpost_migration import VERSION as previous
    parts = ["BEGIN", "SET LOCAL lock_timeout = '5s'", "SET LOCAL statement_timeout = '120s'", "SELECT pg_advisory_xact_lock(61062026)",
        "DO $$ BEGIN IF to_regclass('bl_users') IS NULL OR to_regclass('keu_transaksi') IS NULL OR to_regclass('keu_schema_versions') IS NULL THEN RAISE EXCEPTION 'Target is not an initialized BUMI keu tenant'; END IF; END $$",
        f"DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM keu_schema_versions WHERE version='{previous}') THEN RAISE EXCEPTION 'Apply keu_unpost.sql first'; END IF; END $$",
        *statements(), f"INSERT INTO keu_schema_versions(version) VALUES('{VERSION}') ON CONFLICT DO NOTHING", "COMMIT"]
    return "\n\n".join(part.strip()+";" for part in parts)+"\n"


async def ensure_finance_schema(conn):
    if conn.dialect.name != "postgresql":
        await conn.run_sync(lambda sync: FINANCE_MODELS[0].metadata.create_all(sync, tables=[m.__table__ for m in FINANCE_MODELS]))
        return
    await conn.execute(text("SELECT pg_advisory_xact_lock(61062026)"))
    if (await conn.execute(text("SELECT 1 FROM keu_schema_versions WHERE version=:v"), {"v": VERSION})).first():
        return
    for statement in statements():
        await conn.execute(text(statement))
    await conn.execute(text("INSERT INTO keu_schema_versions(version) VALUES(:v)"), {"v": VERSION})
