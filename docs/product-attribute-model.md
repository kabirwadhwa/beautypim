# BeautyPIM product attribute model (v3)

BeautyPIM separates product knowledge into four layers:

1. **Canonical identity and content** — brand, product name, identifiers, variant, size, category, description and images.
2. **Commercial enrichment** — classification, exactly three target-audience profiles, positioning, benefits, concerns, directions, sensory description and structured claims.
3. **Category module** — exactly the relevant skincare, haircare, makeup or fragrance attributes.
4. **Evidence and observations** — field provenance, enrichment-run metadata, overrides/history, plus retailer/market observations such as price, availability, ratings and source URLs.

Claims use an extensible list with `verified`, `source_supported`, `unverified`, `conflicting` or `unknown` status. Positive claims require evidence. Warnings use typed factual observations and are not medical advice or certification.

Ingredient formulations retain exact raw INCI and ordered normalized relationships. Product-facing ingredient intelligence includes order, functions, benefits/utilities, cautions and key-ingredient status. Aliases, normalization identifiers, confidence and evidence stay in the ingredient/evidence layer.

Legacy attributes are migrated non-destructively. Their old `field_values` rows remain as historical versions (`is_current=false`), while current consolidated values are created for claims, concerns, warnings, sensory description, directions and category modules. Original `source_listings.raw_data` is never reduced by this migration.

Uploaded feeds expose Product Name and Brand as required fields. Size may contain its unit (for example `100 ml`); legacy separate unit columns continue to work. Product family/type/subcategory and retailer columns remain usable as raw evidence and legacy mappings, but Category is the primary classification mapping and source name belongs to import-level metadata.
