# Processing

Permissions follow the role editor's page checkboxes. Pemetaan Order has four pages:
`processing_tukang`, `processing_supplier`, `processing_pengecatan`, and `processing_packing`.
There is no parent `processing` permission. Navigation and the page open when any tab has
`baca` or `tulis`. Default roles and existing role data are unchanged.

Every `/processing` endpoint uses a mapped page guard, with no fixed-role fallback;
unmapped endpoints fail closed. GET `/processing/data` and shared prices, colors,
structure, and supplier lookups require a readable mapping tab or relevant Data Produksi
page (the `LIHAT` set in `peta_akses.py`). Jobs require a readable mapping tab and return
only jobs belonging to readable tabs. Candidates and their recap require Tukang or
Supplier read access. All job writes require `tulis` on the exact route/stage tab.
Historical time corrections additionally retain their admin/owner restriction.

Data Produksi writes require their own page: directories (`data_tukang`, `data_supplier`),
wood structure (`data_produk_kayu`), prices (the page of the price kind), supplier
connection (`data_katalog_otomatis`), or templates (`data_template`). Template reads
also require `data_template`; a mapping tab alone does not grant template access.

The catalog is isolated in `mpe_processing_catalog`. Existing imported values preserve
source row IDs, duplicate SKUs, dimensions, notes and zero prices. The old one-time
`/processing/import` endpoint is removed.

Source: the inspected LAP-OKT-2026-SAEPUL workbook. Names come from the worker/product-bill sections of PENGELUARAN; production prices follow Harga Modal in Data. Import provenance includes the source URL, read timestamp and row references. The source workbook is not edited.

The source contains private commercial data. Only an AES-256-GCM encrypted source
package remains in the frontend; no plaintext catalog or decryption key is committed.
Keep private keys and raw source exports outside this repository. Historical import
links are not a supported way to write data now that the import endpoint is removed.

## Internal order workflow

Access follows the page permissions described above. Processing
reads OMS items but never calls Shopee adapters or writes orders, inventory,
payments or accounting. Only additive `mpe_processing_price` and
`mpe_processing_job` tables are added at startup.

* Data: editable production price overrides preserve the original Drive import.
  Painting tariffs are an independent list maintained by page writers, inclusive of service
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

### Editable directory

Users with `tulis` on the corresponding directory page may add or edit woodworkers and suppliers. Overrides use the
additive `mpe_processing_party` table; the original Drive payload is unchanged.
Optimistic versions reject stale edits, and duplicate names within each directory
are rejected. New mappings resolve the edited directory; existing job name/price
snapshots are retained. Users without the corresponding page permission remain denied.
