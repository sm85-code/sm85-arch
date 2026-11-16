# Summary ERP — Plan #2

Isolated route `/summary`, GET `/api/marketplace-erp/summary` only. Plan #1 excluded.

## Source audit

- `mpe_pesanan`: Shopee native orders and EasyBoss orders imported as local manual orders.
- `mpe_bridge_order`: unique platform/shop/order identity and most recent source snapshot; `pesanan_id` is unique. Join once, never count it as a second order table. Shopee is not imported by the current EasyBoss bridge.
- `mpe_akun_marketplace` / `mpe_toko_penjualan`: native shop IDs and public names; existing assigned-shop permissions apply.
- `mpe_item_pesanan`: quantity and discounted item subtotal for native orders. EasyBoss prices and quantities come from complete, unique-item source snapshots, since imported operational rows can contain zero placeholders.
- Source snapshots project the latest status without altering local operational status. Unknown states remain unknown.
- Current Dashboard, order services, accounting, balances, schedulers and integrations are unchanged.
- Financial reports contain manual/closed-book layers and are not replaced or forced to match Summary.
- Shopee settlement rows represent released funds; EasyBoss balance refresh can invoke remote IO. Neither is a complete, comparable local pending-payout/refund ledger. Summary never invokes either refresh.

## Definitions and missing data

Period is order date (Asia/Jakarta); fallback to ERP import date is counted explicitly. Status means latest saved snapshot, including changes after the selected period. Data coverage is ERP imports, not complete marketplace history.

Gross = stored discounted item subtotals, before excluding cancellation/refunds, excluding shipping and marketplace fees. Money is Decimal. Net/refund/return counts and pending payout remain null with visible explanation because complete source records are unavailable. Cancellation amounts are displayed separately, never deducted twice. Nulls propagate to subtotals, totals and trend gaps; known partial sales are labeled separately.

## Isolation and limits

Dedicated session with repeatable-read/read-only transaction; rollback on exit; 5s statement timeout, 500ms lock timeout, 12s request budget, 2 concurrent Summary reads per worker. Existing date and item foreign-key indexes. Maximum 366 days, 10,000 candidate orders and 500 shops per source; refuse rather than silently truncate. Detail pages contain at most 100 orders. No migration, trigger, schema change, external call or source write.

Permission `summary` has only `baca`. Administrator has access by default; no other built-in role is seeded. Kelola Aplikasi > Role provides a Summary-only control using the existing role API, re-reading each role before preserving all its other permissions. Existing full role editor also lists Summary.

## Verification and rollback

Run backend lint, full tests, Summary PostgreSQL integration, compile check; frontend lint, full tests and production build. Confirm only Summary files, navigation/permission registration and additive tests/help text differ.

Pre-change backend: `58884b3ee069d539450f2986c36580bb48e1ca56`.
Pre-change frontend main: `5b03777637e8b424ad7c9f7613eaefded6a76dd2`.

Backend existing deployment: `/opt/sm85-arch/deploy.sh <merged-sha>` with built-in health check and image rollback. Emergency `/opt/sm85-arch/deploy.sh rollback` (verify recorded previous image first). Frontend: retain Cloudflare Pages production deployment ID and roll back that deployment if needed. No database rollback is necessary or allowed: no migration or historical data change is introduced.
