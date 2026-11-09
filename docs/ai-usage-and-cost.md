# AI usage and cost estimation

Every paid AI call in Beauty PIM records what it consumed and what it is
estimated to have cost, attributable to a product, an import job, a research
job or a bulk selection.

## What is measured

Token counts come from the AI provider and are exact. Dollar figures are
**estimates** derived from a configured rate per model. They will not match a
provider invoice exactly, and every API response and UI surface says so.

Four kinds of spend are recorded, as the `operation` column:

| Operation | What it is |
|---|---|
| `enrichment` | AI enrichment of a product's attributes |
| `web_research` | Paid web search and evidence discovery (Improve Product) |
| `review_summary` | Review Intelligence synthesis |
| `catalog_assistant` | Catalogue assistant chat turns |

Failed calls are recorded too, with `status="failed"`, because a call that
errored after the provider processed the prompt is still billed.

## Where to see it

**Product detail** — the enrichment diagnostics panel shows tokens and cost
for the latest run, plus lifetime totals across all AI work for that product.

**Import job page** — tokens, estimated cost, cost per product, a breakdown by
operation, and budget consumption for that job.

**Bulk Improve** — a running total for the whole selection while it processes,
and a per-product tokens/cost column in the results table.

## API

All endpoints are under `/api/ai-usage` and require an authenticated internal
viewer or above. `POST /backfill` requires editor or admin.

| Endpoint | Returns |
|---|---|
| `GET /summary?days=N` | Deployment-wide totals, split by operation and model |
| `GET /products/{id}` | One product's totals and its recent calls |
| `GET /import-jobs/{id}` | One import job, with cost per product and budget |
| `GET /research-jobs/{id}` | One research job |
| `POST /research-jobs/aggregate` | Combined totals for up to 100 research jobs |
| `GET /pricing` | The rate and provenance for each configured model |
| `POST /backfill` | Import historical `EnrichmentRun` data (idempotent) |

## How costs are estimated

Rates are USD per 1,000,000 tokens, resolved in this precedence order. Every
response reports which one was used as `pricing_source`.

1. **`override`** — operator-supplied rates from `AI_MODEL_PRICING_JSON`.
   Authoritative. Use this to correct a rate without a deploy:

   ```json
   {"gpt-5-mini": {"input": 0.25, "output": 2.00, "cached_input": 0.025}}
   ```

2. **`reference`** — the seeded table in `app/services/ai_pricing.py`. A
   point-in-time snapshot of published list prices, **not a live feed**.
   Entries flagged `needs_confirmation: true` were never encoded anywhere in
   this codebase and are a best estimate — confirm them against your own
   invoice and set an override. `GET /ai-usage/pricing` shows which models
   carry that flag.

3. **`calibrated`** — derived from this deployment's own history: the observed
   blended dollars-per-token of past runs of the same model. This is what stops
   a newly swapped-in model from silently reporting $0.00. It cannot separate
   input from output rates, so the blended figure is applied to both.

If none of the three yields a rate, the cost is `null` and the response sets
`has_unpriced_calls: true`. **A missing rate never becomes a zero** — the UI
renders it as "not estimated", because $0.00 would read as free.

### Details that affect accuracy

- **Thinking tokens.** Gemini reports `thoughtsTokenCount` separately and
  excludes it from `candidatesTokenCount`, but bills it. It is captured and
  charged at the output rate. Ignoring it understates cost, sometimes heavily.
- **Cached prompt tokens.** The cached portion is charged at the model's
  `cached_input` rate and the remainder at the full rate.
- **Web searches.** Billed as tool calls on top of tokens, at
  `AI_WEB_SEARCH_CALL_USD` per call (default $0.01).

Rates for `gpt-4o-mini` and `gemini-1.5-flash` intentionally reproduce the
hardcoded arithmetic this module replaced, so historical and new figures stay
comparable.

## Budget enforcement

`MAX_JOB_COST_LIMIT` (default $50) caps estimated spend **per import job**.
When a job reaches it, the job stops, remaining rows are cancelled, and the
reason is written to `error_message`. Set it to `0` to disable the cap.

The limit is per job, not global, so one expensive job cannot halt an
unrelated one.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `MAX_JOB_COST_LIMIT` | `50.0` | Per-job estimated spend cap; `0` disables |
| `AI_MODEL_PRICING_JSON` | unset | Rate overrides, USD per 1M tokens |
| `AI_WEB_SEARCH_CALL_USD` | `0.01` | Price per provider web search call |

A malformed `AI_MODEL_PRICING_JSON` is logged as an error and ignored rather
than taking the application down.

## Historical data

`POST /api/ai-usage/backfill` imports existing `EnrichmentRun` rows into the
usage table, using the cost recorded at the time. It is idempotent. Run it once
after deploying so the first view of usage is not an empty chart, and so
calibration has history to work from.

Research, review and assistant spend from before this change cannot be
recovered — it was never persisted anywhere.

## Design note

Usage accounting never breaks the work it accounts for. Write failures are
logged and swallowed, and reading `response.elapsed` cannot raise into a
caller's error handling. A missing usage row is an observability gap; a failed
enrichment because accounting threw would be a product defect.
