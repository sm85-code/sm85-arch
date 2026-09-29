# Marketplace ERP Tahap 2 — brief

**Scope:** `tenants/marketplace_erp` only (`/api/marketplace-erp`). No `tenants/toko` expansion.

**Landed**

1. Stock foundation: reservation on `to_ship`, release on cancel, `Produk.stok` as atomic available cache + `StokLedger`. Single DEFAULT warehouse (multi-gudang deferred — see `tenants/marketplace_erp/IDEAL_FOLLOWUPS.md`).
2. OMS: unified `Pesanan` inbox, normalized statuses, pipeline unpaid→to_ship→shipped→completed (+ cancel), owner-gated list/CRUD.
3. Shopee adapter: OAuth authorize URL, token exchange, signed request helper, token storage on `AkunMarketplace`. Live sync behind `SHOPEE_LIVE_SYNC`. Lazada/TikTok/Blibli placeholders.
4. Seed gate: `MARKETPLACE_ERP_SEED_SECRET` + `X-Marketplace-Erp-Seed-Secret` (madrasah pattern).
5. Tests: `tests/test_marketplace_erp_t2_stock_orders.py`, `test_marketplace_erp_seed_gate.py`, `test_marketplace_erp_shopee_oauth.py` (+ existing foundation).

**API additions (owner unless noted)**

| Method | Path |
|--------|------|
| GET | `/seed-now` (gated) |
| GET/POST | `/gudang`, `/stok/ledger`, `/stok/adjust` |
| GET/POST | `/pesanan` |
| GET | `/pesanan/{id}` |
| POST | `/pesanan/{id}/status` |
| DELETE | `/pesanan/{id}` (unpaid only) |
| GET | `/oauth/shopee/start?akun_id=` |
| GET | `/oauth/shopee/callback/{akun_id}` (public redirect target) |
| POST | `/akun/{id}/sync/produk`, `/akun/{id}/sync/pesanan` |

Full design: `docs/marketplace-erp-spek.md` (if present) / workspace spek.
