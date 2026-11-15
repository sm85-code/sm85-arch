-- Idempotent additive migration (also self-healed by the marketplace ERP seeder at startup).
-- Adds lean scheduler bookkeeping to the EasyBoss bridge state. No order/shop rows are changed.
ALTER TABLE mpe_bridge_state ADD COLUMN IF NOT EXISTS lean_json TEXT NOT NULL DEFAULT '{}';
