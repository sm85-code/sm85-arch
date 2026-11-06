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

The first documentation retrieval returned HTTP 403. On 2026-11-06 the official
reference pages were successfully retrieved with HTTP 200 through a different
retrieval path. The official methods, field names, types and push codes below
now supersede the earlier third-party SDK cross-check. This validates the
published contract, not execution against an authorized shop: this runtime has
no Shopee partner credentials, access token or shop ID, and the connected OCI
Gateway exposes no arbitrary command or signed Shopee request operation.
No production or sandbox Shopee API requests were performed.

Before deployment, shop-specific permissions, actual file-host responses, and
supported templates still require an authenticated sandbox exercise. No secret,
production setting or production database was accessed to obtain test credentials.

The Instant feature can reject unsupported shops/regions or missing Instant
channels; preserve the structured provider error. Settings can take 5–10 minutes
to apply and reset at local noon. Do not assume every ID shop is eligible.

## Initial independent contract cross-check

- The public generated `Naputt1/shopee-v2-go` logistics declarations explicitly define `CreateShippingDocumentJobRequest` with `shipping_document_type` and exactly one of `package_list`, `booking_sn`, or `unpackaged_sku_requests`; its response defines `job_id`, `success_id_list`, and `fail_list`.
- `GetShippingDocumentJobStatusRequest` takes `job_id`; its response defines `job_id`, `job_name`, and `job_status` with `PROCESSING`, `READY`, `EXPIRED`, `FAILED`. The ERP additionally tolerates `PENDING`; this extra state is not documented by that SDK.
- `DownloadShippingDocumentJobFilelinkRequest` takes `job_id`; the generated response has **top-level** `file_link`, valid 30 minutes. The ERP accepts top-level and nested `response.file_link`.
- `GetShipmentListRequest` takes `page_size` (1–100) and optional `cursor`; the response contains `order_list`, `more`, and `next_cursor`. The ERP's fallback matches these field names.
- The Shopee announcement's example file link uses `http://`. Do not require HTTPS-only without confirmation from the provider: the downloader allows HTTP/HTTPS with public-IP DNS checks, pinned connection, no redirects, and no credential forwarding. HTTPS remains preferable when Shopee supplies it.
- This initial check used a third-party SDK. The official contract verification below supersedes its source limitation; shop-specific execution remains untested.

## Official contract verification — 2026-11-06

All seven official pages below returned HTTP 200. No application credential was
sent to the documentation service.

| Official reference | Verified contract | ERP result |
| --- | --- | --- |
| [Create job](https://open.shopee.com/documents/v2/v2.logistics.create_shipping_document_job?module=95&type=1) | POST; package_list is string[]; five types; one selector; up to 600; response job_id/success_id_list/fail_list | Matches active regular-label flow |
| [Job status](https://open.shopee.com/documents/v2/v2.logistics.get_shipping_document_job_status?module=95&type=1) | POST JSON job_id; response.job_status: PROCESSING/READY/EXPIRED/FAILED | Matches; PENDING is an additional tolerated state |
| [Job filelink](https://open.shopee.com/documents/v2/v2.logistics.download_shipping_document_job_filelink?module=95&type=1) | POST JSON job_id; top-level file_link; link valid 30 minutes | Matches; nested response.file_link is additionally tolerated |
| [Shipment list](https://open.shopee.com/documents/v2/v2.order.get_shipment_list?module=94&type=1) | GET; required page_size 1–100; optional cursor; response order_list/more/next_cursor | Matches fallback; no order-list time filters are sent |
| [Instant limit](https://open.shopee.com/documents/v2/v2.logistics.set_order_count_limit_outside_instant_operating_time?module=95&type=1) | POST; boolean is_order_limit_enabled; int64 order_count_limit; response update_result | Matches adapter; announcement supplies conditional limit rules |
| [Shipping document push](https://open.shopee.com/push-mechanism/17) | Code 15; order/package/status; duplicates allowed; no guaranteed sequence | Matches routing and canonical receipt deduplication |
| [Package fulfillment push](https://open.shopee.com/push-mechanism/33) | Code 30; ordersn/package_number/fulfillment_status/update_time; duplicates allowed | Matches routing, status summary and receipt identity |

The create-job reference marks SKU quantity optional; this unused ERP helper
requires it explicitly to validate the total label-copy limit before sending.
There is no active unpackaged-SKU caller to migrate. The filelink reference shows
an HTTPS example; the attached announcement also shows HTTP. Both schemes remain
supported with the existing unsigned/public-IP/redirect protections.

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

### Latest results — source commit 69e78fb

- Full repository: **1614 passed, 27 skipped, 4 warnings**, 218.49 seconds.
  Command: `python -m pytest tests/ -n 0 -q -rs`.
- November-update suite independently: **49 passed**, 1 warning, 2.05 seconds.
- All 27 skipped cases require `TEST_DATABASE_URL`, which is not available;
  they are PostgreSQL integration/self-heal checks, not passing tests.
- Repository-wide Ruff, Python compilation and `git diff --check`: passed.
- Runtime code and all tests were unchanged during this full run; only this
  documentation update followed it. No additional implementation fix was needed.
- Warnings are existing crypt deprecation, email-validator/test validation, and
  SQLAlchemy identity-conflict warnings, not failures.
- Python 3.12.14 with repository-pinned requirements in an isolated test venv.
  Docker is unavailable; no container image build was performed.
- PR #504 remained open/unmerged; its head was 69e78fb while testing. The GitHub
  workflow-run lookup returned an empty list. This documentation-only follow-up
  also uses `[skip ci]`; no Actions dispatch or deployment is performed.
- Official reference verification succeeded for seven pages as listed above.
  Authenticated Shopee calls remain unexecuted because this runtime has no
  credentials and the connected gateway has no signed-request/exec capability.
