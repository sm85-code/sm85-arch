# Marketplace ERP — IDEAL / Tahap follow-ups

Tracking notes for `tenants/marketplace_erp` (API prefix `/api/marketplace-erp`).
Does **not** expand `tenants/toko`.

## Done

| Stage | Notes |
|-------|--------|
| **Tahap 1 (M0)** | Auth owner, AkunMarketplace, Produk (SKU induk), ProdukListing. PR #105. |
| **Tahap 2 (this PR)** | Stock reservation + ledger; OMS inbox (`Pesanan`/`ItemPesanan`) with confirm→process→ship stubs; Shopee OAuth URL + token exchange + signed request helper; Lazada/TikTok/Blibli placeholders; seed-now gate (`MARKETPLACE_ERP_SEED_SECRET`); tests. |

### Tahap 2 detail

- **Stock:** `Gudang` (single `DEFAULT`), `StokLedger` (append-only), `StokReservasi`. `Produk.stok` = available cache, updated atomically on adjust / reserve / release. Oversell blocked on `to_ship`. Cancel releases; ship consumes reservation (no restore).
- **OMS:** Normalized statuses `unpaid \| to_ship \| shipped \| completed \| cancelled`. Owner-gated list/CRUD. Soft-fail platform push on `to_ship` (`tersinkron_marketplace` / `catatan_sinkron`).
- **Shopee:** Env `SHOPEE_PARTNER_ID/KEY`, `SHOPEE_REDIRECT_URI`, `SHOPEE_ENV`, `SHOPEE_LIVE_SYNC`. Live pull/push stays off until `SHOPEE_LIVE_SYNC=true`.
- **Seed gate:** Header `X-Marketplace-Erp-Seed-Secret` or authenticated owner; production rejects anonymous without secret.

## Deferred / next

| Item | Why deferred |
|------|----------------|
| Multi-warehouse allocation / transfer | T2 ships one DEFAULT gudang; per-warehouse reservation + transfer ledger = M3 |
| Live Shopee GetItemList / GetOrderList / SetOrderReadyToShip | Needs approved Partner Key + sandbox shop; structure ready behind `SHOPEE_LIVE_SYNC` |
| Shopee webhook receiver | M2 remainder; idempotent upsert already keyed on `(platform, id_eksternal)` |
| Lazada / TikTok Shop / Blibli OAuth + sync | Placeholders only (Shopee first) |
| Staff role + `StaffAkunMarketplace` scoping | M3; T2 remains owner-only single-org |
| Settlement / chat / ads | M4–M5 per product spek |
| Encrypt shop tokens at rest | Open decision; DB already isolated |
| Job runner (refresh tokens, pull orders) | Open decision (cron / ARQ / Celery) |
| FE private repo (`frontend-marketplace-erp`) | Out of this backend PR |

## Non-goals (unchanged)

- Do not merge with `tenants/toko` storefront or `tenants/toko/modules/erp` as SSOT.
- No public B2C checkout in this tenant.
