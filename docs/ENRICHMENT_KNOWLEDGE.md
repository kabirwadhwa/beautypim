# Enrichment knowledge strategy

Beauty PIM uses retrieval-grounded enrichment rather than treating a language
model's general knowledge as a product database.

## Approved starting source

- **European Commission CosIng** supplies INCI names, cosmetic functions and
  identifiers. CosIng is informative and non-legally binding. A listing must
  never be converted into a safety, approval, compliance, efficacy or marketing
  claim.
- Synchronize a small sample with
  `cd backend && python scripts/sync_cosing.py --pages 1`. Use `--pages 0` for a
  full synchronization after reviewing the current Commission service limits.

Only exact normalized ingredient-name matches enter the model context. Every
match retains source name, URL and source record ID. Unknown data remains
unknown.

The custom policy is configured with `ENRICHMENT_CUSTOM_INSTRUCTIONS`. Its
default requires source-supported facts, prohibits concentration inference and
prevents glossary functions from becoming efficacy, safety, compliance or
marketing claims. Deployments may add stricter organization-specific guidance.

## Retailer and brand catalogues

Sephora, Nocibé, Marionnaud and brand sites are valuable product sources, but
their descriptions, images and catalogue feeds are not training data by
default. Add each source only through one of these routes:

1. a documented API or affiliate/product feed whose terms permit this use;
2. a client-provided licensed export;
3. explicit written permission from the catalogue owner.

Retailer facts must retain source URL, retrieval time, market and language.
Claims remain attributed brand/retailer claims rather than objective facts.
Connectors must respect rate limits and deletion/correction requests.

## Next stages

1. Build an evaluation set from client-approved product rows and corrected
   outputs, including hard negatives and unknown fields.
2. Add authorized retailer connectors behind a common provenance contract.
3. Measure field accuracy, evidence accuracy, unsupported-claim rate and
   coverage before considering fine-tuning.
4. Fine-tune only for stable extraction behavior after enough reviewed labels
   exist; continue retrieval for changing catalogue facts.
