# Processing

Processing is admin-only in navigation, the /processing/* route and the /processing API router. Owners and staff cannot read the catalog or import it.

The Data tab contains three reference tables: woodworkers, suppliers and production prices. Imports retain source row IDs, SKU duplicates, dimensions, notes and genuine zero prices. Zero prices are flagged for review. The Order Mapping tab prepares the table layout; no assignments are invented or posted to operational orders.

The catalog is isolated in mpe_processing_catalog, created by the existing additive tenant schema startup. Import is atomic and checksum-idempotent; a different import is rejected once a catalog exists. It never updates operational stock, products, orders, payments or financial snapshots.

Source: the inspected LAP-OKT-2026-SAEPUL workbook. Names come from the worker/product-bill sections of PENGELUARAN; production prices follow Harga Modal in Data. Import provenance includes the source URL, read timestamp and row references. The source workbook is not edited.

The source contains private commercial data and the code repositories are public. Only an AES-256-GCM encrypted source package is shipped; no plaintext catalog or decryption key is committed. The admin's private import link carries the random key in the URL fragment, which is not sent in HTTP requests. Opening it while logged in as admin decrypts the package locally, posts through the admin-only import endpoint and removes the fragment on success. The package is loaded only when that explicit private link is used. A JSON file import is also available. Reopening the same private link is safe and does not overwrite existing data. Keep private keys and raw source exports outside this repository.

## Internal order workflow

All `/processing` routes and UI descendants require the `admin` role. Processing
reads OMS items but never calls Shopee adapters or writes orders, inventory,
payments or accounting. Only additive `mpe_processing_price` and
`mpe_processing_job` tables are added at startup.

* Data: editable production price overrides preserve the original Drive import.
  Painting tariffs are an independent admin-maintained list, inclusive of service
  for reseller billing. Existing work items keep selected product/price snapshots.
* Cek Pesanan selects individual items from processed/shipped/completed Shopee
  orders already in the ERP. A unique source item constraint prevents duplication
  between workers and suppliers, including concurrent clicks. Unselected items
  need no assignment. The popup is paginated and searchable.
* Manual reseller orders stay out of the OMS and can have multiple custom lines,
  quantities and specifications; each line can use a different woodworker and
  custom price. Dates have explicit timezone; UI uses WIB.
* Woodworker: draft → production → painting → packing → ready. Explicit
  `Polos / Tanpa Cat` bypasses painting. An unspecified color cannot be mapped.
* Supplier: draft → supplier → received → packing → ready; no painting. Default
  packing is ordinary. Staff assignment to painters/packers is deferred.
* Finishing uses server timestamps. Reopening the immediately completed step
  retains append-only event history; completed downstream steps must be reopened
  in reverse order. The UI formats timestamps DD/MM/YYYY HH:MM WIB.
* Processing fee is exactly IDR 10,000 per unit, only on reseller woodworker
  items. Painting tariffs do not create wages payable to employees. Packing
  prices are variable per unit (ordinary/wood), zero until entered. On internal
  orders packing is a cost field; on reseller orders it contributes to the quote.
  These records do not yet create financial-report entries or payment journals.
* Quantities are completed as a whole per line; partial allocation/completion and
  remembered cross-order SKU mappings are not part of this first version.
* Optimistic versions reject stale writes. Current cancelled/cancellation-request
  OMS orders are flagged and cannot advance; history is retained.
