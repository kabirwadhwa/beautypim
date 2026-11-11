"use client";

import React, { useCallback, useEffect, useState } from 'react';
import { API_URL } from '../../config';
import Shell from '../../components/Shell';
import UsagePanel from '../../components/UsagePanel';
import { Coins, RefreshCw } from 'lucide-react';
import styles from '../page.module.css';
import {
  ESTIMATE_NOTE,
  UsagePayload,
  formatTokens,
  formatUsd,
  operationLabel,
} from '../../usage';

type PricingEntry = {
  model: string;
  pricing_source: string;
  pricing_source_label: string;
  input_per_million_usd: number | null;
  output_per_million_usd: number | null;
  needs_confirmation: boolean;
};

type ModelRow = {
  model: string;
  pricing_source: string;
  total_tokens: number;
  estimated_cost_usd: number;
  call_count: number;
};

type SummaryPayload = UsagePayload & {
  by_model: ModelRow[];
  pricing: PricingEntry[];
  window_days: number | null;
};

const WINDOWS: Array<{ label: string; days: number | null }> = [
  { label: 'Last 7 days', days: 7 },
  { label: 'Last 30 days', days: 30 },
  { label: 'Last 90 days', days: 90 },
  { label: 'All time', days: null },
];

export default function UsagePage() {
  const [summary, setSummary] = useState<SummaryPayload | null>(null);
  const [days, setDays] = useState<number | null>(30);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchSummary = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const token = localStorage.getItem('token');
      const query = days === null ? '' : `?days=${days}`;
      const resp = await fetch(`${API_URL}/ai-usage/summary${query}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => null);
        throw new Error(body?.detail || 'Could not load AI usage.');
      }
      setSummary(await resp.json());
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not load AI usage.');
    } finally {
      setLoading(false);
    }
  }, [days]);

  useEffect(() => {
    fetchSummary();
  }, [fetchSummary]);

  const totals = summary?.totals;
  const hasSpend = !!totals && totals.call_count > 0;

  return (
    <Shell>
      <div className={styles.pageHeader}>
        <div className={styles.titleGroup}>
          <h1>AI Usage &amp; Cost</h1>
          <p>
            What enrichment, web research, review synthesis and the catalogue
            assistant have consumed, and what it is estimated to have cost.
          </p>
        </div>
        <button
          type="button"
          onClick={fetchSummary}
          disabled={loading}
          className={`${styles.btn} ${styles.btnSecondary}`}
        >
          <RefreshCw size={14} style={{ marginRight: 6 }} />
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 18 }}>
        {WINDOWS.map((window) => (
          <button
            key={window.label}
            type="button"
            onClick={() => setDays(window.days)}
            className={`${styles.btn} ${
              days === window.days ? styles.btnPrimary : styles.btnSecondary
            }`}
            style={{ fontSize: 12 }}
          >
            {window.label}
          </button>
        ))}
      </div>

      {error && (
        <div
          className={styles.panelCard}
          style={{
            border: '1px solid #ef4444',
            background: 'rgba(239,68,68,.08)',
            color: '#fca5a5',
            marginBottom: 20,
          }}
        >
          {error}
        </div>
      )}

      {loading && !summary && (
        <div className={styles.panelCard} style={{ color: '#64748b' }}>
          Loading AI usage…
        </div>
      )}

      {summary && !hasSpend && (
        <div className={styles.panelCard} style={{ marginBottom: 20 }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              color: '#e2e8f0',
              fontWeight: 600,
              marginBottom: 8,
            }}
          >
            <Coins size={16} color="#fbbf24" />
            <span>No AI spend recorded in this period</span>
          </div>
          <p style={{ color: '#94a3b8', fontSize: 13, lineHeight: 1.6, margin: 0 }}>
            Nothing has been enriched or researched in the selected window. Try a
            wider range, or run an enrichment or Improve Product job and the cost
            will appear here.
          </p>
        </div>
      )}

      {summary && hasSpend && (
        <>
          <div className={styles.panelCard} style={{ marginBottom: 20 }}>
            <UsagePanel usage={summary} title="Total across all AI work" />
          </div>

          <div className={styles.panelCard} style={{ marginBottom: 20 }}>
            <div className={styles.panelTitle}>
              <span>Spend by model</span>
            </div>
            <div className={styles.tableContainer}>
              <table className={styles.denseTable}>
                <thead>
                  <tr>
                    <th>Model</th>
                    <th>Calls</th>
                    <th>Tokens</th>
                    <th>Estimated cost</th>
                    <th>Rate basis</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.by_model.map((row) => (
                    <tr key={`${row.model}-${row.pricing_source}`}>
                      <td style={{ fontFamily: 'monospace' }}>{row.model}</td>
                      <td>{row.call_count.toLocaleString()}</td>
                      <td>{formatTokens(row.total_tokens)}</td>
                      <td style={{ color: '#fbbf24', fontWeight: 600 }}>
                        {row.pricing_source === 'unknown'
                          ? 'not estimated'
                          : formatUsd(row.estimated_cost_usd)}
                      </td>
                      <td style={{ color: '#94a3b8' }}>{row.pricing_source}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className={styles.panelCard} style={{ marginBottom: 20 }}>
            <div className={styles.panelTitle}>
              <span>Where the money went</span>
            </div>
            <div className={styles.tableContainer}>
              <table className={styles.denseTable}>
                <thead>
                  <tr>
                    <th>Operation</th>
                    <th>Calls</th>
                    <th>Tokens</th>
                    <th>Estimated cost</th>
                    <th>Share of spend</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.by_operation.map((row) => {
                    const share =
                      totals.estimated_cost_usd > 0
                        ? (row.estimated_cost_usd / totals.estimated_cost_usd) * 100
                        : 0;
                    return (
                      <tr key={row.operation}>
                        <td>{operationLabel(row.operation)}</td>
                        <td>{row.call_count.toLocaleString()}</td>
                        <td>{formatTokens(row.total_tokens)}</td>
                        <td>{formatUsd(row.estimated_cost_usd)}</td>
                        <td>
                          <div
                            style={{
                              display: 'flex',
                              alignItems: 'center',
                              gap: 8,
                            }}
                          >
                            <div
                              style={{
                                flex: 1,
                                height: 4,
                                borderRadius: 2,
                                background: '#1e293b',
                                minWidth: 60,
                              }}
                            >
                              <div
                                style={{
                                  width: `${Math.min(100, share)}%`,
                                  height: '100%',
                                  background: '#6366f1',
                                }}
                              />
                            </div>
                            <span style={{ color: '#94a3b8', fontSize: 11 }}>
                              {share.toFixed(0)}%
                            </span>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

      {summary?.pricing && summary.pricing.length > 0 && (
        <div className={styles.panelCard}>
          <div className={styles.panelTitle}>
            <span>How these costs are estimated</span>
          </div>
          <div className={styles.tableContainer}>
            <table className={styles.denseTable}>
              <thead>
                <tr>
                  <th>Configured model</th>
                  <th>Input $/1M</th>
                  <th>Output $/1M</th>
                  <th>Rate basis</th>
                </tr>
              </thead>
              <tbody>
                {summary.pricing.map((entry) => (
                  <tr key={entry.model}>
                    <td style={{ fontFamily: 'monospace' }}>{entry.model}</td>
                    <td>
                      {entry.input_per_million_usd == null
                        ? '—'
                        : `$${entry.input_per_million_usd}`}
                    </td>
                    <td>
                      {entry.output_per_million_usd == null
                        ? '—'
                        : `$${entry.output_per_million_usd}`}
                    </td>
                    <td>
                      <span style={{ color: '#94a3b8' }}>
                        {entry.pricing_source_label}
                      </span>
                      {entry.needs_confirmation && (
                        <span
                          className={`${styles.badge} ${styles.badgeWarning}`}
                          style={{ marginLeft: 8 }}
                          title="This rate was never configured anywhere in the codebase. Confirm it against a provider invoice and set AI_MODEL_PRICING_JSON."
                        >
                          confirm rate
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p
            style={{
              marginTop: 12,
              marginBottom: 0,
              fontSize: 11,
              color: '#64748b',
              lineHeight: 1.6,
            }}
          >
            {ESTIMATE_NOTE} A rate marked{' '}
            <strong style={{ color: '#fbbf24' }}>confirm rate</strong> is a best
            estimate for a model whose price was never configured — set{' '}
            <code style={{ color: '#94a3b8' }}>AI_MODEL_PRICING_JSON</code> to
            correct it without a redeploy.
          </p>
        </div>
      )}
    </Shell>
  );
}
