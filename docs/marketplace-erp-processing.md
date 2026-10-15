# Processing

Processing is admin-only in navigation, the /processing/* route and the /processing API router. Owners and staff cannot read the catalog or import it.

The Data tab contains three reference tables: woodworkers, suppliers and production prices. Imports retain source row IDs, SKU duplicates, dimensions, notes and genuine zero prices. Zero prices are flagged for review. The Order Mapping tab prepares the table layout; no assignments are invented or posted to operational orders.

The catalog is isolated in mpe_processing_catalog, created by the existing additive tenant schema startup. Import is atomic and checksum-idempotent; a different import is rejected once a catalog exists. It never updates operational stock, products, orders, payments or financial snapshots.

Source: the inspected LAP-OKT-2026-SAEPUL workbook. Names come from the worker/product-bill sections of PENGELUARAN; production prices follow Harga Modal in Data. Import provenance includes the source URL, read timestamp and row references. The source workbook is not edited.

The source contains private commercial data and the code repositories are public. Only an AES-256-GCM encrypted source package is shipped; no plaintext catalog or decryption key is committed. The admin's private import link carries the random key in the URL fragment, which is not sent in HTTP requests. Opening it while logged in as admin decrypts the package locally, posts through the admin-only import endpoint and removes the fragment on success. The package is loaded only when that explicit private link is used. A JSON file import is also available. Reopening the same private link is safe and does not overwrite existing data. Keep private keys and raw source exports outside this repository.
