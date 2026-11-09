/**
 * Formatting for AI token counts and estimated costs.
 *
 * A null cost means "we have no rate for this model", which must never be
 * displayed as $0.00 — that would read as free. It shows as "not estimated"
 * instead.
 */

export type UsageTotals = {
  prompt_tokens: number;
  completion_tokens: number;
  cached_prompt_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
  web_search_calls: number;
  estimated_cost_usd: number;
  call_count: number;
  has_unpriced_calls: boolean;
  is_estimate: boolean;
};

export type UsageBreakdown = {
  operation: string;
  total_tokens: number;
  estimated_cost_usd: number;
  call_count: number;
};

export type UsageBudget = {
  limit_usd: number | null;
  spent_usd: number;
  remaining_usd: number | null;
  used_fraction: number | null;
  exceeded: boolean;
};

export type UsagePayload = {
  totals: UsageTotals;
  by_operation: UsageBreakdown[];
  models?: Array<{ model: string; pricing_source: string; pricing_source_label: string }>;
  products_touched?: number;
  cost_per_product_usd?: number | null;
  budget?: UsageBudget;
  estimate_note?: string;
};

/** Human labels for the operation slugs stored on each usage row. */
export const OPERATION_LABELS: Record<string, string> = {
  enrichment: 'AI enrichment',
  web_research: 'Web research',
  review_summary: 'Review synthesis',
  catalog_assistant: 'Catalogue assistant',
  other: 'Other AI work',
};

export function operationLabel(operation: string): string {
  return OPERATION_LABELS[operation] ?? operation;
}

/** Compact token counts: 1234 -> "1,234", 1250000 -> "1.25M". */
export function formatTokens(tokens: number | null | undefined): string {
  if (tokens == null) return '—';
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(2)}M tokens`;
  if (tokens >= 10_000) return `${(tokens / 1_000).toFixed(1)}k tokens`;
  return `${tokens.toLocaleString()} tokens`;
}

/**
 * Money, with enough precision to stay meaningful at these magnitudes. A
 * single enrichment can cost a fraction of a cent, so rounding everything to
 * two decimals would show most real calls as $0.00.
 */
export function formatUsd(amount: number | null | undefined): string {
  if (amount == null) return 'not estimated';
  if (amount === 0) return '$0.00';
  if (amount < 0.01) return `$${amount.toFixed(5)}`;
  if (amount < 1) return `$${amount.toFixed(4)}`;
  return `$${amount.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/** The standing caveat shown anywhere a dollar figure appears. */
export const ESTIMATE_NOTE =
  'Token counts come from the AI provider. Dollar figures are estimates based ' +
  'on the configured rate for each model and will not match an invoice exactly.';
