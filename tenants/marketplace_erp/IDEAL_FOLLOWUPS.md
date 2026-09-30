# Marketplace ERP — IDEAL / Tahap follow-ups

Tracking notes for `tenants/marketplace_erp` (API prefix `/api/marketplace-erp`).
Does **not** expand `tenants/toko`.

## Done

| Stage | Notes |
|-------|--------|
| **Tahap 1 (M0)** | Auth owner, AkunMarketplace, Produk (SKU induk), ProdukListing. PR #105. |
| **Tahap 2** | Stock reservation + ledger; OMS inbox (`Pesanan`/`ItemPesanan`) with confirm→process→ship stubs; Shopee OAuth URL + token exchange + signed request helper; Lazada/TikTok/Blibli placeholders; seed-now gate (`MARKETPLACE_ERP_SEED_SECRET`); tests. |
| **Tahap 3** | Multi-gudang + transfer; `staff` role + `StaffAkunMarketplace` per-shop scoping (`/akun`, `/pesanan`, pengiriman); manual pengiriman (kurir/resi) on `Pesanan`; `Settlement` (manual payout reconciliation, auto matched/discrepancy); `GET /laporan/ringkas` (omzet, pesanan per status, produk terlaris, stok kritis). All local-data -- built ahead of any live marketplace API connection, per owner's request (30 toko: 15 Shopee, 5 TikTokShop, 5 Lazada, 5 Blibli being registered separately). |
| **Tahap 4 (this PR)** | `IklanCampaign` + `IklanMetrikHarian` (ads). Manual daily spend entry (no ads API partner approval exists yet -- separate from the shop OAuth this tenant has); `GET /iklan/{id}/laporan` computes ROAS from **actual** `Pesanan`/`ItemPesanan` sales of the campaign's linked `produk_id`, not a manually-entered revenue figure. |

### Tahap 2 detail

- **Stock:** `Gudang` (single `DEFAULT`), `StokLedger` (append-only), `StokReservasi`. `Produk.stok` = available cache, updated atomically on adjust / reserve / release. Oversell blocked on `to_ship`. Cancel releases; ship consumes reservation (no restore).
- **OMS:** Normalized statuses `unpaid \| to_ship \| shipped \| completed \| cancelled`. Owner-gated list/CRUD. Soft-fail platform push on `to_ship` (`tersinkron_marketplace` / `catatan_sinkron`).
- **Shopee:** Env `SHOPEE_PARTNER_ID/KEY`, `SHOPEE_REDIRECT_URI`, `SHOPEE_ENV`, `SHOPEE_LIVE_SYNC`. Live pull/push stays off until `SHOPEE_LIVE_SYNC=true`.
- **Seed gate:** Header `X-Marketplace-Erp-Seed-Secret` or authenticated owner; production rejects anonymous without secret.

### Tahap 3 detail

- **Multi-gudang:** `POST /gudang` creates additional warehouses; `POST /stok/transfer` moves qty between two gudang as a paired `StokLedger` entry (`transfer_out`/`transfer_in`). Per-warehouse on-hand is read from the ledger sum, not a running balance column. `Produk.stok` (available-everywhere) is unaffected by a transfer.
- **Staff scoping:** `role="staff"` (already reserved in `USER_ROLES`) + `StaffAkunMarketplace` (`POST/GET/DELETE /staff-akun`, owner-only to manage). `akun_ids_diizinkan`/`pastikan_akses_akun` in `infrastructure/auth.py` filter `/akun`, `/pesanan` (list/get/status/pengiriman) to only the shops a staff account is assigned to; owner stays unrestricted.
- **Pengiriman:** `Pesanan.kurir`/`nomor_resi`/`tanggal_kirim` (nullable, self-healed via `ALTER TABLE`). `POST /pesanan/{id}/pengiriman` -- manual entry only, rejects orders still `unpaid`. No courier API integration yet.
- **Settlement:** `Settlement` table (`akun_id`, periode, gross/fee/net breakdown). `POST /settlement` auto-computes `status` (`matched` if `net` reconciles against gross-fees within a small epsilon, else `discrepancy`); `PATCH /settlement/{id}` can promote to `paid` after human review, or edit the numbers (re-triggers the same auto-check unless promoting to paid).
- **Laporan ringkas:** `GET /laporan/ringkas?dari=&sampai=&batas_stok_kritis=` -- total omzet (orders counted from `to_ship` onward, same convention as `tenants/toko/modules/erp`), count per status, top-10 produk terlaris by qty, produk with `stok <= batas_stok_kritis`.

### Tahap 4 detail

- **Iklan:** `IklanCampaign` (`akun_id`, optional `produk_id`, `budget_harian`, `tanggal_mulai`/`selesai`, linear `status`: `draft -> aktif <-> dijeda -> selesai`, no path back from `selesai`). `IklanMetrikHarian` is a manual daily entry (`impression`/`klik`/`biaya`) upserted on `(campaign_id, tanggal)` -- re-entering a day corrects it. `POST/GET/PATCH/DELETE /iklan`, `POST/GET /iklan/{id}/metrik`.
- **ROAS:** `GET /iklan/{id}/laporan?dari=&sampai=` sums `IklanMetrikHarian.biaya` in the window and, when the campaign has a `produk_id`, sums `ItemPesanan.subtotal` for that produk across `Pesanan` counted as real sales (`to_ship`/`shipped`/`completed`) in the same window -- `roas = omzet_atribusi / total_biaya` (`None` when `total_biaya` is 0). This is computed from real order data, not a number entered by hand, which is what makes it an ERP feature rather than a standalone spend tracker.

## Deferred / next

| Item | Why deferred |
|------|----------------|
| Live Shopee GetItemList / GetOrderList / SetOrderReadyToShip | Needs approved Partner Key + sandbox shop; structure ready behind `SHOPEE_LIVE_SYNC` |
| Shopee webhook receiver | Idempotent upsert already keyed on `(platform, id_eksternal)` |
| Lazada / TikTok Shop / Blibli OAuth + sync | Placeholders only (Shopee first; owner is registering these separately) |
| Shopee Ads / TikTok Ads / Lazada Sponsored Discovery / Blibli Ads API integration | Separate partner approval from shop OAuth; `IklanCampaign`/`IklanMetrikHarian` today are manual-entry only |
| Chat | M4–M5 per product spek |
| Encrypt shop tokens at rest | Open decision; DB already isolated |
| Settlement auto-import from platform statement API | M4 -- currently manual entry only, matches design in `docs/marketplace-erp-spek.md` |
| Per-warehouse StokReservasi allocation (reserve from a specific gudang, not just DEFAULT) | Reservation still always resolves to `ensure_default_gudang`; multi-gudang so far covers manual stock and transfers, not order-time allocation |
| Job runner (refresh tokens, pull orders) | Open decision (cron / ARQ / Celery) |
| FE private repo (`frontend-marketplace-erp`) | Out of this backend PR |
| Revoke other sessions on password change | `/auth/change-password` re-issues the caller's cookie, but older JWTs stay valid until expiry (token `session_version` is always 0 for this tenant) -- add a `session_version` column + check in `get_current_user_marketplace_erp` |
| Server-side enforcement of `must_change_password` | Currently FE-driven (redirect to Ganti Password); backend could 403 non-auth endpoints while the flag is set |

## Non-goals (unchanged)

- Do not merge with `tenants/toko` storefront or `tenants/toko/modules/erp` as SSOT.
- No public B2C checkout in this tenant.
