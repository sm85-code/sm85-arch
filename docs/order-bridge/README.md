# Order Bridge EasyBoss → Marketplace ERP

Status: backend, UI, persistence, locking, and importer implemented with synthetic fixtures. **Real EasyBoss order import is NOT verified or activated.** No production DB, container, scheduler, Shopee integration, or order was modified during development.

Frontend is a separate repository: `sm85-code/frontend-marketplace-erp`, branch `hermes/order-bridge-easyboss`. Both PRs are required for the complete UI. The existing Shopee account tab remains separate; internal and reseller shops now have separate tabs. Internal shops include the existing Web App and bridge controls.

## What works with the tested adapter

- Shop preview with fingerprint revalidation; stable `(platform, easyboss_shop_id)` matching; optional marketplace identity; explicit legacy-shop linking; rename without replacing ERP ID.
- Only `lazada` and `tiktokshop`; no reseller or Web App mutation. Missing shops are preserved. Source-inactive shops are disabled; locally disabled shops are never silently re-enabled. Legacy shops remain for review.
- Manual order creation through `pesanan_manual.buat`, yielding actual `Pesanan` and `ItemPesanan` consumed by the existing order/production flow. No ProcessingJob shortcut, no mapping/supplier/worker changes.
- Unique source key `(platform, shop_id, original_order_id)` plus global deterministic manual code. Source receipt, all items, metadata, and order commit atomically. Each failed order rolls back independently.
- PostgreSQL session advisory lock on a dedicated connection spans per-order commits, manual triggers, retries, shop sync and shop deletion. Unique constraints remain a second line of defense. Lock releases on process/connection termination. Restart marks abandoned runs interrupted.
- Only complete, paid, `ready` order snapshots are eligible. Missing deadline, buyer, date or items fails visibly. Package fragments and contradictory snapshots are rejected. No fake dates or fabricated product values.
- Original imported snapshot remains in `Pesanan.detail_json.easyboss`; latest validated source snapshot and status/hash audit history remain in `mpe_bridge_order`. A changed/cancelled imported order becomes `review`; existing production work is not modified.
- Failed retry fetches fresh source orders; stored stale snapshots are never blindly reimported. Pagination completes before any write. A late page failure leaves checkpoint unchanged.
- Shop delete checks every tenant ORM table's string/text fields, including JSON transaction/history references, not just manual orders. Referenced shops return 409 and offer deactivation; confirmation rechecks under lock. Manual creation locks the shop row. No old shop is automatically deleted.
- Dashboard: configuration/connection state, scheduler heartbeat, counts, errors, logs, recent runs, preview/apply, scan, retry, pause/enable. Existing ERP cookie/JWT and `toko_penjualan` read/write permissions guard all routes.

## Real integration blocker

The task environment has GitHub access but **no EasyBoss MCP connector, OCI connection, EasyBoss session, or EasyBoss service credential binding**. The operational repository documents the live MCP implementation at `/opt/ai-ops/easyboss-mcp/server.py`, but that file is not in the accessible repositories. No live EasyBoss request was made.

The single upstream integration dependency is access to that implementation and an authorized read session to verify full shop directory, complete order detail/items, payment/status mapping, and package membership. Existing package-list monitor evidence is insufficient to prove complete orders. Do not set `EASYBOSS_BRIDGE_CONTRACT_VERIFIED=1` until these are verified and implemented by the gateway. The optional `bridge-v1` HTTP gateway below is an explicit adapter boundary defined here; **it is not claimed to exist in EasyBoss or the current MCP**. NativeReadClient implements only source-supported read endpoints and is intentionally not wired as a fake complete-order importer.

## Configuration

All defaults are OFF. No owner login/token is required by the worker: it runs inside the ERP backend and calls the application service. The only outbound credential is a narrowly scoped, read-only internal gateway token. Never put cookies/passwords/tokens in Git, request bodies, screenshots, or logs.

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL_MARKETPLACE_ERP` | Existing isolated ERP PostgreSQL database |
| `EASYBOSS_BRIDGE_URL` | HTTPS base URL of an implemented, verified `bridge-v1` read-only gateway |
| `EASYBOSS_BRIDGE_TOKEN` | Secret bearer token restricted upstream to shop/order reads |
| `EASYBOSS_BRIDGE_CONTRACT_VERIFIED` | `1` only after upstream evidence/sandbox acceptance; absent disables API operations |
| `EASYBOSS_BRIDGE_SCHEDULER` | `1` starts the background worker; absent means no worker |
| `EASYBOSS_BRIDGE_INTERVAL_SECONDS` | Minimum 60, default 60; increase after measuring upstream limits |

DB automation state is also disabled initially. Both process flag and dashboard Enable are required. The dashboard flag cannot start a process whose scheduler flag is off.

Runtime scans currently re-read the complete upstream order window rather than trust an unverified changed-since filter. Checkpoint records the start of the last completely fetched scan, even when individual orders failed; it is diagnostic, not an exclusion watermark. The upstream must provide a bounded, documented retention window that includes unresolved failures and changes to imported orders. Retry re-reads that same window. Do not enable minute polling until rate limits and scan volume are established. No age cutoff is invented; first live import must be explicitly scoped and reviewed upstream after production approval.

## Migration

Development/review script: `scripts/migrations/20261106_order_bridge.sql`. Adds five columns to `mpe_toko_penjualan` and a unique index; creates three necessary durable tables: `mpe_bridge_state`, `mpe_bridge_run`, `mpe_bridge_order`. Source receipts have RESTRICT foreign keys to shops and orders. No business rows are rewritten or deleted.

The repository's existing startup self-heal also applies these additive changes, and `metadata.create_all` creates new tables. **Merging main triggers the existing production deployment workflow**, which would run this schema setup on startup. Production migration/deployment needs separate explicit owner authorization; do not merge merely to try the feature. This work does not run or change the production workflow.

## Tests

```sh
ruff check .
python -m pytest tests/ -n 0
TEST_DATABASE_URL=postgresql://.../disposable_db python -m pytest tests/test_marketplace_erp_order_bridge_pg.py -n 0
```

Unit fixtures are synthetic SQLite/MockTransport, including native form/pagination contract, errors at HTTP 200, missing fields, variants, duplicate and conflicting packages, retry, partial writes, auth and deletion. PostgreSQL tests use disposable schemas and test actual locks across independent connections, restart idempotency and rollback. CI integration-pg includes the bridge PG tests. Frontend runs `yarn lint`, `yarn test`, `yarn build` with the repository's committed `.env.production`; building does not deploy or call production APIs.

## Activation after separate production authorization

1. Obtain read-only MCP implementation/session access; implement the upstream `bridge-v1` boundary against observed full directory and full order details. Record sanitized evidence and actual status/payment mapping. Verify order-vs-package completeness, item identity and pagination under source changes. Source cookie lifecycle remains upstream; no owner ERP login loop.
2. On disposable DB/sandbox: apply migration, configure gateway secrets, verify shops including zero-order shops; preview and explicitly link both old manual shops only after inspecting real IDs. Check that Web App and reseller rows are preserved.
3. Run synthetic/sandbox import and retry, inspect `Pesanan` list and workflow, verify SKUs/variants/date/deadline and cancellation review. Keep scheduler disabled.
4. After owner approval for production migration/import/deployment: backup DB, deploy backend + frontend, apply additive schema through the approved workflow, set a reviewed source window, preview shops, perform a controlled initial scan, then set the scheduler flag and Enable. No production action is authorized by this document itself.
5. Monitor heartbeat (stale after 180 seconds), imported/failed/review receipts and scan duration. Adjust polling for API limits. Connection `ok` means the latest successful gateway operation, not an independent live native-MCP health check.

## Pause / rollback

Pause in dashboard prevents future automatic scans; an already running transaction/scan completes. Use the environment kill switch for an approved process restart if immediate shutdown is necessary. No restart is performed by the dashboard.

Disable automation and scheduler, then revert application releases through the separately approved deployment process. Keep additive columns/tables and all source/order receipts to preserve idempotency and history. Do not drop bridge tables or delete imported orders as a rollback. Source changes after import require operational review; no automatic cancellation of production jobs occurs. Existing Shopee and manual entry remain usable.
