'use client';

import React from 'react';
import { Coins } from 'lucide-react';
import {
  ESTIMATE_NOTE,
  UsagePayload,
  formatTokens,
  formatUsd,
  operationLabel,
} from '../usage';

/**
 * Token and estimated-cost summary for a job, a product or a bulk selection.
 *
 * Deliberately shows the dollar figure next to the token count it was derived
 * from, and labels it an estimate, so nobody reads it as a billed amount.
 */
export default function UsagePanel({
  usage,
  title = 'AI usage & estimated cost',
  compact = false,
}: {
  usage?: UsagePayload | null;
  title?: string;
  compact?: boolean;
}) {
  const totals = usage?.totals;
  if (!totals || totals.call_count === 0) {
    if (compact) return null;
    return (
      <div
        style={{
          border: '1px solid #1e293b',
          borderRadius: 10,
          padding: 14,
          background: '#0b1220',
          color: '#64748b',
          fontSize: 12,
        }}
      >
        No paid AI calls have been recorded for this yet.
      </div>
    );
  }

  const budget = usage?.budget;
  // Warn before the cap bites, not only after the job has already stopped.
  const nearLimit =
    budget?.used_fraction != null && budget.used_fraction >= 0.8 && !budget.exceeded;

  if (compact) {
    return (
      <span style={{ color: '#94a3b8', fontSize: 12 }} title={ESTIMATE_NOTE}>
        {formatTokens(totals.total_tokens)} ·{' '}
        <strong style={{ color: '#fbbf24' }}>
          {formatUsd(totals.estimated_cost_usd)}
        </strong>{' '}
        est.
      </span>
    );
  }

  return (
    <div
      style={{
        border: '1px solid #1e293b',
        borderRadius: 10,
        padding: 16,
        background: '#0b1220',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          color: '#e2e8f0',
          fontWeight: 600,
          fontSize: 13,
          marginBottom: 14,
        }}
      >
        <Coins size={15} color="#fbbf24" />
        <span>{title}</span>
      </div>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))',
          gap: 14,
          fontSize: 12,
        }}
      >
        <Metric label="Total tokens" value={formatTokens(totals.total_tokens)} />
        <Metric
          label="Estimated cost"
          value={formatUsd(totals.estimated_cost_usd)}
          accent
        />
        <Metric label="AI calls" value={totals.call_count.toLocaleString()} />
        {usage?.products_touched != null && (
          <Metric
            label="Products"
            value={usage.products_touched.toLocaleString()}
          />
        )}
        {usage?.cost_per_product_usd != null && (
          <Metric
            label="Cost per product"
            value={formatUsd(usage.cost_per_product_usd)}
          />
        )}
        {totals.web_search_calls > 0 && (
          <Metric
            label="Web searches"
            value={totals.web_search_calls.toLocaleString()}
          />
        )}
      </div>

      {totals.prompt_tokens + totals.completion_tokens > 0 && (
        <div style={{ marginTop: 12, fontSize: 11, color: '#64748b' }}>
          {formatTokens(totals.prompt_tokens)} in ·{' '}
          {formatTokens(totals.completion_tokens)} out
          {totals.reasoning_tokens > 0 && (
            <> · {formatTokens(totals.reasoning_tokens)} thinking</>
          )}
          {totals.cached_prompt_tokens > 0 && (
            <> · {formatTokens(totals.cached_prompt_tokens)} cached</>
          )}
        </div>
      )}

      {usage?.by_operation && usage.by_operation.length > 1 && (
        <div style={{ marginTop: 14 }}>
          <div style={{ color: '#64748b', fontSize: 11, marginBottom: 6 }}>
            WHERE IT WENT
          </div>
          {usage.by_operation.map((row) => (
            <div
              key={row.operation}
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                padding: '4px 0',
                fontSize: 12,
                borderBottom: '1px solid #131c2e',
              }}
            >
              <span style={{ color: '#cbd5e1' }}>
                {operationLabel(row.operation)}
                <span style={{ color: '#475569' }}> ×{row.call_count}</span>
              </span>
              <span style={{ color: '#94a3b8' }}>
                {formatTokens(row.total_tokens)} ·{' '}
                {formatUsd(row.estimated_cost_usd)}
              </span>
            </div>
          ))}
        </div>
      )}

      {budget?.limit_usd != null && (
        <div style={{ marginTop: 14 }}>
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              fontSize: 11,
              color: budget.exceeded ? '#ef4444' : nearLimit ? '#fbbf24' : '#64748b',
              marginBottom: 4,
            }}
          >
            <span>
              Budget {formatUsd(budget.spent_usd)} of {formatUsd(budget.limit_usd)}
            </span>
            {budget.exceeded && <strong>LIMIT REACHED</strong>}
            {nearLimit && <strong>NEARING LIMIT</strong>}
          </div>
          <div
            style={{
              height: 4,
              borderRadius: 2,
              background: '#1e293b',
              overflow: 'hidden',
            }}
          >
            <div
              style={{
                height: '100%',
                width: `${Math.min(100, (budget.used_fraction ?? 0) * 100)}%`,
                background: budget.exceeded
                  ? '#ef4444'
                  : nearLimit
                  ? '#fbbf24'
                  : '#10b981',
              }}
            />
          </div>
        </div>
      )}

      <div style={{ marginTop: 12, fontSize: 11, color: '#64748b', lineHeight: 1.5 }}>
        {usage?.estimate_note || ESTIMATE_NOTE}
        {totals.has_unpriced_calls && (
          <>
            {' '}
            Some calls used a model with no configured rate, so their tokens are
            counted but their cost is not included in this total.
          </>
        )}
      </div>
    </div>
  );
}

function Metric({
  label,
  value,
  accent = false,
}: {
  label: string;
  value: string;
  accent?: boolean;
}) {
  return (
    <div>
      <div style={{ color: '#64748b', marginBottom: 2 }}>{label}</div>
      <div
        style={{
          fontWeight: accent ? 700 : 500,
          color: accent ? '#fbbf24' : '#e2e8f0',
          fontSize: accent ? 15 : 13,
        }}
      >
        {value}
      </div>
    </div>
  );
}
