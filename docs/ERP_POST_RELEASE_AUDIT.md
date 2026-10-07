# ERP post-release audit — 7 October 2026

The combined implementation was merged in Backend #321 and Frontend #64. This follow-up reviewed the integrated code, provider contracts against the cached Shopee API references, and browser behavior with intercepted API fixtures. No live marketplace operation or deployment was performed.

## Corrected findings

- Publication metadata failures previously left the browser with a locked draft and no durable receipt. Preflight now has a receipt before metadata reads; confirmed pre-write failures return `belum_dikirim`. The browser retains the draft and unlocks it for correction. Unknown transport/write results retain the operation ID and remain locked to prevent duplicate publication.
- A confirmed publication result is committed before optional snapshot refresh; malformed/delayed snapshots produce a warning instead of hiding confirmed success.
- Variant initialization must confirm exact tier combinations and distinct positive model IDs before activation.
- `add_item` size-chart fields belong inside `size_chart_info`. Free-text attribute value ID 0 must permit custom text even when metadata includes an “Others” sentinel. Editing attribute values preserves the selected unit.
- Shopee success responses can contain whitespace in `error`. The signed adapter trims it, blocks redirects, rejects non-2xx success-shaped responses, and hides signed URLs in transport errors. HTTP 5xx remains uncertain rather than a definitive provider rejection; request IDs are preserved when supplied.
- Image-level upload errors are checked even when a legacy image ID is also present. Duplicate uploaded image IDs are not added twice to a draft.
- Warehouse and settlement read failures now show an error and retry control instead of silently presenting empty data.
- A native fieldset minimum width caused a populated variant table to expand the entire mobile page. The fieldset now permits the table's own horizontal scrolling.

## Verification

- Browser smoke audit of 15 routes at desktop 1440×1000 and mobile 390×844: no JavaScript crashes, no document overflow with empty fixtures, beginner guidance present on menu pages.
- Populated mobile publication matrix: document width reduced from 1448 to 390 pixels; table preserved.
- Failed settlement reads: visible API errors; no misleading “Belum ada dana cair” state.
- Publication browser scenarios: confirmed preflight failure and HTTP 422 preserve editable drafts; uncertain HTTP 503 retains the operation lock.
- Backend focused regressions cover durable preflight receipts, duplicate model IDs, nested size-chart payloads, custom attribute sentinels, whitespace success, transport privacy, and existing shipping behavior.
- Frontend lint, 116 tests, and production build passed. Existing lint warnings remain; they are not runtime failures. Full Backend and PostgreSQL integration checks run in PR CI.

## Scope and limits

Mobile remains table-based, ERP models remain marketplace-neutral, and existing AI behavior is unchanged. Actual shop permissions, live Shopee responses, deployment, and production-data behavior are not established by mocked checks. Partial/uncertain publication completion, product videos, and subsequent return negotiation retain the documented Seller Centre handoff. This audit does not claim every Shopee endpoint is implemented.
