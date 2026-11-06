# EasyBoss evidence and field mapping

## Verified from accessible implementation, not from live API

Source revision: [serah-terima-ai-ops@473668fd8234ee3df15e15a155f590737b5eb3ae](https://github.com/sm85-code/serah-terima-ai-ops/tree/473668fd8234ee3df15e15a155f590737b5eb3ae).

Files: `99-Alat/easyboss-monitor/monitor.py`, `99-Alat/easyboss_pesanan.py`, `02-Aplikasi/EasyBoss-Monitor.md`, `03-Integrasi/EasyBoss-Pesanan.md`.

| Native contract | Evidence |
| --- | --- |
| Auth | Existing Playwright cookie session; `POST /api/auth/account/getAccountInfo`, result success; not an ERP credential |
| Package search | `POST https://www.easyboss.com/api/order/package/searchOrderPackageList`, form `page`, `pageSize=100`, `appPackageTab`, `sortField=gmtOrderStart`, `sortType=asc` |
| Response | Business success requires `result == "success"`; `packageList`, `total` are top-level |
| Shop counts | `POST /api/order/package/getShopAndOrderCountMap`, form `appPackageTab`, `platform=""`, `isShopGroup=0`; top-level `shopAndCountMap` entries: `shopId`, `shopName`, `platform`, `orderCount` |
| Package identity | `opOrderPackageId`; do not treat this as original order ID |
| Shop identity | package `shopId` |
| Order identity/platform/date | `orderInfo.platformOrderSn`, `orderInfo.platform`, `orderInfo.gmtOrderStart` |
| Deadline | package `gmtLastDelivery` or `orderInfo.gmtLastDelivery`; may be absent |
| Time zone | Monitor explicitly interprets naive gmt* as UTC+8, displays UTC+7 WIB; `native_time` converts strictly to UTC |
| Tabs | waitProcess, waitShip, submitPlatform, waitReceiverConfirm, isolation, finished, closed |

**Not verified:** authoritative directory (including shops with no orders), marketplace native shop ID, item detail endpoint/shape, product/variant SKU fields, recipient/buyer fields, item/order notes, shipping fields, actual payment eligibility, full order package membership, API rate limits. `closed` includes after-sales/return semantics and must not be blindly mapped to cancelled. `waitProcess` alone does not prove paid. A gateway must explicitly resolve these from live read evidence.

## Implemented adapter interface

`Gateway.shops() -> list[Shop]`, `Gateway.orders(since) -> list[Order | RejectedOrder]`. Application services also accept an in-process adapter exposing those async methods (used by fixtures).

Optional HTTP transport: HTTPS GET `<base>/shops` and `<base>/orders`, bearer token, no redirects. Response:

```json
{"contract":"bridge-v1","success":true,"items":[],"next_cursor":null}
```

`next_cursor` is an opaque nonempty string or null, bounded to 1,000 pages of at most 200 records. Repeated cursors abort the scan. Gateway must return a consistent complete order snapshot, not paginated package fragments. Transport retries timeout, 429 and 5xx at most three times; 401/403 and business/contract errors stop immediately. Neither upstream request headers nor response bodies are emitted to logs. Any EasyBoss auth/captcha refresh is upstream's responsibility.

`Shop` fields: `platform` (canonical `lazada`, `tiktokshop`; unsupported platforms are skipped), `shop_id`, optional `marketplace_shop_id`, `name`, `active`. The full directory must include zero-order shops. Do not derive active status from a missing count. Duplicate stable IDs abort shop sync. Stable IDs take priority over names. Same-name legacy records require explicit reviewed linking; no fuzzy auto-linking.

`Order` fields are defined strictly in `infrastructure/adapters/easyboss.py`. Times must be timezone-aware ISO datetimes, nullable when absent. Complete snapshots require every original order item exactly once, each with a stable original line identity, including all packages. The gateway must deduplicate package repetitions without summing repeated order-level quantities. It must explicitly map paid/ready based on observed upstream status, including COD semantics. Until that is known, emit `unknown`/null and no import occurs.

## Canonical adapter → existing ERP manual payload

| Canonical field | ERP field / handling |
| --- | --- |
| platform + shop_id | Resolve stable linked active internal `toko_id`; no name matching at import |
| platform + shop_id + order_id | DB unique bridge identity; SHA-256 deterministic `kode_pesanan` (`EB-…`) prevents collisions with manual/global codes |
| order_id | `reference`, also retained unchanged in source metadata |
| easyboss_order_id, package_ids | Source metadata only; never replace original order identity |
| buyer | `pemesan`; absent buyer blocks import rather than substitute shop name |
| ordered_at | `ordered_at`, offset required |
| deadline | `deadline`, offset required and >= order date; null → `missing_deadline` failure |
| items[].product | items[].produk |
| items[].variant | items[].varian; empty is allowed when no variant supplied |
| items[].quantity | items[].qty, strict positive integer |
| items[].note, buyer_note | Joined into items[].note with newlines; overflow rejected rather than truncated |
| items[].item_id, product_sku, variant_sku | Original source metadata; no production product mapping is introduced |
| status, source_status, paid, complete | Eligibility + audit; no arbitrary numeric EasyBoss status guesses |
| carrier, tracking_number | Audit metadata; do not change shipping/fulfilment workflow automatically |

All allowlisted original canonical metadata is retained in `Pesanan.detail_json.easyboss` on successful import, with the existing `manual` detail preserved. This is not claimed to be a raw native response archive: raw API responses can contain credentials or unnecessary PII, and unverified native fields are not persisted. Invalid individual item payloads with usable source identity produce `invalid_source_order` receipts while valid orders continue; an invalid identity aborts the scan because it cannot be safely retried/deduplicated.

Cancellation before import is skipped. Cancellation/status/items/shipping changes after import produce a review receipt and source-hash/status audit event, preserving original imported metadata and production work. Dashboard shows both failed receipts and review receipts; retry targets only failed receipts and fetches fresh data.
