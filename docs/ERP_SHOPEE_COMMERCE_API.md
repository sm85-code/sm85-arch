# Shopee commerce integration coverage

Reference: `sm85-code/api-docs` main `77ebf7e17f5bb3a4726d3315baaa1e40cd677382`, copied from `congminh1254/shopee-sdk` `0d38ec6c20587e7b4d60c7f28af98361679dba01` (v2.9.0). Requests use the endpoint schemas and response samples; the TypeScript SDK is not installed in the Python backend.

## Functions available in ERP

| Area | Existing functionality retained | Added in this release | UI location |
| --- | --- | --- | --- |
| Products | Publication, base/model editing, prices, stock, images, dimensions, preorder, unlisting, quality and statistics | Initialize variants, add a missing combination, delete a variant/product, read active promotions and violation/deboost details | Catalog → product detail → Pengelolaan produk Shopee |
| Orders | List/detail, shipping preparation and labels, tracking number, seller cancellation and buyer cancellation handling | Tracking events (optional package number), seller internal note, order escrow breakdown | Orders → Detail |
| Settlement | Released order amounts, shop balance and wallet transactions | Pending/released income list with cursor pagination | Settlement → Pendapatan |
| Advertising | Product campaigns and the 25 Ads operations in the existing registry, Shop GMV Max | Hourly CPC/product campaign metrics; GMV Max report can use Shopee's current campaign without a manual campaign ID | Ads → Performa / Shop GMV Max |
| Shop settings | Authorized marketplace accounts and product logistics metadata | Shop profile, full/partial holiday mode, shipping channel/COD/eligible automatic pickup, address usage | Kelola Toko → Aksi lainnya → Pengaturan Shopee |

## Endpoint mapping added

| Shopee path (`/api/v2/`) | ERP route (`/api/marketplace-erp/`) |
| --- | --- |
| `product/delete_item` | `DELETE katalog-shopee/{id}/shopee` |
| `product/init_tier_variation` | `POST katalog-shopee/{id}/inisialisasi-varian` |
| `product/add_model` | `POST katalog-shopee/{id}/varian` |
| `product/delete_model` | `DELETE katalog-shopee/{id}/varian/{model_id}` |
| `product/get_item_promotion` | `GET katalog-shopee/{id}/informasi-shopee/promosi` |
| `product/get_item_violation_info` | `GET katalog-shopee/{id}/informasi-shopee/pelanggaran` |
| `logistics/get_tracking_info` | `GET pesanan/{id}/pelacakan` |
| `order/set_note` | `PATCH pesanan/{id}/catatan-shopee` |
| `payment/get_escrow_detail` | `GET pesanan/{id}/pendapatan-shopee` |
| `payment/get_income_detail` | `GET akun/{id}/pendapatan-shopee` |
| `ads/get_all_cpc_ads_hourly_performance`, `ads/get_product_campaign_hourly_performance` | `GET akun/{id}/iklan/performa-jam` |
| `shop/get_profile`, `shop/update_profile` | `GET/PATCH akun/{id}/pengaturan-shopee/profil` |
| `shop/get_shop_holiday_mode`, `shop/set_shop_holiday_mode` | `GET/PUT akun/{id}/pengaturan-shopee/libur` |
| `logistics/get_channel_list`, `logistics/update_channel` | `GET akun/{id}/pengaturan-shopee/jasa-kirim`; `PATCH .../jasa-kirim/{channel_id}` |
| `logistics/get_address_list`, `logistics/set_address_config` | `GET/PUT akun/{id}/pengaturan-shopee/alamat` |

## Contract and behavior details

- Provider credentials/signatures stay server-side. Product, advertising, income and shop settings operations require admin; tracking and internal notes also allow owner/staff, scoped to their assigned shops. Requests reject a local order that is not linked to Shopee.
- Mutations do not retry automatically. No-response-body mutations require Shopee's explicit empty error and request ID. New model responses must contain exactly the requested combinations and unique positive model IDs. Existing live model IDs and mandatory shipping channels are checked before changing them.
- Deleting a Shopee product retains its local catalog row, order references and ERP master inventory; only its marketplace status and linked listings are deactivated. Variant writes refresh Shopee catalog data through the existing refresh/warning flow.
- Seller internal `note` is distinct from buyer `message_to_seller`; changing it preserves the buyer's note.
- `get_income_detail` returns a top-level `income_detail_list` object. Its actual IDR response sample uses `list`, whereas the field table calls this `income_detail_list_item`; both are accepted. Missing monetary values remain missing, not zero. Released income requires an ordered date range with a maximum 14-day difference. Pending income includes all pending entries according to Shopee, even though the endpoint still receives date fields.
- `tests/fixtures/shopee_income_detail.json` is the first response sample value from reference `schemas/v2.payment.get_income_detail.json`. Tests use this sample, mocked provider calls and SQLite; no live account mutations occur during verification.
- Dates and hourly UI labels use WIB. Holiday schedules start on the hour and end on second 59:59 before the selected ending hour.
- Address settings change the usage of an existing Shopee address, not its street/name/contact details. Create or edit physical address details in Seller Centre. The UI initializes one tier; the API supports full combinations for one or two tiers. Add extra option names in the existing product editor before adding missing combinations.

## Scope and verification limits

This release covers local seller workflows for Indonesian ERP accounts. The copied repository has hundreds of schemas; they are not all exposed as ERP actions. Cross-border payout, FBS/warehouse-specific workflows, market-specific/Mall features and the Brazil-only Business Insights hot-listing endpoint are outside this release. Availability of any endpoint still depends on the app's Shopee permissions and the shop's eligibility.

Synchronization behavior is unchanged. Build, automated contracts and browser flows validate the implementation; deployment and production Shopee permissions must be checked separately after release.
