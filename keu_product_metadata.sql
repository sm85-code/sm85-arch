BEGIN;

SET LOCAL lock_timeout = '5s';

SET LOCAL statement_timeout = '120s';

SELECT pg_advisory_xact_lock(61062026);

DO $$ BEGIN IF to_regclass('bl_users') IS NULL OR to_regclass('keu_produk') IS NULL THEN RAISE EXCEPTION 'Target is not an initialized BUMI keu tenant'; END IF; END $$;

CREATE TABLE IF NOT EXISTS keu_schema_versions (version varchar(64) PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());

ALTER TABLE keu_produk ADD COLUMN IF NOT EXISTS sku_induk varchar(128);

ALTER TABLE keu_produk ADD COLUMN IF NOT EXISTS nama_asli varchar(255) NOT NULL DEFAULT '';

ALTER TABLE keu_produk ADD COLUMN IF NOT EXISTS gambar_url varchar(2048) NOT NULL DEFAULT '';

ALTER TABLE keu_produk ADD COLUMN IF NOT EXISTS varian_list jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE keu_produk ADD COLUMN IF NOT EXISTS harga_jual numeric(20,2) NOT NULL DEFAULT 0;

ALTER TABLE keu_produk ADD COLUMN IF NOT EXISTS status varchar(16) NOT NULL DEFAULT 'master';

ALTER TABLE keu_produk ALTER COLUMN jenis DROP NOT NULL;

UPDATE keu_produk SET nama_asli=nama WHERE nama_asli='';

ALTER TABLE keu_produk DROP CONSTRAINT IF EXISTS ck_keu_produk_harga;

ALTER TABLE keu_produk ADD CONSTRAINT ck_keu_produk_harga CHECK (harga_jual >= 0);

ALTER TABLE keu_produk DROP CONSTRAINT IF EXISTS ck_keu_produk_status;

ALTER TABLE keu_produk ADD CONSTRAINT ck_keu_produk_status CHECK (status IN ('draf','master') AND (status <> 'master' OR jenis IS NOT NULL));

ALTER TABLE keu_produk DROP CONSTRAINT IF EXISTS ck_keu_produk_varian;

ALTER TABLE keu_produk ADD CONSTRAINT ck_keu_produk_varian CHECK (jsonb_typeof(varian_list)='array');

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
 IF NOT NEW.dibatalkan AND (product_kind IS NULL OR product_state IS DISTINCT FROM 'master' OR product_enabled IS DISTINCT FROM TRUE OR enabled IS DISTINCT FROM TRUE OR NOT EXISTS (SELECT 1 FROM keu_vendor_slot WHERE vendor_id=NEW.vendor_id) OR (product_kind='kayu' AND vendor_kind IS DISTINCT FROM 'tukang_kayu') OR (product_kind='non_kayu' AND vendor_kind IS DISTINCT FROM 'supplier')) THEN
  RAISE EXCEPTION 'Choose an active vendor for a mapped product' USING ERRCODE='23514';
 END IF;
 SELECT COALESCE(sum(qty),0) INTO allocated FROM keu_alokasi_vendor WHERE item_id=NEW.item_id AND NOT dibatalkan AND id<>NEW.id;
 IF allocated + (CASE WHEN NEW.dibatalkan THEN 0 ELSE NEW.qty END) > capacity THEN RAISE EXCEPTION 'Allocation exceeds item quantity' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END;
$keu$;

INSERT INTO keu_schema_versions(version) VALUES ('20261007_keu_product_metadata') ON CONFLICT DO NOTHING;

COMMIT;
