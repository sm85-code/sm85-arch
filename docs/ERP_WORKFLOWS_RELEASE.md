# Marketplace workflows

The ERP remains marketplace-neutral. The current provider adapter is Shopee; other provider integrations are not silently treated as Shopee. Existing SKU inventory, orders, settlements and AI behavior are retained.

## User entry points

- Katalog → Buat / Salin Produk: create an initially hidden listing, optionally copying a live source shop product. Select destination category, attributes, brand, logistics, images, size chart, physical fields and preorder. A full cartesian variant matrix has separate SKU/price/stock/weight/dimension/preorder/GTIN fields. Copy uses original prices and zero destination stock; stock must be intentionally entered. Destination metadata is revalidated before creation.
- Pesanan → Retur & Refund → Detail: approve an existing solution or request the provider's current dispute reasons and requirements, upload photos and submit a dispute. Video and subsequent negotiation remain in Seller Centre. No automatic inventory restock or settlement entry.
- Settlement → Transaksi Saldo Penjual: read wallet movements, fees and withdrawals, with full WIB days up to 15 days and documented offset pagination. This projection is not added to escrow revenue or manual settlement totals. Missing currency is labeled, not inferred as IDR.
- All authenticated pages contain an expandable beginner guide. Mobile keeps horizontally scrollable tables; shared dialogs fit the viewport.

## Publication recovery

`mpe_publikasi_marketplace` is an additive receipt table created through the existing startup metadata initialization. The unique key is `(akun_id, operation_id)`. A hash prevents reusing the same key for a different payload. The receipt is committed before `add_item`; a repeated key only reads the recorded outcome and never sends the parent creation again, even after timeout or process interruption.

Parent creation requests UNLIST. The confirmed item ID is immediately persisted before variant initialization. Initialization waits the documented minimum 5 seconds, sends `standardise_tier_variation`, and requires complete, distinct model indices and IDs. Activation follows only confirmed initialization. Confirmed writes followed by a delayed snapshot produce a warning, not a second creation. Partial/uncertain operations expose the receipt and known item ID; users inspect or finish that existing listing in Seller Centre. There is no automatic remote replay or retry.

The FE stores the pending operation and draft in per-user session storage for tab reload recovery. Photos are uploaded, not downloaded from user URLs. Uploads are bounded to 10 MB JPG/PNG, use partner signing for public media and shop signing for return evidence, and do not follow redirects.

## Validation and release

Disputes validate fresh provider reason/module requirements, including required evidence. The server constructs the requirement text and verifies Shopee-hosted HTTPS URLs without fetching them. Account access and roles precede remote calls. Wallet order links are scoped to the selected account. Shopee Ads writes reject nonfinite values, incomplete confirmations and per-keyword/item failures.

References were checked against the user's api-docs repository and cached Open Platform endpoint documentation. Automated tests mock all provider writes; no live marketplace action was performed. Real shop permissions and regional requirements continue to be reported through provider errors with request IDs.

CI runs on pull requests and main; duplicate push runs on feature branches were removed. Full backend tests and PostgreSQL integration remain required. Deploy backend before frontend so the new endpoints exist; no existing endpoint or column was removed.
