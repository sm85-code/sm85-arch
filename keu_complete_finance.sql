BEGIN;

SET LOCAL lock_timeout = '5s';

SET LOCAL statement_timeout = '120s';

SELECT pg_advisory_xact_lock(61062026);

DO $$ BEGIN IF to_regclass('bl_users') IS NULL OR to_regclass('keu_transaksi') IS NULL OR to_regclass('keu_schema_versions') IS NULL THEN RAISE EXCEPTION 'Target is not an initialized BUMI keu tenant'; END IF; END $$;

DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM keu_schema_versions WHERE version='20261009_keu_unpost') THEN RAISE EXCEPTION 'Apply keu_unpost.sql first'; END IF; END $$;

CREATE TABLE IF NOT EXISTS keu_coa (
	id VARCHAR(64) NOT NULL, 
	kode VARCHAR(64) NOT NULL, 
	nama VARCHAR(128) NOT NULL, 
	jenis VARCHAR(16) NOT NULL, 
	kelompok VARCHAR(64) NOT NULL, 
	kas_akun_id VARCHAR(64), 
	sistem BOOLEAN DEFAULT 'false' NOT NULL, 
	aktif BOOLEAN DEFAULT 'true' NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_keu_coa_jenis CHECK (jenis IN ('aset','kewajiban','ekuitas','pendapatan','beban')), 
	UNIQUE (kode), 
	UNIQUE (kas_akun_id), 
	FOREIGN KEY(kas_akun_id) REFERENCES keu_akun (id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS keu_buku (
	id VARCHAR(64) NOT NULL, 
	tanggal_awal DATE NOT NULL, 
	status VARCHAR(16) DEFAULT 'migrasi' NOT NULL, 
	metode_stok VARCHAR(16) NOT NULL, 
	tanggal_status VARCHAR(16) NOT NULL, 
	dibuat_oleh VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_keu_buku_status CHECK (status IN ('migrasi','aktif')), 
	CONSTRAINT ck_keu_buku_tunggal CHECK (id='utama'), 
	CONSTRAINT ck_keu_buku_stok CHECK (metode_stok='fifo'), 
	CONSTRAINT ck_keu_buku_tanggal CHECK (tanggal_status='updated_at'), 
	FOREIGN KEY(dibuat_oleh) REFERENCES bl_users (id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS keu_jurnal (
	id VARCHAR(64) NOT NULL, 
	sumber_key VARCHAR(512) NOT NULL, 
	jenis VARCHAR(32) NOT NULL, 
	tanggal DATE NOT NULL, 
	status VARCHAR(16) DEFAULT 'draf' NOT NULL, 
	keterangan TEXT DEFAULT '' NOT NULL, 
	rincian JSONB DEFAULT '{}' NOT NULL, 
	transaksi_id VARCHAR(64), 
	settlement_id VARCHAR(64), 
	pesanan_id VARCHAR(64), 
	dibuat_oleh VARCHAR(64) NOT NULL, 
	alasan_batal TEXT DEFAULT '' NOT NULL, 
	dibatalkan_oleh VARCHAR(64), 
	dibatalkan_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_keu_jurnal_status CHECK (status IN ('draf','terkirim','dibatalkan')), 
	CONSTRAINT ck_keu_jurnal_batal CHECK (status<>'dibatalkan' OR (length(trim(alasan_batal))>=3 AND dibatalkan_oleh IS NOT NULL AND dibatalkan_at IS NOT NULL)), 
	UNIQUE (sumber_key), 
	UNIQUE (transaksi_id), 
	FOREIGN KEY(transaksi_id) REFERENCES keu_transaksi (id) ON DELETE RESTRICT, 
	UNIQUE (settlement_id), 
	FOREIGN KEY(settlement_id) REFERENCES keu_settlement (id) ON DELETE RESTRICT, 
	FOREIGN KEY(pesanan_id) REFERENCES keu_pesanan (id) ON DELETE RESTRICT, 
	FOREIGN KEY(dibuat_oleh) REFERENCES bl_users (id) ON DELETE RESTRICT, 
	FOREIGN KEY(dibatalkan_oleh) REFERENCES bl_users (id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_keu_jurnal_pesanan_id ON keu_jurnal (pesanan_id);

CREATE INDEX IF NOT EXISTS ix_keu_jurnal_tanggal ON keu_jurnal (tanggal);

CREATE TABLE IF NOT EXISTS keu_kategori_coa (
	id VARCHAR(64) NOT NULL, 
	coa_id VARCHAR(64) NOT NULL, 
	arus VARCHAR(16) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_keu_kategori_arus CHECK (arus IN ('operasional','investasi','pendanaan')), 
	FOREIGN KEY(id) REFERENCES bl_kategori (id) ON DELETE RESTRICT, 
	FOREIGN KEY(coa_id) REFERENCES keu_coa (id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS keu_jurnal_baris (
	id VARCHAR(64) NOT NULL, 
	jurnal_id VARCHAR(64) NOT NULL, 
	nomor INTEGER NOT NULL, 
	coa_id VARCHAR(64) NOT NULL, 
	debet NUMERIC(20, 2) DEFAULT '0' NOT NULL, 
	kredit NUMERIC(20, 2) DEFAULT '0' NOT NULL, 
	arus VARCHAR(16), 
	pesanan_id VARCHAR(64), 
	vendor_id VARCHAR(64), 
	produk_id VARCHAR(64), 
	PRIMARY KEY (id), 
	CONSTRAINT uq_keu_jurnal_baris UNIQUE (jurnal_id, nomor), 
	CONSTRAINT ck_keu_baris_uang CHECK (nomor>0 AND ((debet>0 AND kredit=0) OR (kredit>0 AND debet=0))), 
	CONSTRAINT ck_keu_baris_arus CHECK (arus IS NULL OR arus IN ('operasional','investasi','pendanaan','mutasi')), 
	FOREIGN KEY(jurnal_id) REFERENCES keu_jurnal (id) ON DELETE RESTRICT, 
	FOREIGN KEY(coa_id) REFERENCES keu_coa (id) ON DELETE RESTRICT, 
	FOREIGN KEY(pesanan_id) REFERENCES keu_pesanan (id) ON DELETE RESTRICT, 
	FOREIGN KEY(vendor_id) REFERENCES keu_vendor (id) ON DELETE RESTRICT, 
	FOREIGN KEY(produk_id) REFERENCES keu_produk (id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_keu_jurnal_baris_coa_id ON keu_jurnal_baris (coa_id);

CREATE INDEX IF NOT EXISTS ix_keu_jurnal_baris_jurnal_id ON keu_jurnal_baris (jurnal_id);

CREATE INDEX IF NOT EXISTS ix_keu_jurnal_baris_pesanan_id ON keu_jurnal_baris (pesanan_id);

CREATE INDEX IF NOT EXISTS ix_keu_jurnal_baris_produk_id ON keu_jurnal_baris (produk_id);

CREATE INDEX IF NOT EXISTS ix_keu_jurnal_baris_vendor_id ON keu_jurnal_baris (vendor_id);

CREATE TABLE IF NOT EXISTS keu_stok_mutasi (
	id VARCHAR(64) NOT NULL, 
	jurnal_id VARCHAR(64) NOT NULL, 
	produk_id VARCHAR(64) NOT NULL, 
	item_id VARCHAR(64), 
	jenis VARCHAR(16) NOT NULL, 
	qty INTEGER NOT NULL, 
	nilai NUMERIC(20, 2) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_keu_stok_mutasi CHECK (jenis IN ('masuk','keluar') AND qty>0 AND nilai>=0), 
	UNIQUE (jurnal_id), 
	FOREIGN KEY(jurnal_id) REFERENCES keu_jurnal (id) ON DELETE RESTRICT, 
	FOREIGN KEY(produk_id) REFERENCES keu_produk (id) ON DELETE RESTRICT, 
	FOREIGN KEY(item_id) REFERENCES keu_item (id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_keu_stok_mutasi_item_id ON keu_stok_mutasi (item_id);

CREATE INDEX IF NOT EXISTS ix_keu_stok_mutasi_produk_id ON keu_stok_mutasi (produk_id);

CREATE TABLE IF NOT EXISTS keu_stok_pemakaian (
	id VARCHAR(64) NOT NULL, 
	keluar_id VARCHAR(64) NOT NULL, 
	masuk_id VARCHAR(64) NOT NULL, 
	qty INTEGER NOT NULL, 
	nilai NUMERIC(20, 2) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_keu_stok_pemakaian UNIQUE (keluar_id, masuk_id), 
	CONSTRAINT ck_keu_stok_pemakaian CHECK (qty>0 AND nilai>=0 AND keluar_id<>masuk_id), 
	FOREIGN KEY(keluar_id) REFERENCES keu_stok_mutasi (id) ON DELETE RESTRICT, 
	FOREIGN KEY(masuk_id) REFERENCES keu_stok_mutasi (id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_keu_stok_pemakaian_keluar_id ON keu_stok_pemakaian (keluar_id);

CREATE INDEX IF NOT EXISTS ix_keu_stok_pemakaian_masuk_id ON keu_stok_pemakaian (masuk_id);

CREATE OR REPLACE FUNCTION keu_journal_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
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
$keu$;

DROP TRIGGER IF EXISTS keu_journal_guard ON keu_jurnal;

CREATE TRIGGER keu_journal_guard BEFORE INSERT OR UPDATE OR DELETE ON keu_jurnal FOR EACH ROW EXECUTE FUNCTION keu_journal_guard();

CREATE OR REPLACE FUNCTION keu_journal_line_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
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
$keu$;

DROP TRIGGER IF EXISTS keu_journal_line_guard ON keu_jurnal_baris;

CREATE TRIGGER keu_journal_line_guard BEFORE INSERT OR UPDATE OR DELETE ON keu_jurnal_baris FOR EACH ROW EXECUTE FUNCTION keu_journal_line_guard();

CREATE OR REPLACE FUNCTION keu_journal_balance_check() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
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
$keu$;

DROP TRIGGER IF EXISTS keu_journal_balance_check ON keu_jurnal;

CREATE CONSTRAINT TRIGGER keu_journal_balance_check AFTER INSERT OR UPDATE ON keu_jurnal DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION keu_journal_balance_check();

CREATE OR REPLACE FUNCTION keu_finance_master_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
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
$keu$;

DROP TRIGGER IF EXISTS keu_finance_master_guard ON keu_coa;

CREATE TRIGGER keu_finance_master_guard BEFORE UPDATE OR DELETE ON keu_coa FOR EACH ROW EXECUTE FUNCTION keu_finance_master_guard();

DROP TRIGGER IF EXISTS keu_finance_master_guard ON keu_buku;

CREATE TRIGGER keu_finance_master_guard BEFORE UPDATE OR DELETE ON keu_buku FOR EACH ROW EXECUTE FUNCTION keu_finance_master_guard();

CREATE OR REPLACE FUNCTION keu_stock_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
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
$keu$;

DROP TRIGGER IF EXISTS keu_stock_guard ON keu_stok_mutasi;

CREATE TRIGGER keu_stock_guard BEFORE INSERT OR UPDATE OR DELETE ON keu_stok_mutasi FOR EACH ROW EXECUTE FUNCTION keu_stock_guard();

DROP TRIGGER IF EXISTS keu_stock_guard ON keu_stok_pemakaian;

CREATE TRIGGER keu_stock_guard BEFORE INSERT OR UPDATE OR DELETE ON keu_stok_pemakaian FOR EACH ROW EXECUTE FUNCTION keu_stock_guard();

CREATE OR REPLACE FUNCTION keu_stock_usage_check() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
DECLARE jid text; movement keu_stok_mutasi%ROWTYPE; n bigint; value numeric;
BEGIN
 SELECT * INTO movement FROM keu_stok_mutasi WHERE id=NEW.id;
 IF movement.jenis='keluar' THEN
  SELECT COALESCE(sum(qty),0),COALESCE(sum(nilai),0) INTO n,value FROM keu_stok_pemakaian WHERE keluar_id=movement.id;
  IF n<>movement.qty OR value<>movement.nilai THEN RAISE EXCEPTION 'FIFO allocations do not reconcile with stock movement' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NULL;
END;
$keu$;

DROP TRIGGER IF EXISTS keu_stock_usage_check ON keu_stok_mutasi;

CREATE CONSTRAINT TRIGGER keu_stock_usage_check AFTER INSERT ON keu_stok_mutasi DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION keu_stock_usage_check();

CREATE OR REPLACE FUNCTION keu_allocation_check() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
DECLARE capacity integer; allocated bigint; product_kind text; vendor_kind text; enabled boolean; product_state text; product_enabled boolean;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Cancel allocation instead of deleting it' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' AND (NEW.item_id IS DISTINCT FROM OLD.item_id OR NEW.vendor_id IS DISTINCT FROM OLD.vendor_id) THEN
  RAISE EXCEPTION 'Allocation identity is immutable' USING ERRCODE='23514';
 END IF;
 SELECT i.qty,p.jenis,p.status,p.aktif INTO capacity,product_kind,product_state,product_enabled FROM keu_item i LEFT JOIN keu_produk p ON p.id=i.produk_id WHERE i.id=NEW.item_id FOR UPDATE OF i;
 IF NOT FOUND THEN RAISE EXCEPTION 'Item not found' USING ERRCODE='23503'; END IF;
 SELECT jenis,aktif INTO vendor_kind,enabled FROM keu_vendor WHERE id=NEW.vendor_id FOR SHARE;
 IF NOT NEW.dibatalkan AND (product_kind IS NULL OR product_state IS DISTINCT FROM 'master' OR product_enabled IS DISTINCT FROM TRUE OR enabled IS DISTINCT FROM TRUE OR (product_kind='kayu' AND vendor_kind IS DISTINCT FROM 'tukang_kayu') OR (product_kind='non_kayu' AND vendor_kind IS DISTINCT FROM 'supplier')) THEN
  RAISE EXCEPTION 'Choose an active vendor for a mapped product' USING ERRCODE='23514';
 END IF;
 SELECT COALESCE(sum(qty),0) INTO allocated FROM keu_alokasi_vendor WHERE item_id=NEW.item_id AND NOT dibatalkan AND id<>NEW.id;
 SELECT allocated+COALESCE(sum(s.qty),0) INTO allocated FROM keu_stok_mutasi s JOIN keu_jurnal j ON j.id=s.jurnal_id WHERE s.item_id=NEW.item_id AND s.jenis='keluar' AND j.status='terkirim';
 IF allocated + (CASE WHEN NEW.dibatalkan THEN 0 ELSE NEW.qty END) > capacity THEN RAISE EXCEPTION 'Allocation exceeds item quantity' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END;
$keu$;

CREATE OR REPLACE FUNCTION keu_item_capacity_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
DECLARE allocated bigint;
BEGIN
 SELECT COALESCE(sum(qty),0) INTO allocated FROM keu_alokasi_vendor WHERE item_id=NEW.id AND NOT dibatalkan;
 SELECT allocated+COALESCE(sum(s.qty),0) INTO allocated FROM keu_stok_mutasi s JOIN keu_jurnal j ON j.id=s.jurnal_id WHERE s.item_id=NEW.id AND s.jenis='keluar' AND j.status='terkirim';
 IF allocated > NEW.qty OR (allocated>0 AND NEW.produk_id IS DISTINCT FROM OLD.produk_id) THEN RAISE EXCEPTION 'Cancel allocations before changing item capacity/product' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END;
$keu$;

INSERT INTO keu_schema_versions(version) VALUES('20261010_keu_complete_finance') ON CONFLICT DO NOTHING;

COMMIT;
