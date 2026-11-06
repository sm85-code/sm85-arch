-- Development/review only. Never execute against production without explicit authorization.
-- Idempotent additive migration for existing ERP databases. No order/shop rows are changed.
BEGIN;
ALTER TABLE mpe_toko_penjualan ADD COLUMN IF NOT EXISTS sumber VARCHAR(16) NOT NULL DEFAULT 'manual';
ALTER TABLE mpe_toko_penjualan ADD COLUMN IF NOT EXISTS easyboss_shop_id VARCHAR(128);
ALTER TABLE mpe_toko_penjualan ADD COLUMN IF NOT EXISTS marketplace_shop_id VARCHAR(128);
ALTER TABLE mpe_toko_penjualan ADD COLUMN IF NOT EXISTS sinkron_status VARCHAR(32) NOT NULL DEFAULT 'manual';
ALTER TABLE mpe_toko_penjualan ADD COLUMN IF NOT EXISTS sinkron_at TIMESTAMPTZ;
CREATE UNIQUE INDEX IF NOT EXISTS uq_mpe_toko_easyboss ON mpe_toko_penjualan(platform, easyboss_shop_id);

CREATE TABLE IF NOT EXISTS mpe_bridge_state (
	id VARCHAR(32) NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	checkpoint TIMESTAMP WITH TIME ZONE, 
	heartbeat TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id)
)

;

CREATE TABLE IF NOT EXISTS mpe_bridge_run (
	id VARCHAR(64) NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	finished_at TIMESTAMP WITH TIME ZONE, 
	imported INTEGER NOT NULL, 
	skipped INTEGER NOT NULL, 
	failed INTEGER NOT NULL, 
	error VARCHAR(128), 
	PRIMARY KEY (id)
)

;

CREATE TABLE IF NOT EXISTS mpe_bridge_order (
	id VARCHAR(64) NOT NULL, 
	platform VARCHAR(16) NOT NULL, 
	shop_id VARCHAR(128) NOT NULL, 
	order_id VARCHAR(200) NOT NULL, 
	toko_id VARCHAR(64), 
	pesanan_id VARCHAR(64), 
	status VARCHAR(32) NOT NULL, 
	source_status VARCHAR(64) NOT NULL, 
	attempts INTEGER NOT NULL, 
	error VARCHAR(128), 
	snapshot_json TEXT NOT NULL, 
	audit_json TEXT DEFAULT '[]' NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_mpe_bridge_order_source UNIQUE (platform, shop_id, order_id), 
	FOREIGN KEY(toko_id) REFERENCES mpe_toko_penjualan (id) ON DELETE RESTRICT, 
	UNIQUE (pesanan_id), 
	FOREIGN KEY(pesanan_id) REFERENCES mpe_pesanan (id) ON DELETE RESTRICT
)

;
COMMIT;
