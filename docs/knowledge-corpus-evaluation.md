# Knowledge corpus final adversarial validation

Validation date: 2026-08-09. Results come from migration revision `d4a91e72bc10`, a clean SQLite database, and complete imports of the three local XLSX sources. Source files are excluded from Git.

## Release-gate fixes

- Non-EAN rich-workbook variants now use dataset-scoped source SKU identity. Range/size labels can no longer attach one SKU's INCI to another SKU.
- Cross-dataset exact-EAN bridges reconcile source families while retaining every observation and variant.
- Source product and parent identifiers are dataset-scoped unless globally unambiguous.
- Brand/name/size matching returns ambiguous/family evidence instead of an arbitrary exact match when more than one candidate exists.
- Exact EANs with contradictory identity evidence return `conflict`; they cannot suppress external research.
- Comparable examples exclude INCI, ingredients, claims, shade, identifiers, market observations and other direct-only facts.
- Missing stock data remains unknown instead of being converted to `out_of_stock`.
- Apostrophes and the safe aliases YSL/Yves Saint Laurent and Kiehl's/Kiehl's Since 1851 normalize consistently.
- Startup now fails closed when Alembic migration fails.

## Final clean import

| Metric | Count |
|---|---:|
| Import-driving source rows | 87,916 |
| Retained source observations | 59,280 |
| Excluded/unidentifiable rows | 28,636 |
| Normalized product/family identities | 28,980 |
| Normalized variants | 54,089 |
| Unique normalized EANs | 36,889 |
| Dataset-scoped source parent IDs | 26,475 |
| Unique normalized brands | 1,124 |
| Formulations | 13,404 |
| Description observations | 57,380 |
| Category observations | 58,532 |
| Market observations | 43,542 |
| Price observations | 43,542 |
| Availability observations | 43,542 |
| Image observations | 43,508 |
| Repeated EAN source observations | 5,185 |
| Open evidence conflicts | 13,890 |
| Formulation conflicts | 0 |
| Failed rows | 0 |

The higher variant count and zero formulation conflicts are intentional safety corrections. The earlier importer collapsed distinct rich-workbook SKUs on weak range/size identity. In a deterministic sample of 100 of the former 1,229 formulation conflicts, all 100 joined multiple source SKUs to one old variant. After the fix, all 13,404 formulations remain, but each is attached only to its supported SKU variant.

## Exclusion audit

| Reason | Count | Sample audited | False exclusions |
|---|---:|---:|---:|
| Non-beauty merchandise | 28,573 | 100 overall + 20 bucket | 0 |
| Unsupported wellness/sports supplements | 33 | 20 | 0 for current BeautyPIM scope |
| Missing usable brand/name identity | 30 | 20 | 0 recoverable identities |
| Failed/malformed rows | 0 | all | 0 |

The original filter did miss valid beauty-adjacent records. The correction recovered 552 observations across home fragrance/candles, toiletry bags, beauty/bathroom accessories, massage/scalp tools and strongly identified beauty products filed under generic seasonal/lifestyle departments. The final 100-row overall sample contained non-beauty jewellery, clothing and bags. Beauty supplements remain deliberately outside the current product scope; this is a product-scope decision, not an invalid-EAN rejection.

## Conflict audit

Normal price, availability and image changes are historical market observations, not conflicts.

| Field | Count | Severity |
|---|---:|---|
| Product name / exact identity wording | 5,037 | High |
| Brand / exact identity | 199 | High |
| Category | 3,566 | Medium |
| Subcategory/source taxonomy | 4,729 | Medium |
| Product type | 359 | Medium |
| INCI/formulation | 0 | High |
| Price/availability/image | 0 conflicts; retained as observations | Low/temporal |

High-severity conflicts total 5,236 and medium-severity conflicts total 8,654. The high-severity sample contained both safe naming aliases and genuine source problems such as an EAN used for a bundle in one observation and a single item in another. These are preserved, exposed as conflicts and no longer treated as sufficient exact evidence.

## Non-EAN and identity evaluation

Deterministic stratified holdout, 200 records:

| Match path | Sample | Correct | Ambiguous/unmatched | Incorrect |
|---|---:|---:|---:|---:|
| Dataset-scoped exact source product ID | 80 | 100% | 0% | 0% |
| Dataset-scoped exact parent ID | 60 | 100% | 0% | 0% |
| Brand + normalized name + size/shade | 60 | 38.33% | 61.67% | 0% |

The conservative name path intentionally returns ambiguity for generic names such as “Eau de Parfum”. An earlier holdout found one false exact match caused by applying SQL LIMIT before ambiguity detection; the query was fixed and the repeated holdout produced zero incorrect matches.

## Parent/variant and brand audit

- Dataset parent IDs split across multiple knowledge families: **0** after reconciliation (481 before the fix).
- EANs split across multiple knowledge products: **0**.
- Non-EAN variants containing multiple source SKUs: **0**.
- Distinct shades, sizes, concentrations and EANs remain separate variants.
- Obvious alias normalization reduced normalized brands from 1,126 to 1,124 and removed 226 formatting/alias conflicts.
- Potential sub-brand pairs including Rimmel/Rimmel London, Shu Uemura/Shu Uemura Art of Hair, Biotherm/Biotherm Homme and Armani/Giorgio Armani remain separate or conflicted pending human policy; they were not merged blindly.

## Evidence hierarchy safety

Dedicated and legacy comparable retrieval now remove direct-only facts. Automated tests verify that comparable products expose no formulations, market observations, GTIN, ingredients, claims, certifications, shade or exact variant identity. Exact identity conflicts set `match_level=conflict`, appear in evidence summaries and force research/review rather than automatic acceptance.

## Enrichment evaluation

Stratified exact-EAN sample: 100 records spanning skincare, haircare, makeup, fragrance and additional beauty categories.

| Measure | Result |
|---|---:|
| Exact GTIN retrieval | 100% |
| Correct variant retrieval | 100% |
| Incorrect variant retrieval | 0% |
| Mean exact evidence fields | 9.17 |
| Exact-EAN formulation coverage | 0% |
| Mean indexed lookup latency (SQLite) | 4.20 ms |
| P95 indexed lookup latency (SQLite) | 9.22 ms |
| Theoretical searches avoidable after conflict gate | 85% |

The 85% figure is an evaluated local decision rate, not measured production spend reduction. Observed external-search cost reduction remains unmeasured until production traffic is sampled. Exact-EAN formulation coverage remains zero because the rich INCI workbook has no EAN bridge and the two EAN feeds have no INCI. SKU-exact formulations remain usable; family INCI is never promoted to unsupported EAN certainty.

## Migration and idempotency

- Clean migration to `d4a91e72bc10`: passed.
- Existing seeded database migration from `c3f2a18d9b41`: preserved 1 user, 1 canonical product, 1 variant and 1 formulation; corpus tables started empty.
- Repeating all three full imports returned the same completed import jobs and unchanged counts.
- Customer canonical products remained isolated from corpus imports.

Production PostgreSQL latency, query plans, application smoke tests and actual web-search reduction must be measured after deployment; local SQLite results are not presented as production performance.
