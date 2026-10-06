BEGIN;

SET LOCAL lock_timeout = '5s';

SET LOCAL statement_timeout = '120s';

SELECT pg_advisory_xact_lock(61062026);

DO $$ BEGIN IF to_regclass('bl_users') IS NULL OR to_regclass('keu_transaksi') IS NULL OR to_regclass('keu_schema_versions') IS NULL THEN RAISE EXCEPTION 'Target is not an initialized BUMI keu tenant'; END IF; END $$;

DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM keu_schema_versions WHERE version='20261008_keu_dynamic_vendors') THEN RAISE EXCEPTION 'Apply keu_dynamic_vendors.sql first'; END IF; END $$;

ALTER TABLE keu_transaksi ADD COLUMN IF NOT EXISTS alasan_batal text NOT NULL DEFAULT '';

ALTER TABLE keu_transaksi ADD COLUMN IF NOT EXISTS dibatalkan_oleh varchar(64) REFERENCES bl_users(id) ON DELETE RESTRICT;

ALTER TABLE keu_transaksi ADD COLUMN IF NOT EXISTS dibatalkan_at timestamptz;

ALTER TABLE keu_settlement ADD COLUMN IF NOT EXISTS alasan_batal text NOT NULL DEFAULT '';

ALTER TABLE keu_settlement ADD COLUMN IF NOT EXISTS dibatalkan_oleh varchar(64) REFERENCES bl_users(id) ON DELETE RESTRICT;

ALTER TABLE keu_settlement ADD COLUMN IF NOT EXISTS dibatalkan_at timestamptz;

CREATE OR REPLACE FUNCTION keu_transaction_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$

BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Financial history cannot be deleted' USING ERRCODE='23514'; END IF;
 
 IF TG_OP='UPDATE' AND OLD.status='terkirim' AND NEW.status='dibatalkan' THEN
  IF (to_jsonb(NEW)-ARRAY['status','alasan_batal','dibatalkan_oleh','dibatalkan_at']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','alasan_batal','dibatalkan_oleh','dibatalkan_at']) THEN
   RAISE EXCEPTION 'Original posted entry is immutable' USING ERRCODE='23514';
  END IF;
  IF length(trim(NEW.alasan_batal)) NOT BETWEEN 3 AND 2000 OR NEW.dibatalkan_at IS NULL OR NOT EXISTS
   (SELECT 1 FROM bl_users WHERE id=NEW.dibatalkan_oleh AND role IN ('owner','admin') AND aktif AND NOT must_change_password) THEN
   RAISE EXCEPTION 'Cancellation requires reason and authorized actor' USING ERRCODE='23514';
  END IF;
  IF EXISTS (SELECT 1 FROM bl_tutup_buku WHERE periode=to_char(OLD.tanggal,'YYYY-MM') AND status='ditutup') THEN
   RAISE EXCEPTION 'Accounting period is closed' USING ERRCODE='23514';
  END IF;
  
  RETURN NEW;
 END IF;
 IF TG_OP='UPDATE' AND OLD.status<>'draf' AND NEW IS DISTINCT FROM OLD THEN RAISE EXCEPTION 'Posted or cancelled history is immutable' USING ERRCODE='23514'; END IF;
 IF NEW.status='dibatalkan' AND (TG_OP='INSERT' OR OLD.status='draf') THEN RAISE EXCEPTION 'Only posted entries can be unposted' USING ERRCODE='23514'; END IF;
 IF (TG_OP='INSERT' OR OLD.status='draf') AND (NEW.alasan_batal<>'' OR NEW.dibatalkan_oleh IS NOT NULL OR NEW.dibatalkan_at IS NOT NULL) THEN RAISE EXCEPTION 'Cancellation metadata is reserved for unpost' USING ERRCODE='23514'; END IF;
 IF NEW.status='terkirim' AND EXISTS (SELECT 1 FROM keu_settlement WHERE id=NEW.settlement_id AND status='dibatalkan') THEN RAISE EXCEPTION 'Settlement is cancelled' USING ERRCODE='23514'; END IF;
 IF NEW.status='terkirim' AND (TG_OP='INSERT' OR OLD.status<>'terkirim') THEN
  
  IF EXISTS (SELECT 1 FROM bl_tutup_buku WHERE periode=to_char(NEW.tanggal,'YYYY-MM') AND status='ditutup') THEN RAISE EXCEPTION 'Accounting period is closed' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END;
$keu$;

CREATE OR REPLACE FUNCTION keu_settlement_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
DECLARE allocated numeric;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Financial history cannot be deleted' USING ERRCODE='23514'; END IF;
 IF TG_OP='INSERT' AND NEW.status<>'draf' THEN RAISE EXCEPTION 'Settlement must start as draft' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' AND OLD.status='terkirim' AND NEW.status='dibatalkan' THEN
  IF (to_jsonb(NEW)-ARRAY['status','alasan_batal','dibatalkan_oleh','dibatalkan_at']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','alasan_batal','dibatalkan_oleh','dibatalkan_at']) THEN
   RAISE EXCEPTION 'Original posted entry is immutable' USING ERRCODE='23514';
  END IF;
  IF length(trim(NEW.alasan_batal)) NOT BETWEEN 3 AND 2000 OR NEW.dibatalkan_at IS NULL OR NOT EXISTS
   (SELECT 1 FROM bl_users WHERE id=NEW.dibatalkan_oleh AND role IN ('owner','admin') AND aktif AND NOT must_change_password) THEN
   RAISE EXCEPTION 'Cancellation requires reason and authorized actor' USING ERRCODE='23514';
  END IF;
  IF EXISTS (SELECT 1 FROM bl_tutup_buku WHERE periode=to_char(OLD.tanggal_cair,'YYYY-MM') AND status='ditutup') THEN
   RAISE EXCEPTION 'Accounting period is closed' USING ERRCODE='23514';
  END IF;
  IF EXISTS (SELECT 1 FROM keu_transaksi WHERE settlement_id=OLD.id AND status<>'dibatalkan') THEN RAISE EXCEPTION 'Cancel linked transactions first' USING ERRCODE='23514'; END IF;
  RETURN NEW;
 END IF;
 IF TG_OP='UPDATE' AND OLD.status<>'draf' AND NEW IS DISTINCT FROM OLD THEN RAISE EXCEPTION 'Posted or cancelled history is immutable' USING ERRCODE='23514'; END IF;
 IF NEW.status='dibatalkan' AND (TG_OP='INSERT' OR OLD.status='draf') THEN RAISE EXCEPTION 'Only posted entries can be unposted' USING ERRCODE='23514'; END IF;
 IF (TG_OP='INSERT' OR OLD.status='draf') AND (NEW.alasan_batal<>'' OR NEW.dibatalkan_oleh IS NOT NULL OR NEW.dibatalkan_at IS NOT NULL) THEN RAISE EXCEPTION 'Cancellation metadata is reserved for unpost' USING ERRCODE='23514'; END IF;
 
 IF NEW.status='terkirim' AND (TG_OP='INSERT' OR OLD.status<>'terkirim') THEN
  SELECT sum(jumlah) INTO allocated FROM keu_alokasi_settlement WHERE settlement_id=NEW.id; IF allocated IS NULL OR allocated<>NEW.neto THEN RAISE EXCEPTION 'Settlement allocations do not reconcile' USING ERRCODE='23514'; END IF;
  IF EXISTS (SELECT 1 FROM bl_tutup_buku WHERE periode=to_char(NEW.tanggal_cair,'YYYY-MM') AND status='ditutup') THEN RAISE EXCEPTION 'Accounting period is closed' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END;
$keu$;

INSERT INTO keu_schema_versions(version) VALUES ('20261009_keu_unpost') ON CONFLICT DO NOTHING;

COMMIT;
