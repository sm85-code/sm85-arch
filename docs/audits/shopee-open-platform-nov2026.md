# Shopee Open Platform: November 2026 audit

Scope: Marketplace ERP on base `70cdd3b`; no production operations, deployment,
credentials changes, database migrations, or Actions dispatch.

## Sources and coverage

The two supplied Shopee announcements are authoritative for this change:

- 2026-10-30, **[IMPORTANCE][Action Required]Shipping Document Creation and Download API Updates**.
  Already live; migrate by Q2 2027; retirement date remains unannounced.
- 2026-11-06, **Significant OpenAPI Updates of Order,Payment and Logistics**.
  Campaign downgrade effective 2026-11-02; Instant limits effective 2026-11-04.

| Area | Existing implementation | Decision |
| --- | --- | --- |
| Standard labels | `erp_shopee.unduh_resi[_banyak]` used create/result/raw-download | Default to parameter lookup, package discovery, create job, poll job status, download filelink, fetch unsigned bytes |
| Booking / unpackaged SKU labels | No caller in this repository | Shared job request builder supports exactly one selector, five document types, and 600-item / SKU-copy limits; no invented UI workflow |
| TO labels / First Mile | No callers found in tenants/shared | No active integration to migrate; future implementations must use filelink endpoints |
| Order list | Incremental cursor pages with a watermark | Detect warning at envelope or response level, switch to shipment list without unsupported time filters, deduplicate order IDs, fetch current details |
| Package search | No `search_package_list` caller found | No active integration to migrate |
| Campaign push | Codes 15 and 30 were not routed to order sync | Subscribe through existing settings mechanism and route both to existing shop sync; registration is not performed by this change |
| Webhook replay | Persistent receipt keyed on raw bytes | Canonical shop + event code + business data; delivery timestamp, JSON spacing, and property order do not change identity |
| ID Instant Logistics | Not implemented | Read/write adapter with strict boolean and nonnegative integer validation; disabled requests omit the limit; no automatic settings changes or new public route |
| Payment SSPL | Escrow normalizer stores scalar fee fields in raw detail | VN only. Preserve `sspl_fee` and provider net escrow; do not deduct it again or impose a VN fee on ID stores |
| FBS invoices | No callers found | BR only; CNPJ/types/disabled invoices are outside the ID integration. Launch date is unconfirmed |

## Runtime behavior

- `package_list` contains package-number strings, including every package returned
  for each selected order. Missing packages, partial accepted-ID lists, provider
  failures, unknown job states, or mismatched job IDs abort the download.
- Job status uses `job_status`. Only `READY` proceeds; `FAILED` / `EXPIRED` fail;
  `PROCESSING` / `PENDING` polling is bounded. An outer 60-second budget covers
  parameter lookup, package discovery, job requests, polling, and download.
- File links last 30 minutes. They are consumed immediately, not stored or logged.
  A 401/403 from the file host obtains a fresh link once for the same job.
  The ERP endpoint still returns document bytes, preserving frontend compatibility.
- File fetches have no Shopee tokens/signatures/cookies, no ambient proxy/netrc,
  no redirects, validated public DNS addresses pinned to the connection, TLS SNI
  and certificate verification, a 30 MiB cap, and format checking.
- Cancellation of a local deadline does not stop an already-running HTTP thread.
  An uncertain create-job outcome is never retried automatically or sent to legacy.
- A degraded snapshot contains only the fulfillment queue, not all changed orders.
  Neither incremental nor full-sync watermark advances; result includes
  `downgrade=true` and `watermark_dipertahankan=true`. Normal polling resumes
  reconciliation once Shopee returns a complete order list. Missing orders are
  never interpreted as deletions or cancellations.
- Existing importer protections remain responsible for stock/order idempotency.
  Push payloads trigger a fresh provider read rather than applying possibly stale
  pushed statuses directly. Receipt uniqueness survives process restarts and
  concurrent duplicate inserts; business update_time distinguishes later events.
  The four-hour receipt lifetime follows the existing replay policy. Background
  sync remains a safety net for failed background tasks; this is not a durable outbox.

## Transition and operational limits

Default is `job`. Optional non-secret `SHOPEE_SHIPPING_DOCUMENT_FLOW=legacy`
selects the unchanged old regular-order label flow for deliberate rollback during
Shopee's transition. Unknown values fail closed. No deployment setting is changed
by this PR. Remove this option after Shopee announces and completes retirement.

The announcements do not specify every request/response schema. Direct official
reference pages for create_shipping_document_job and get_shipment_list returned
HTTP 403 in this environment. Structural fields were cross-checked against the
public generated `Naputt1/shopee-v2-go` declarations (`logistics.type.gen.go` and
`order.type.gen.go`), but these are secondary evidence, not a live contract test.
Before a later deployment, validate job payload/status, event codes 15/30,
shop permissions, file-host behavior, and supported templates with the authorized
Shopee sandbox/reference. The October notice itself inconsistently names First
Mile's replacement; resolve that against official reference before adding a caller.
No production or sandbox Shopee requests are performed by these tests.

The Instant feature can reject unsupported shops/regions or missing Instant
channels; preserve the structured provider error. Settings can take 5–10 minutes
to apply and reset at local noon. Do not assume every ID shop is eligible.

## Validation

- New offline unit and integration suite: `tests/test_marketplace_erp_shopee_nov2026.py`.
- HTTP mock integration exercises the real signed request implementation and
  provider JSON/error parsing; SQLite integration exercises repeated sync imports,
  watermark preservation, signed webhook routing, and persistent receipts.
- Existing regular-label tests explicitly select legacy mode, preserving rollback
  coverage. Existing order, stock, finance, OAuth and push suites remain unchanged.
- Commands: `ruff check .`, `python -m compileall -q tenants shared`,
  `python -m pytest tests/ -n 0 -q`; results are recorded in the PR.
- Real PostgreSQL checks require a disposable `TEST_DATABASE_URL`; skipped checks
  are reported, not represented as passed. No production database is used.
- Commits include `[skip ci]` so the repository's pull_request workflows do not run.
  No workflow file is edited; full CI remains necessary before any later merge
  under AGENTS.md, outside this task's prohibition on running Actions.

### Results in this workspace

- Full repository run: **1606 passed, 27 skipped**, 4 warnings (219.97 s).
- Final affected-suite rerun after refinements: **164 passed**, 1 warning (10.63 s),
  including **48 new November-update cases**. This rerun covers the final source;
  the full run started before the last small validation/test refinements.
- Repository-wide Ruff, Python compilation, and `git diff --check`: passed.
- Warnings are existing crypt deprecation and SQLAlchemy test-conflict warnings.
  Skipped external-service/PostgreSQL checks were not replaced by production calls.
- Python 3.12.14; repository-pinned requirements installed in an isolated test venv.
  No Docker executable is available here, so no container image build was performed.
