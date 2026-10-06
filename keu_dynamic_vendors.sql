BEGIN;

SET LOCAL lock_timeout = '5s';

SET LOCAL statement_timeout = '120s';

SELECT pg_advisory_xact_lock(61062026);

DO $$ BEGIN IF to_regclass('bl_users') IS NULL OR to_regclass('keu_vendor') IS NULL OR to_regclass('keu_schema_versions') IS NULL THEN RAISE EXCEPTION 'Target is not an initialized BUMI keu tenant'; END IF; END $$;

DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM keu_schema_versions WHERE version='20261007_keu_product_metadata') THEN RAISE EXCEPTION 'Apply keu_product_metadata.sql first'; END IF; END $$;

ALTER TABLE keu_vendor ADD COLUMN IF NOT EXISTS kode varchar(128);

ALTER TABLE keu_vendor ADD COLUMN IF NOT EXISTS alamat text NOT NULL DEFAULT '';

ALTER TABLE keu_vendor ADD COLUMN IF NOT EXISTS keterangan text NOT NULL DEFAULT '';

UPDATE keu_vendor v SET kode=CASE
        WHEN EXISTS (SELECT 1 FROM keu_vendor_slot s WHERE s.vendor_id=v.id)
        THEN (SELECT (CASE WHEN s.jenis='tukang_kayu' THEN 'tk-' ELSE 'sup-' END) || s.nomor::text FROM keu_vendor_slot s WHERE s.vendor_id=v.id)
        ELSE 'VND-' || v.id END WHERE v.kode IS NULL;

ALTER TABLE keu_vendor ALTER COLUMN kode SET NOT NULL;

ALTER TABLE keu_vendor DROP CONSTRAINT IF EXISTS keu_vendor_kode_key;

ALTER TABLE keu_vendor ADD CONSTRAINT keu_vendor_kode_key UNIQUE (kode);

ALTER TABLE keu_vendor DROP CONSTRAINT IF EXISTS ck_keu_vendor_kode;

ALTER TABLE keu_vendor ADD CONSTRAINT ck_keu_vendor_kode CHECK (length(trim(kode)) > 0);

ALTER TABLE keu_vendor_slot DROP CONSTRAINT IF EXISTS ck_keu_vendor_slot;

ALTER TABLE keu_vendor_slot ADD CONSTRAINT ck_keu_vendor_slot CHECK (jenis IN ('tukang_kayu','supplier') AND nomor >= 1);

CREATE OR REPLACE FUNCTION keu_vendor_type_guard() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $keu$
BEGIN
 IF NEW.jenis IS DISTINCT FROM OLD.jenis THEN
  IF EXISTS (SELECT 1 FROM keu_alokasi_vendor WHERE vendor_id=OLD.id) THEN
   RAISE EXCEPTION 'Referenced vendor type is immutable' USING ERRCODE='23514';
  END IF;
  UPDATE keu_vendor_slot SET vendor_id=NULL WHERE vendor_id=OLD.id;
 END IF;
 RETURN NEW;
END;
$keu$;

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
 IF allocated + (CASE WHEN NEW.dibatalkan THEN 0 ELSE NEW.qty END) > capacity THEN RAISE EXCEPTION 'Allocation exceeds item quantity' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END;
$keu$;

INSERT INTO keu_schema_versions(version) VALUES ('20261008_keu_dynamic_vendors') ON CONFLICT DO NOTHING;

COMMIT;
