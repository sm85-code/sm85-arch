-- Additive repair for databases created before audit_json was added to the ORM.
-- Existing snapshots, transactions, and audit rows are preserved.
ALTER TABLE IF EXISTS mpe_laporan_keuangan_tutup ADD COLUMN IF NOT EXISTS audit_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE IF EXISTS mpe_laporan_keuangan_audit ADD COLUMN IF NOT EXISTS audit_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE IF EXISTS mpe_pesanan_hapus_log ADD COLUMN IF NOT EXISTS audit_json TEXT NOT NULL DEFAULT '[]';
