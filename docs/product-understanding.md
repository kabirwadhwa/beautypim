# Product Understanding and Semantic Consistency

BeautyPIM resolves product meaning before generative enrichment. The lifecycle is:

1. Preserve the complete imported source row and interpret mapped and unmapped source columns by semantic role.
2. Normalize identifiers and attempt indexed exact GTIN/source-identity retrieval from the Knowledge Corpus.
3. Resolve consumer brand, product family, variant, size and taxonomy. Supplier/legal entity and source shorthand remain provenance.
4. Persist a versioned `product_understanding` field-value contract.
5. Build completeness and research gaps from that contract.
6. Give the contract, exact evidence and gaps to enrichment. Generate exactly one applicable category module; unknown never defaults to skincare.
7. Apply deterministic claim, placeholder and cross-module quality gates.
8. Persist attributes, validation issues, evidence and audit history.
9. Reuse the same contract for product detail, completeness, research, assistant retrieval, exports and PDF selection.

## Human identity review exception path

BeautyPIM remains automatic by default. Exact GTIN/corpus identities and other
safe resolved identities continue directly into enrichment without user input.
The canonical identity-review decision is derived from the persisted Product
Understanding contract and dependency-aware gap plan. When foundational
identity is unsafe, only that product pauses and a blocking validation issue
places it in the Product Grid identity-review queue.

The guided review compares preserved source values, current resolved values,
safe candidates, match scope, evidence and conflicts. Editors may use a
suggestion, keep a current value, enter a value manually, save without research,
or skip for now. Confirmation is written through the existing `human_edit`
FieldValue/versioning and audit systems; later automation cannot silently
replace it and contradictory evidence creates a conflict.

`Confirm identity & continue enrichment` refreshes Product Understanding and
completeness, rebuilds the category-aware gap plan, and resumes only the
originally requested improvement mode/fields that remain applicable. Bulk jobs
continue independently while ambiguous products wait in the review queue.
Skipping never marks a product resolved or approved.

## Evidence precedence

Human overrides and explicit customer facts remain protected. Official/verified evidence and exact-product corpus evidence outrank family-safe evidence and inference. Comparable products can inform only safe vocabulary and never establish identity, formulation, claims, price or availability.

## Unknown behavior

An unresolved product receives universal conservative handling and an identity-first research plan. Its completeness is capped, category modules remain absent, and a warning is surfaced. Tokens such as `STD`, `C`, `BOTH`, `N/A` and legal supplier names cannot silently become product facts.
