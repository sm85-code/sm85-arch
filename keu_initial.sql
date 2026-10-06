BEGIN;

SET LOCAL lock_timeout = '5s';

SET LOCAL statement_timeout = '120s';

SELECT pg_advisory_xact_lock(61062026);

DO $$ BEGIN IF to_regclass('bl_users') IS NULL OR to_regclass('bl_kategori') IS NULL THEN RAISE EXCEPTION 'Target is not an initialized BUMI tenant'; END IF; END $$;

CREATE TABLE IF NOT EXISTS keu_schema_versions (version varchar(64) PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());

DO $$ BEGIN IF EXISTS (SELECT 1 FROM keu_schema_versions WHERE version = '20261006_keu_initial') THEN RAISE EXCEPTION 'Migration already applied'; END IF; END $$;

CREATE TABLE keu_saluran (
	id VARCHAR(64) NOT NULL, 
	nama VARCHAR(128) NOT NULL, 
	sistem VARCHAR(32) NOT NULL, 
	akun_ref VARCHAR(128) NOT NULL, 
	aktif BOOLEAN DEFAULT false NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_keu_saluran_sumber UNIQUE (sistem, akun_ref), 
	CONSTRAINT ck_keu_saluran_sistem CHECK (sistem IN ('store', 'marketplace_erp', 'manual')), 
	CONSTRAINT ck_keu_saluran_ref CHECK (length(trim(akun_ref)) > 0)
);

CREATE TABLE keu_akun (
	id VARCHAR(64) NOT NULL, 
	kode VARCHAR(64) NOT NULL, 
	nama VARCHAR(128) NOT NULL, 
	jenis VARCHAR(16) NOT NULL, 
	saldo_awal NUMERIC(20, 2) DEFAULT '0' NOT NULL, 
	aktif BOOLEAN DEFAULT 'true' NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_keu_akun_jenis CHECK (jenis IN ('kas', 'bank', 'ewallet')), 
	UNIQUE (kode)
);

CREATE TABLE keu_pelanggan (
	id VARCHAR(64) NOT NULL, 
	nama VARCHAR(255) NOT NULL, 
	segmen VARCHAR(16), 
	kontak VARCHAR(255) DEFAULT '' NOT NULL, 
	aktif BOOLEAN DEFAULT 'true' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_keu_pelanggan_segmen CHECK (segmen IS NULL OR segmen IN ('umkm', 'reseller'))
);

CREATE TABLE keu_vendor (
	id VARCHAR(64) NOT NULL, 
	nama VARCHAR(255) NOT NULL, 
	jenis VARCHAR(16) NOT NULL, 
	kontak VARCHAR(255) DEFAULT '' NOT NULL, 
	aktif BOOLEAN DEFAULT 'true' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_keu_vendor_jenis CHECK (jenis IN ('tukang_kayu', 'supplier'))
);

CREATE TABLE keu_produk (
	id VARCHAR(64) NOT NULL, 
	sku VARCHAR(128) NOT NULL, 
	nama VARCHAR(255) NOT NULL, 
	jenis VARCHAR(16) NOT NULL, 
	biaya_acuan NUMERIC(20, 2) DEFAULT '0' NOT NULL, 
	aktif BOOLEAN DEFAULT 'true' NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_keu_produk_jenis CHECK (jenis IN ('kayu', 'non_kayu')), 
	CONSTRAINT ck_keu_produk_biaya CHECK (biaya_acuan >= 0), 
	UNIQUE (sku)
);

CREATE TABLE keu_vendor_slot (
	jenis VARCHAR(16) NOT NULL, 
	nomor INTEGER NOT NULL, 
	vendor_id VARCHAR(64), 
	PRIMARY KEY (jenis, nomor), 
	CONSTRAINT uq_keu_vendor_slot_vendor UNIQUE (vendor_id), 
	CONSTRAINT ck_keu_vendor_slot CHECK ((jenis = 'tukang_kayu' AND nomor BETWEEN 1 AND 5) OR (jenis = 'supplier' AND nomor BETWEEN 1 AND 3)), 
	FOREIGN KEY(vendor_id) REFERENCES keu_vendor (id) ON DELETE RESTRICT
);

CREATE TABLE keu_pesanan (
	id VARCHAR(64) NOT NULL, 
	saluran_id VARCHAR(64) NOT NULL, 
	sumber_ref VARCHAR(255) NOT NULL, 
	nomor VARCHAR(128) NOT NULL, 
	tanggal DATE NOT NULL, 
	pelanggan_id VARCHAR(64), 
	segmen_snapshot VARCHAR(16), 
	status_sumber VARCHAR(64) NOT NULL, 
	status VARCHAR(16) DEFAULT 'draf' NOT NULL, 
	total_sumber NUMERIC(20, 2) NOT NULL, 
	sumber_updated_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_keu_pesanan_sumber UNIQUE (saluran_id, sumber_ref), 
	CONSTRAINT ck_keu_pesanan_segmen CHECK (segmen_snapshot IS NULL OR segmen_snapshot IN ('umkm', 'reseller')), 
	CONSTRAINT ck_keu_pesanan_status CHECK (status IN ('draf', 'aktif', 'selesai', 'batal')), 
	CONSTRAINT ck_keu_pesanan_total CHECK (total_sumber >= 0), 
	FOREIGN KEY(saluran_id) REFERENCES keu_saluran (id) ON DELETE RESTRICT, 
	FOREIGN KEY(pelanggan_id) REFERENCES keu_pelanggan (id) ON DELETE RESTRICT
);

CREATE INDEX ix_keu_pesanan_saluran_id ON keu_pesanan (saluran_id);

CREATE INDEX ix_keu_pesanan_tanggal ON keu_pesanan (tanggal);

CREATE TABLE keu_settlement (
	id VARCHAR(64) NOT NULL, 
	saluran_id VARCHAR(64) NOT NULL, 
	sumber_ref VARCHAR(255) NOT NULL, 
	tanggal_cair DATE NOT NULL, 
	bruto NUMERIC(20, 2) NOT NULL, 
	potongan NUMERIC(20, 2) NOT NULL, 
	penyesuaian NUMERIC(20, 2) DEFAULT '0' NOT NULL, 
	neto NUMERIC(20, 2) NOT NULL, 
	rincian JSONB DEFAULT '{}' NOT NULL, 
	status VARCHAR(16) DEFAULT 'draf' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_keu_settlement_sumber UNIQUE (saluran_id, sumber_ref), 
	CONSTRAINT ck_keu_settlement_rekonsiliasi CHECK (neto = bruto - potongan + penyesuaian), 
	CONSTRAINT ck_keu_settlement_status CHECK (status IN ('draf', 'terkirim', 'dibatalkan')), 
	FOREIGN KEY(saluran_id) REFERENCES keu_saluran (id) ON DELETE RESTRICT
);

CREATE INDEX ix_keu_settlement_saluran_id ON keu_settlement (saluran_id);

CREATE INDEX ix_keu_settlement_tanggal_cair ON keu_settlement (tanggal_cair);

CREATE TABLE keu_impor (
	id VARCHAR(64) NOT NULL, 
	saluran_id VARCHAR(64) NOT NULL, 
	jenis VARCHAR(16) NOT NULL, 
	nama_file VARCHAR(255) NOT NULL, 
	file_sha256 VARCHAR(64) NOT NULL, 
	versi_format INTEGER DEFAULT '1' NOT NULL, 
	pemetaan JSONB DEFAULT '{}' NOT NULL, 
	status VARCHAR(16) DEFAULT 'draf' NOT NULL, 
	dibuat_oleh VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_keu_impor_file UNIQUE (saluran_id, jenis, file_sha256, versi_format), 
	CONSTRAINT ck_keu_impor_jenis CHECK (jenis IN ('order', 'settlement', 'biaya')), 
	CONSTRAINT ck_keu_impor_status CHECK (status IN ('draf', 'valid', 'diterapkan', 'ditolak')), 
	CONSTRAINT ck_keu_impor_format CHECK (length(file_sha256) = 64 AND versi_format > 0), 
	FOREIGN KEY(saluran_id) REFERENCES keu_saluran (id) ON DELETE RESTRICT, 
	FOREIGN KEY(dibuat_oleh) REFERENCES bl_users (id) ON DELETE RESTRICT
);

CREATE TABLE keu_cursor (
	saluran_id VARCHAR(64) NOT NULL, 
	entitas VARCHAR(16) NOT NULL, 
	watermark_at TIMESTAMP WITH TIME ZONE, 
	watermark_ref VARCHAR(255), 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (saluran_id, entitas), 
	CONSTRAINT ck_keu_cursor_entitas CHECK (entitas IN ('produk', 'order', 'settlement')), 
	FOREIGN KEY(saluran_id) REFERENCES keu_saluran (id) ON DELETE RESTRICT
);

CREATE TABLE keu_item (
	id VARCHAR(64) NOT NULL, 
	pesanan_id VARCHAR(64) NOT NULL, 
	sumber_ref VARCHAR(255) NOT NULL, 
	produk_id VARCHAR(64), 
	nama_snapshot VARCHAR(255) NOT NULL, 
	varian_snapshot VARCHAR(255) DEFAULT '' NOT NULL, 
	qty INTEGER NOT NULL, 
	harga_satuan NUMERIC(20, 2) NOT NULL, 
	subtotal_sumber NUMERIC(20, 2) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_keu_item_sumber UNIQUE (pesanan_id, sumber_ref), 
	CONSTRAINT ck_keu_item_qty CHECK (qty > 0), 
	CONSTRAINT ck_keu_item_uang CHECK (harga_satuan >= 0 AND subtotal_sumber >= 0), 
	FOREIGN KEY(pesanan_id) REFERENCES keu_pesanan (id) ON DELETE RESTRICT, 
	FOREIGN KEY(produk_id) REFERENCES keu_produk (id) ON DELETE RESTRICT
);

CREATE INDEX ix_keu_item_pesanan_id ON keu_item (pesanan_id);

CREATE TABLE keu_transaksi (
	id VARCHAR(64) NOT NULL, 
	saluran_id VARCHAR(64) NOT NULL, 
	sumber_ref VARCHAR(255) NOT NULL, 
	akun_id VARCHAR(64) NOT NULL, 
	kategori_id VARCHAR(64) NOT NULL, 
	settlement_id VARCHAR(64), 
	tanggal DATE NOT NULL, 
	jenis VARCHAR(16) NOT NULL, 
	jumlah NUMERIC(20, 2) NOT NULL, 
	status VARCHAR(16) DEFAULT 'draf' NOT NULL, 
	keterangan TEXT DEFAULT '' NOT NULL, 
	dibuat_oleh VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_keu_transaksi_sumber UNIQUE (saluran_id, sumber_ref), 
	CONSTRAINT ck_keu_transaksi_jenis CHECK (jenis IN ('masuk', 'keluar')), 
	CONSTRAINT ck_keu_transaksi_jumlah CHECK (jumlah > 0), 
	CONSTRAINT ck_keu_transaksi_status CHECK (status IN ('draf', 'terkirim', 'dibatalkan')), 
	FOREIGN KEY(saluran_id) REFERENCES keu_saluran (id) ON DELETE RESTRICT, 
	FOREIGN KEY(akun_id) REFERENCES keu_akun (id) ON DELETE RESTRICT, 
	FOREIGN KEY(kategori_id) REFERENCES bl_kategori (id) ON DELETE RESTRICT, 
	FOREIGN KEY(settlement_id) REFERENCES keu_settlement (id) ON DELETE RESTRICT, 
	FOREIGN KEY(dibuat_oleh) REFERENCES bl_users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_keu_transaksi_akun_id ON keu_transaksi (akun_id);

CREATE INDEX ix_keu_transaksi_tanggal ON keu_transaksi (tanggal);

CREATE TABLE keu_masukan (
	id VARCHAR(64) NOT NULL, 
	saluran_id VARCHAR(64) NOT NULL, 
	impor_id VARCHAR(64), 
	nomor_baris INTEGER, 
	entitas VARCHAR(16) NOT NULL, 
	sumber_ref VARCHAR(255) NOT NULL, 
	sumber_updated_at TIMESTAMP WITH TIME ZONE, 
	revisi_sha256 VARCHAR(64) NOT NULL, 
	payload JSONB NOT NULL, 
	kesalahan JSONB DEFAULT '[]' NOT NULL, 
	status VARCHAR(16) DEFAULT 'menunggu' NOT NULL, 
	percobaan INTEGER DEFAULT '0' NOT NULL, 
	coba_lagi_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_keu_masukan_revisi UNIQUE (saluran_id, entitas, sumber_ref, revisi_sha256), 
	CONSTRAINT uq_keu_masukan_baris UNIQUE (impor_id, nomor_baris), 
	CONSTRAINT ck_keu_masukan_asal CHECK ((impor_id IS NULL AND nomor_baris IS NULL) OR (impor_id IS NOT NULL AND nomor_baris IS NOT NULL AND nomor_baris > 0)), 
	CONSTRAINT ck_keu_masukan_entitas CHECK (entitas IN ('produk', 'order', 'settlement', 'biaya')), 
	CONSTRAINT ck_keu_masukan_status CHECK (status IN ('menunggu', 'terproses', 'gagal', 'diabaikan')), 
	CONSTRAINT ck_keu_masukan_revisi CHECK (length(revisi_sha256) = 64 AND percobaan >= 0), 
	FOREIGN KEY(saluran_id) REFERENCES keu_saluran (id) ON DELETE RESTRICT, 
	FOREIGN KEY(impor_id) REFERENCES keu_impor (id) ON DELETE RESTRICT
);

CREATE INDEX ix_keu_masukan_antrian ON keu_masukan (status, coba_lagi_at);

CREATE TABLE keu_alokasi_vendor (
	id VARCHAR(64) NOT NULL, 
	item_id VARCHAR(64) NOT NULL, 
	vendor_id VARCHAR(64) NOT NULL, 
	qty INTEGER NOT NULL, 
	biaya_satuan NUMERIC(20, 2) NOT NULL, 
	dibatalkan BOOLEAN DEFAULT false NOT NULL, 
	alasan TEXT DEFAULT '' NOT NULL, 
	dibuat_oleh VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_keu_alokasi_vendor UNIQUE (item_id, vendor_id), 
	CONSTRAINT ck_keu_alokasi_vendor_nilai CHECK (qty > 0 AND biaya_satuan >= 0), 
	CONSTRAINT ck_keu_alokasi_vendor_batal CHECK (NOT dibatalkan OR length(trim(alasan)) >= 3), 
	FOREIGN KEY(item_id) REFERENCES keu_item (id) ON DELETE RESTRICT, 
	FOREIGN KEY(vendor_id) REFERENCES keu_vendor (id) ON DELETE RESTRICT, 
	FOREIGN KEY(dibuat_oleh) REFERENCES bl_users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_keu_alokasi_vendor_item_id ON keu_alokasi_vendor (item_id);

CREATE INDEX ix_keu_alokasi_vendor_vendor_id ON keu_alokasi_vendor (vendor_id);

CREATE TABLE keu_alokasi_settlement (
	id VARCHAR(64) NOT NULL, 
	settlement_id VARCHAR(64) NOT NULL, 
	item_id VARCHAR(64) NOT NULL, 
	jumlah NUMERIC(20, 2) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_keu_alokasi_settlement UNIQUE (settlement_id, item_id), 
	FOREIGN KEY(settlement_id) REFERENCES keu_settlement (id) ON DELETE RESTRICT, 
	FOREIGN KEY(item_id) REFERENCES keu_item (id) ON DELETE RESTRICT
);

CREATE INDEX ix_keu_alokasi_settlement_item_id ON keu_alokasi_settlement (item_id);

CREATE INDEX ix_keu_alokasi_settlement_settlement_id ON keu_alokasi_settlement (settlement_id);

INSERT INTO keu_vendor_slot (jenis, nomor) SELECT 'tukang_kayu', n FROM generate_series(1, 5) n;

INSERT INTO keu_vendor_slot (jenis, nomor) SELECT 'supplier', n FROM generate_series(1, 3) n;

CREATE OR REPLACE FUNCTION keu_vendor_slot_check() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
DECLARE kind text;
BEGIN
 IF NEW.vendor_id IS NOT NULL THEN
  SELECT jenis INTO kind FROM keu_vendor WHERE id=NEW.vendor_id FOR SHARE;
  IF kind IS DISTINCT FROM NEW.jenis THEN RAISE EXCEPTION 'Vendor type does not match slot' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END;
$keu$;

DROP TRIGGER IF EXISTS keu_vendor_slot_check ON keu_vendor_slot;

CREATE TRIGGER keu_vendor_slot_check BEFORE INSERT OR UPDATE ON keu_vendor_slot FOR EACH ROW EXECUTE FUNCTION keu_vendor_slot_check();

CREATE OR REPLACE FUNCTION keu_vendor_type_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
BEGIN
 IF NEW.jenis IS DISTINCT FROM OLD.jenis AND (EXISTS (SELECT 1 FROM keu_vendor_slot WHERE vendor_id=OLD.id) OR EXISTS (SELECT 1 FROM keu_alokasi_vendor WHERE vendor_id=OLD.id)) THEN
  RAISE EXCEPTION 'Referenced vendor type is immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$keu$;

DROP TRIGGER IF EXISTS keu_vendor_type_guard ON keu_vendor;

CREATE TRIGGER keu_vendor_type_guard BEFORE UPDATE OF jenis ON keu_vendor FOR EACH ROW EXECUTE FUNCTION keu_vendor_type_guard();

CREATE OR REPLACE FUNCTION keu_allocation_check() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
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
$keu$;

DROP TRIGGER IF EXISTS keu_allocation_check ON keu_alokasi_vendor;

CREATE TRIGGER keu_allocation_check BEFORE INSERT OR UPDATE OR DELETE ON keu_alokasi_vendor FOR EACH ROW EXECUTE FUNCTION keu_allocation_check();

CREATE OR REPLACE FUNCTION keu_item_capacity_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
DECLARE allocated bigint;
BEGIN
 SELECT COALESCE(sum(qty),0) INTO allocated FROM keu_alokasi_vendor WHERE item_id=NEW.id AND NOT dibatalkan;
 IF allocated > NEW.qty OR (allocated>0 AND NEW.produk_id IS DISTINCT FROM OLD.produk_id) THEN RAISE EXCEPTION 'Cancel allocations before changing item capacity/product' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END;
$keu$;

DROP TRIGGER IF EXISTS keu_item_capacity_guard ON keu_item;

CREATE TRIGGER keu_item_capacity_guard BEFORE UPDATE OF qty, produk_id ON keu_item FOR EACH ROW EXECUTE FUNCTION keu_item_capacity_guard();

CREATE OR REPLACE FUNCTION keu_product_type_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
BEGIN
 IF NEW.jenis IS DISTINCT FROM OLD.jenis AND EXISTS (SELECT 1 FROM keu_item i JOIN keu_alokasi_vendor a ON a.item_id=i.id WHERE i.produk_id=OLD.id AND NOT a.dibatalkan) THEN RAISE EXCEPTION 'Allocated product type is immutable' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END;
$keu$;

DROP TRIGGER IF EXISTS keu_product_type_guard ON keu_produk;

CREATE TRIGGER keu_product_type_guard BEFORE UPDATE OF jenis ON keu_produk FOR EACH ROW EXECUTE FUNCTION keu_product_type_guard();

CREATE OR REPLACE FUNCTION keu_settlement_allocation_check() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
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
$keu$;

DROP TRIGGER IF EXISTS keu_settlement_allocation_check ON keu_alokasi_settlement;

CREATE TRIGGER keu_settlement_allocation_check BEFORE INSERT OR UPDATE OR DELETE ON keu_alokasi_settlement FOR EACH ROW EXECUTE FUNCTION keu_settlement_allocation_check();

CREATE OR REPLACE FUNCTION keu_settlement_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
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
$keu$;

DROP TRIGGER IF EXISTS keu_settlement_guard ON keu_settlement;

CREATE TRIGGER keu_settlement_guard BEFORE INSERT OR UPDATE OR DELETE ON keu_settlement FOR EACH ROW EXECUTE FUNCTION keu_settlement_guard();

CREATE OR REPLACE FUNCTION keu_transaction_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Transaction cannot be deleted' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' AND OLD.status<>'draf' AND NEW IS DISTINCT FROM OLD THEN RAISE EXCEPTION 'Posted transaction requires a separate correction' USING ERRCODE='23514'; END IF;
 IF NEW.status='terkirim' AND EXISTS (SELECT 1 FROM bl_tutup_buku WHERE periode=to_char(NEW.tanggal,'YYYY-MM') AND status='ditutup') THEN RAISE EXCEPTION 'Accounting period is closed' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END;
$keu$;

DROP TRIGGER IF EXISTS keu_transaction_guard ON keu_transaksi;

CREATE TRIGGER keu_transaction_guard BEFORE INSERT OR UPDATE OR DELETE ON keu_transaksi FOR EACH ROW EXECUTE FUNCTION keu_transaction_guard();

INSERT INTO keu_schema_versions(version) VALUES ('20261006_keu_initial');

COMMIT;
