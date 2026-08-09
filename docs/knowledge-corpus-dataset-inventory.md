# BeautyPIM reference-corpus dataset inventory

Inventory date: 2026-08-09. Counts below were calculated from the downloaded XLSX files, excluding header rows. Raw row count is not presented as product count.

## Source 1 — rich bilingual beauty workbook

- File: `BO RAPPORT OMSCHRIJVINGEN ATTRIBUTEN ONLINE.xlsx`
- SHA-256: `eda369408734bff8c2c191378ddccfa26ea242237c5b66cb4d8ff5dc15425328`
- Size: 12,091,175 bytes
- Languages/locales: Dutch Belgium (`nl_BE`) and French Belgium (`fr_BE`)
- Market: Belgium
- Stable joins: `SKU number`; `Base Product` is the supplied family/parent identifier

| Sheet | Raw rows | Columns | SKU coverage | Unique SKU | Base-product coverage | Unique bases |
|---|---:|---:|---:|---:|---:|---:|
| PRODUCT OMSCHRIJVINGEN | 15,776 | 21 | 15,768 | 15,765 | 15,768 | 8,058 |
| PRODUCT ATTRIBUTEN | 15,768 | 44 | 15,768 | 15,765 | 15,768 | 8,058 |

The description sheet contains bilingual names, descriptions, INCI, directions, three informative-text fields, size and unit. Measured coverage includes 15,083 NL descriptions, 14,402 FR descriptions, 13,383 NL ingredient strings, 12,830 FR ingredient strings, 11,103 NL directions and 10,596 FR directions.

The attribute sheet contains brand (205 unique values), classification (14,990 rows), fragrance family (3,067), skin type (5,687), key ingredient (4,220), free-from (3,427), finish (3,992), product format (7,409), concern/treatment, coverage, body/hair attributes and category flags. `Benefit nl_BE` is present as a column but empty in the downloaded file.

## Source 2 — retail feed 1

- File: local retail-feed source 1 (filename intentionally not published)
- SHA-256: `8bf8dec7c985bb575f10be13fd88a1040d1bf61db80e5009e9eee5a0cc6a9b2b`
- Size: 6,736,882 bytes
- Sheet: `Sheet1`
- Raw rows: 15,505
- Columns: 39
- Language/market/currency: Dutch / NL / EUR
- Identity: 15,505 merchant-product-ID observations, 15,405 unique merchant product IDs; 15,229 EAN-bearing rows, 14,071 distinct EAN values
- Parent structure: 6,647 rows have a parent ID; 1,174 distinct parent IDs
- Coverage: 15,503 brand, 15,292 description, 15,495 primary image, 15,505 category/path, price, URL and availability rows

Important columns include product and merchant IDs, EAN, parent product ID, brand, name, description, retailer category path, price/RRP/base price, currency, availability/stock, primary and alternate images, product URL, locale, last-updated and size.

## Source 3 — variant-heavy retail feed 2

- File: local retail-feed source 2 (filename intentionally not published)
- SHA-256: `c70e0c41bbaca5d5370d02e5b84d876dc5b1f158c51571788d8d8aad59f7344d`
- Size: 31,849,901 bytes
- Sheet: `Sheet1`
- Raw rows: 56,643
- Columns: 47
- Language/market/currency: Dutch / NL / EUR
- Identity: 56,642 populated and unique merchant product IDs; 56,640 EAN-bearing rows, 56,636 distinct EAN values
- Parent structure: 56,642 parent-bearing rows grouped into 32,208 distinct parent IDs
- Coverage: 56,367 brand, 55,390 description, 56,611 primary image, 56,639 category path, 56,643 price and availability, 22,879 size and 12,207 colour rows

Important columns include product, merchant and parent IDs; EAN/UPC/MPN/GTIN; brand/name/description; three-level category data; colour, size and specifications; price/currency; availability/stock; image and product URL; locale and last-updated. Multiple rows under the same parent carry different EANs, colours/shades or sizes and must remain variants.

## Combined interpretation

- Total physical data rows across every downloaded sheet/tab: **103,692** (31,544 rich-workbook sheet rows plus 15,505 and 56,643 feed rows).
- Import-driving product/variant rows after joining the rich workbook's two tabs: **87,916** (15,768 + 15,505 + 56,643).
- These are observations, not 87,916 unique products.
- The rich workbook's two tabs must be joined before counting identities.
- Duplicate EANs, parent IDs and repeated source rows remain independent source observations.
- Prices, availability, images and URLs are market observations. Feed `last_updated` is retained when supplied; otherwise the timestamp is explicitly `dataset_imported_at`.
- No runtime Google Sheets dependency is required after file import.
