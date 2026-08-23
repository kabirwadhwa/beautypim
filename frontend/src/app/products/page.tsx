"use client";
import { API_URL, BACKEND_URL } from '../../config';

import React, { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import Shell from '../../components/Shell';
import { Search, Filter, AlertTriangle, ArrowRight, X, Sparkles, Tag } from 'lucide-react';
import styles from '../page.module.css';

interface Product {
  id: string;
  internal_code: string;
  product_name: string;
  brand_name: string;
  category_path: string | null;
  product_category: string | null;
  subcategory: string | null;
  product_type: string | null;
  gtin: string | null;
  variant_count: number;
  review_status: string;
  validation_issue_count: number;
  highest_issue_severity: string | null;
  tags: string[];
}

interface BulkProgress {
  action: 'improve' | 're_enrich';
  stage: string;
  total: number;
  completed: number;
  successful: number;
  failed: number;
  skipped: number;
  percent: number;
  running: boolean;
  outcomes?: Record<string, number>;
  items?: Array<{
    product_id: string | null;
    product_name?: string | null;
    business_outcome?: string | null;
    business_status?: string | null;
    before_completeness?: number | null;
    after_completeness?: number | null;
    fields_added?: string[];
    fields_still_missing?: string[];
    sources_ingested?: number;
    error?: string | null;
    terminal?: boolean;
    waiting_for_rate_limit?: boolean;
  }>;
}

export default function ProductsPage() {
  const router = useRouter();
  const [products, setProducts] = useState<Product[]>([]);
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [issueFilter, setIssueFilter] = useState<boolean | null>(null);
  const [categoryFilter, setCategoryFilter] = useState('');
  const [productTypeFilter, setProductTypeFilter] = useState('');
  const [tagFilter, setTagFilter] = useState('');
  const [loading, setLoading] = useState(true);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [actionLoading, setActionLoading] = useState(false);
  const [bulkCategory, setBulkCategory] = useState('');
  const [bulkSubcategory, setBulkSubcategory] = useState('');
  const [bulkTag, setBulkTag] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [bulkMessage, setBulkMessage] = useState<string | null>(null);
  const [bulkProgress, setBulkProgress] = useState<BulkProgress | null>(null);

  const waitForResearchJobs = async (
    jobIds: string[], total: number, alreadyCompleted: number,
    alreadySuccessful: number, alreadyFailed: number, alreadySkipped: number,
    action: 'improve' | 're_enrich'
  ) => {
    if (!jobIds.length) {
      setBulkProgress({ action, stage: action === 'improve' ? 'Bulk improvement complete' : 'Re-enrichment complete',
        total, completed: total, successful: alreadySuccessful, failed: alreadyFailed,
        skipped: alreadySkipped, percent: 100, running: false });
      return { successful: alreadySuccessful, failed: alreadyFailed };
    }
    const token = localStorage.getItem("token");
    for (;;) {
      const resp = await fetch(`${API_URL}/products/bulk/actions/improve/status`, {
        method: 'POST',
        headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ research_job_ids: jobIds })
      });
      const data = await resp.json().catch(() => null);
      if (!resp.ok) throw new Error(data?.detail || 'Unable to read bulk progress.');
      const completed = alreadyCompleted + (data.completed_count || 0);
      const successful = alreadySuccessful + (data.successful_count || 0);
      const failed = alreadyFailed + (data.failed_count || 0);
      setBulkProgress({
        action, stage: data.all_terminal
          ? (failed ? 'Bulk processing complete · some products need attention' : (action === 'improve' ? 'Bulk improvement complete' : 'Re-enrichment complete'))
          : 'Researching and applying missing evidence', total, completed,
        successful, failed, skipped: alreadySkipped,
        percent: Math.min(100, Math.round((completed / total) * 100)), running: !data.all_terminal,
        outcomes: data.outcome_counts || {}, items: data.items || [],
      });
      if (data.all_terminal) return { successful, failed, items: data.items || [] };
      await new Promise(resolve => window.setTimeout(resolve, 2000));
    }
  };

  const fetchProducts = async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const token = localStorage.getItem("token");
      const headers = { "Authorization": `Bearer ${token}` };

      const pageSize = 100;
      const allProducts: Product[] = [];
      // The backend intentionally caps each response. Fetch every page so the
      // grid, filters, select-all and bulk actions operate on the full customer
      // catalogue rather than silently stopping at the first 100 products.
      for (let page = 1; page <= 1000; page += 1) {
        const params = new URLSearchParams({ limit: String(pageSize), page: String(page) });
        if (debouncedSearch) params.set('search', debouncedSearch);
        if (statusFilter) params.set('status_filter', statusFilter);
        if (issueFilter !== null) params.set('issue_filter', String(issueFilter));
        const resp = await fetch(`${API_URL}/products?${params.toString()}`, { headers, signal });
        if (!resp.ok) {
          const body = await resp.json().catch(() => null);
          throw new Error(body?.detail || "Unable to load products.");
        }
        const pageProducts: Product[] = await resp.json();
        allProducts.push(...pageProducts);
        if (pageProducts.length < pageSize) break;
      }
      setProducts(allProducts);
      setSelectedIds(previous => previous.filter(id => allProducts.some(product => product.id === id)));
    } catch (e: any) {
      if (e?.name !== "AbortError") setError(e?.message || "Unable to load products.");
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  };

  useEffect(() => {
    const timeout = window.setTimeout(() => setDebouncedSearch(search.trim()), 300);
    return () => window.clearTimeout(timeout);
  }, [search]);

  useEffect(() => {
    const controller = new AbortController();
    fetchProducts(controller.signal);
    return () => controller.abort();
  }, [debouncedSearch, statusFilter, issueFilter]);

  const handleSelectRow = (id: string) => {
    if (selectedIds.includes(id)) {
      setSelectedIds(selectedIds.filter(x => x !== id));
    } else {
      setSelectedIds([...selectedIds, id]);
    }
  };

  const categoryOptions = Array.from(new Set(products.map(p => p.product_category).filter(Boolean) as string[])).sort();
  const productTypeOptions = Array.from(new Set(products.map(p => p.product_type).filter(Boolean) as string[])).sort();
  const tagOptions = Array.from(new Set(products.flatMap(p => p.tags || []))).sort((a, b) => a.localeCompare(b));
  const visibleProducts = products.filter(product =>
    (!categoryFilter || product.product_category === categoryFilter) &&
    (!productTypeFilter || product.product_type === productTypeFilter) &&
    (!tagFilter || (product.tags || []).includes(tagFilter))
  );
  const visibleVariantCount = visibleProducts.reduce((total, product) => total + (product.variant_count || 0), 0);

  const handleSelectAll = (checked: boolean) => {
    if (checked) {
      setSelectedIds(visibleProducts.map(p => p.id));
    } else {
      setSelectedIds([]);
    }
  };

  const handleBulkAction = async (action: 'approve' | 'reject' | 're_enrich' | 'set_classification' | 'add_tags' | 'remove_tags') => {
    if (selectedIds.length === 0) return;
    setActionLoading(true);
    setError(null);
    setBulkMessage(null);
    if (action === 're_enrich') {
      setBulkProgress({ action, stage: 'Re-enriching selected products', total: selectedIds.length,
        completed: 0, successful: 0, failed: 0, skipped: 0, percent: 0, running: true });
    } else {
      setBulkProgress(null);
    }
    try {
      const token = localStorage.getItem("token");
      const chunkSize = action === 're_enrich' ? 2 : 50;
      let successful = 0;
      const failures: string[] = [];
      const researchJobIds: string[] = [];
      for (let offset = 0; offset < selectedIds.length; offset += chunkSize) {
        const resp = await fetch(`${API_URL}/products/bulk/actions`, {
          method: "POST",
          headers: {
            "Authorization": `Bearer ${token}`,
            "Content-Type": "application/json"
          },
          body: JSON.stringify({
            product_ids: selectedIds.slice(offset, offset + chunkSize), action,
            category: action === 'set_classification' ? bulkCategory : undefined,
            subcategory: action === 'set_classification' ? bulkSubcategory : undefined,
            tags: action === 'add_tags' || action === 'remove_tags' ? [bulkTag] : undefined
          })
        });
        const data = await resp.json().catch(() => null);
        if (!resp.ok) throw new Error(data?.detail || `Bulk ${action} failed.`);
        successful += data.success_count || 0;
        if (data.failed_count) failures.push(...(data.errors || []).map((item: any) => item.error));
        if (action === 're_enrich') {
          researchJobIds.push(...(data.items || []).map((item: any) => item.research_job_id).filter(Boolean));
          const completed = Math.min(selectedIds.length, offset + chunkSize);
          setBulkProgress({ action, stage: 'Re-enriching selected products', total: selectedIds.length,
            completed, successful, failed: failures.length, skipped: 0,
            percent: Math.round((completed / selectedIds.length) * 100), running: true });
        }
      }
      if (failures.length) throw new Error(`${successful} updated; ${failures.length} failed. ${failures[0]}`);
      if (action === 're_enrich' && researchJobIds.length) {
        const directlyComplete = selectedIds.length - researchJobIds.length;
        const result = await waitForResearchJobs(
          researchJobIds, selectedIds.length, directlyComplete,
          directlyComplete, 0, 0, 're_enrich'
        );
        successful = result.successful;
        if (result.failed) throw new Error(`${result.successful} completed; ${result.failed} failed during evidence research.`);
      } else if (action === 're_enrich') {
        setBulkProgress({ action, stage: 'Re-enrichment complete', total: selectedIds.length,
          completed: selectedIds.length, successful, failed: 0, skipped: 0, percent: 100, running: false });
      }
      setSelectedIds([]);
      if (action === 'add_tags' || action === 'remove_tags') setBulkTag('');
      setBulkMessage(`${successful} product${successful === 1 ? '' : 's'} updated.`);
      await fetchProducts();
    } catch (e: any) {
      setBulkProgress(previous => previous ? { ...previous, running: false, stage: 'Bulk action finished with errors' } : previous);
      setError(e?.message || `Bulk ${action} failed.`);
    } finally {
      setActionLoading(false);
    }
  };

  const handleBulkImprove = async (requestedIds: string[] = selectedIds) => {
    if (requestedIds.length === 0) return;
    setActionLoading(true);
    setError(null);
    setBulkMessage(`Queueing ${requestedIds.length} selected products…`);
    setBulkProgress({ action: 'improve', stage: 'Queueing selected products', total: requestedIds.length,
      completed: 0, successful: 0, failed: 0, skipped: 0, percent: 0, running: true });
    try {
      const token = localStorage.getItem("token");
      let queued = 0;
      let skipped = 0;
      let failed = 0;
      let firstFailure = '';
      const researchJobIds: string[] = [];
      for (let offset = 0; offset < requestedIds.length; offset += 100) {
        const resp = await fetch(`${API_URL}/products/bulk/actions/improve`, {
          method: "POST",
          headers: {
            "Authorization": `Bearer ${token}`,
            "Content-Type": "application/json"
          },
          body: JSON.stringify({ product_ids: requestedIds.slice(offset, offset + 100), mode: "missing_only" })
        });
        const data = await resp.json().catch(() => null);
        if (!resp.ok) throw new Error(data?.detail || "Bulk Improve failed.");
        queued += data.queued_count || 0;
        skipped += data.skipped_count || 0;
        failed += data.failed_count || 0;
        if (!firstFailure && data.failed_count) {
          firstFailure = (data.items || []).find((item: any) => item.status === 'failed')?.error || '';
        }
        researchJobIds.push(...(data.items || []).map((item: any) => item.research_job_id).filter(Boolean));
        const submitted = Math.min(requestedIds.length, offset + 100);
        setBulkProgress({ action: 'improve', stage: 'Queueing selected products', total: requestedIds.length,
          completed: skipped + failed, successful: 0, failed, skipped,
          percent: Math.min(10, Math.round((submitted / requestedIds.length) * 10)), running: true });
      }
      const result = await waitForResearchJobs(
        researchJobIds, requestedIds.length, skipped + failed, 0, failed, skipped, 'improve'
      );
      const message = `${result.successful} product${result.successful === 1 ? '' : 's'} improved` +
        `${skipped ? ` · ${skipped} already complete` : ''}` +
        `${result.failed ? ` · ${result.failed} need attention` : ''}.`;
      setBulkMessage(message);
      if (result.failed) {
        setError(`${message} Review the per-product outcomes below. ${firstFailure}`.trim());
      }
      setSelectedIds([]);
      await fetchProducts();
    } catch (e: any) {
      setBulkMessage(null);
      setBulkProgress(previous => previous ? { ...previous, running: false, stage: 'Bulk improvement finished with errors' } : previous);
      setError(e?.message || "Bulk Improve failed.");
    } finally {
      setActionLoading(false);
    }
  };

  const getStatusClass = (status: string) => {
    switch (status.toLowerCase()) {
      case 'approved': return styles.badgeSuccess;
      case 'rejected': return styles.badgeDanger;
      case 'imported': return styles.badgeNeutral;
      default: return styles.badgeWarning;
    }
  };

  return (
    <Shell>
      <div className={styles.pageHeader}>
        <div className={styles.titleGroup}>
          <h1>Canonical Products Catalog</h1>
          <p>Verify matching records, review AI validations, and publish clean schemas</p>
        </div>

        {selectedIds.length > 0 && (
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
            <input className={styles.inputField} value={bulkCategory} onChange={e => setBulkCategory(e.target.value)} placeholder="Category" style={{ width: 145 }} />
            <input className={styles.inputField} value={bulkSubcategory} onChange={e => setBulkSubcategory(e.target.value)} placeholder="Subcategory" style={{ width: 155 }} />
            <button
              onClick={() => handleBulkAction('set_classification')}
              className={`${styles.btn} ${styles.btnSecondary}`}
              disabled={actionLoading || !bulkCategory.trim() || !bulkSubcategory.trim()}
            >
              Bulk edit category ({selectedIds.length})
            </button>
            <input className={styles.inputField} value={bulkTag} onChange={e => setBulkTag(e.target.value)} placeholder="Tag" maxLength={50} style={{ width: 130 }} />
            <button
              onClick={() => handleBulkAction('add_tags')}
              className={`${styles.btn} ${styles.btnSecondary}`}
              disabled={actionLoading || !bulkTag.trim()}
            >
              <Tag size={15} /> Add tag ({selectedIds.length})
            </button>
            <button
              onClick={() => handleBulkAction('remove_tags')}
              className={`${styles.btn} ${styles.btnSecondary}`}
              disabled={actionLoading || !bulkTag.trim()}
            >
              Remove tag
            </button>
            <button 
              onClick={() => handleBulkAction('approve')} 
              className={`${styles.btn} ${styles.btnPrimary}`}
              disabled={actionLoading}
            >
              Bulk Approve ({selectedIds.length})
            </button>
            <button
              onClick={() => handleBulkImprove()}
              className={`${styles.btn} ${styles.btnPrimary}`}
              disabled={actionLoading}
              title="Research and improve missing high-value fields for every selected product"
            >
              <Sparkles size={15} /> Improve selected ({selectedIds.length})
            </button>
            <button
              onClick={() => handleBulkAction('re_enrich')}
              className={`${styles.btn} ${styles.btnSecondary}`}
              disabled={actionLoading}
              title="Regenerate enrichment from each product's latest source record"
            >
              <Sparkles size={15} /> Re-enrich ({selectedIds.length})
            </button>
            <button 
              onClick={() => handleBulkAction('reject')} 
              className={`${styles.btn} ${styles.btnSecondary}`}
              style={{ color: '#ef4444', borderColor: '#ef4444' }}
              disabled={actionLoading}
            >
              Bulk Reject ({selectedIds.length})
            </button>
          </div>
        )}
      </div>

      {/* Filter and Search controls */}
      <div className={styles.panelCard} style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'center' }}>
        <div style={{ position: 'relative', flex: 1, minWidth: '240px' }}>
          <input 
            type="text" 
            placeholder="Search by ICN, barcode, brand, or product name..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className={styles.inputField}
            style={{ paddingLeft: '36px' }}
          />
          <Search size={18} color="#64748b" style={{ position: 'absolute', left: 12, top: 12 }} />
        </div>

        <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
          <Filter size={18} color="#94a3b8" />
          <select 
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className={styles.inputField}
            style={{ backgroundColor: '#0b0f19', width: '160px' }}
          >
            <option value="">All statuses</option>
            <option value="imported">Imported</option>
            <option value="needs_review">Needs Review</option>
            <option value="in_review">In Review</option>
            <option value="enriching">Enriching</option>
            <option value="enrichment_failed">Enrichment Failed</option>
            <option value="approved">Approved</option>
            <option value="rejected">Rejected</option>
            <option value="published">Published</option>
          </select>
          <select
            value={categoryFilter}
            onChange={(e) => setCategoryFilter(e.target.value)}
            className={styles.inputField}
            style={{ backgroundColor: '#0b0f19', width: '170px' }}
          >
            <option value="">All categories</option>
            {categoryOptions.map(category => <option key={category} value={category}>{category}</option>)}
          </select>
          <select
            value={tagFilter}
            onChange={(e) => setTagFilter(e.target.value)}
            className={styles.inputField}
            style={{ backgroundColor: '#0b0f19', width: '150px' }}
          >
            <option value="">All tags</option>
            {tagOptions.map(tag => <option key={tag} value={tag}>{tag}</option>)}
          </select>
          <select
            value={productTypeFilter}
            onChange={(e) => setProductTypeFilter(e.target.value)}
            className={styles.inputField}
            style={{ backgroundColor: '#0b0f19', width: '170px' }}
          >
            <option value="">All product types</option>
            {productTypeOptions.map(type => <option key={type} value={type}>{type}</option>)}
          </select>

          <select 
            value={issueFilter === null ? '' : String(issueFilter)}
            onChange={(e) => {
              const val = e.target.value;
              setIssueFilter(val === '' ? null : val === 'true');
            }}
            className={styles.inputField}
            style={{ backgroundColor: '#0b0f19', width: '180px' }}
          >
            <option value="">All issue states</option>
            <option value="true">Has validation issues</option>
            <option value="false">Clear of issues</option>
          </select>
          {(search || statusFilter || issueFilter !== null || categoryFilter || productTypeFilter || tagFilter) && (
            <button
              type="button"
              className={`${styles.btn} ${styles.btnSecondary}`}
              onClick={() => {
                setSearch('');
                setStatusFilter('');
                setIssueFilter(null);
                setCategoryFilter('');
                setProductTypeFilter('');
                setTagFilter('');
              }}
              title="Clear all filters"
            >
              <X size={15} /> Clear
            </button>
          )}
        </div>
      </div>

      {error && (
        <div role="alert" style={{ marginBottom: 16, padding: 12, border: '1px solid #ef4444', borderRadius: 6, color: '#fecaca', background: 'rgba(239,68,68,.1)' }}>
          {error}
        </div>
      )}
      {bulkMessage && !error && (
        <div role="status" style={{ marginBottom: 16, padding: 12, border: '1px solid #10b981', borderRadius: 6, color: '#a7f3d0', background: 'rgba(16,185,129,.1)' }}>
          {bulkMessage}
        </div>
      )}
      {bulkProgress && (
        <div role="status" aria-live="polite" style={{ marginBottom: 16, padding: 16, border: '1px solid #4f46e5', borderRadius: 8, background: 'rgba(79,70,229,.1)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 16, marginBottom: 8 }}>
            <strong style={{ color: '#e0e7ff' }}>{bulkProgress.stage}</strong>
            <strong style={{ color: '#c7d2fe' }}>{bulkProgress.percent}%</strong>
          </div>
          <div aria-label={`Bulk progress ${bulkProgress.percent}%`} style={{ height: 10, borderRadius: 999, overflow: 'hidden', background: '#1e293b' }}>
            <div style={{ width: `${bulkProgress.percent}%`, height: '100%', background: bulkProgress.failed ? '#f59e0b' : '#6366f1', transition: 'width .25s ease' }} />
          </div>
          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', marginTop: 9, color: '#cbd5e1', fontSize: 13 }}>
            <span>{bulkProgress.completed} / {bulkProgress.total} finished</span>
            <span>{bulkProgress.successful} successful</span>
            {bulkProgress.skipped > 0 && <span>{bulkProgress.skipped} already complete</span>}
            {bulkProgress.failed > 0 && <span style={{ color: '#fca5a5' }}>{bulkProgress.failed} need attention</span>}
            {bulkProgress.running && <span>Safe to keep this page open while processing</span>}
          </div>
          {bulkProgress.outcomes && (
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 12 }}>
              {Object.entries(bulkProgress.outcomes).filter(([, count]) => count > 0).map(([outcome, count]) => (
                <span key={outcome} className={`${styles.badge} ${outcome === 'improved' ? styles.badgeSuccess : outcome === 'failed' ? styles.badgeDanger : styles.badgeWarning}`}>
                  {outcome.replaceAll('_', ' ')}: {count}
                </span>
              ))}
            </div>
          )}
          {!!bulkProgress.items?.length && (
            <div style={{ marginTop: 14, overflowX: 'auto', maxHeight: 300, overflowY: 'auto' }}>
              <table className={styles.denseTable} style={{ fontSize: 12 }}>
                <thead><tr><th>Product</th><th>Outcome</th><th>Before → After</th><th>Sources</th><th>Fields added</th><th>Remaining gaps / reason</th></tr></thead>
                <tbody>{bulkProgress.items.map((item, index) => (
                  <tr key={`${item.product_id || 'missing'}-${index}`}>
                    <td>{item.product_name || item.product_id || 'Unknown product'}</td>
                    <td>{(item.business_outcome || (item.terminal ? 'failed' : item.waiting_for_rate_limit ? 'waiting for rate limit' : 'processing')).replaceAll('_', ' ')}</td>
                    <td>{item.before_completeness ?? '—'}% → {item.after_completeness ?? '—'}%</td>
                    <td>{item.sources_ingested ?? 0}</td>
                    <td>{item.fields_added?.length ? item.fields_added.join(', ') : '—'}</td>
                    <td>{item.error || item.fields_still_missing?.slice(0, 4).join(', ') || '—'}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
          {!bulkProgress.running && bulkProgress.items?.some(item =>
            item.product_id && ['failed', 'no_material_improvement', 'needs_identity_resolution', 'rate_limited_retriable', 'blocked_sources', 'partially_improved'].includes(item.business_outcome || '')
          ) && (
            <button
              type="button"
              className={`${styles.btn} ${styles.btnSecondary}`}
              style={{ marginTop: 12 }}
              disabled={actionLoading}
              onClick={() => handleBulkImprove((bulkProgress.items || []).filter(item =>
                item.product_id && ['failed', 'no_material_improvement', 'needs_identity_resolution', 'rate_limited_retriable', 'blocked_sources', 'partially_improved'].includes(item.business_outcome || '')
              ).map(item => item.product_id as string))}
            >
              Retry failed / incomplete
            </button>
          )}
        </div>
      )}

      {!loading && (
        <div role="status" style={{ marginBottom: 12, color: '#94a3b8', fontSize: 13 }}>
          Showing <strong style={{ color: '#f8fafc' }}>{visibleProducts.length}</strong> product families
          {' · '}
          <strong style={{ color: '#f8fafc' }}>{visibleVariantCount}</strong> variants
          {(categoryFilter || productTypeFilter) && visibleProducts.length !== products.length
            ? ` (filtered from ${products.length} loaded families)`
            : ' loaded'}
        </div>
      )}

      <div className={styles.tableContainer}>
        {loading ? (
          <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', padding: 48, color: '#64748b' }}>
            <span>Retrieving Product Grid Records...</span>
          </div>
        ) : (
          <table className={styles.denseTable}>
            <thead>
              <tr>
                <th style={{ width: 40 }}>
                  <input 
                    type="checkbox" 
                    checked={visibleProducts.length > 0 && visibleProducts.every(product => selectedIds.includes(product.id))}
                    onChange={(e) => handleSelectAll(e.target.checked)}
                  />
                </th>
                <th>ICN</th>
                <th>Brand Name</th>
                <th>Product Name</th>
                <th>GTIN / EAN</th>
                <th>Variants</th>
                <th>Category</th>
                <th>Subcategory</th>
                <th>Product Type</th>
                <th>Tags</th>
                <th>Issues</th>
                <th>Review State</th>
                <th style={{ width: 80 }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {visibleProducts.length === 0 ? (
                <tr>
                  <td colSpan={13} style={{ textAlign: 'center', color: '#64748b', padding: 24 }}>
                    No products found matching active filter parameters.
                  </td>
                </tr>
              ) : (
                visibleProducts.map((p) => (
                  <tr key={p.id} style={{ cursor: 'pointer' }} onClick={() => router.push(`/products/${p.id}`)}>
                    <td onClick={(e) => e.stopPropagation()}>
                      <input 
                        type="checkbox" 
                        checked={selectedIds.includes(p.id)}
                        onChange={() => handleSelectRow(p.id)}
                      />
                    </td>
                    <td style={{ fontFamily: 'monospace', color: '#a5b4fc' }} title={p.internal_code}>
                      {p.internal_code.slice(0, 12)}…
                    </td>
                    <td style={{ fontWeight: 600 }}>{p.brand_name}</td>
                    <td>{p.product_name}</td>
                    <td style={{ fontFamily: 'monospace', color: '#94a3b8' }}>{p.gtin || "—"}</td>
                    <td>{p.variant_count || 0}</td>
                    <td>{p.product_category || "—"}</td>
                    <td style={{ color: '#c4b5fd' }}>{p.subcategory || "—"}</td>
                    <td style={{ color: '#94a3b8' }}>{p.product_type || "—"}</td>
                    <td>
                      <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', minWidth: 110 }}>
                        {(p.tags || []).length ? p.tags.map(tag => (
                          <span key={tag} className={`${styles.badge} ${styles.badgeNeutral}`}>{tag}</span>
                        )) : <span style={{ color: '#64748b' }}>—</span>}
                      </div>
                    </td>
                    <td>
                      {p.validation_issue_count > 0 ? (
                        <span className={`${styles.badge} ${p.highest_issue_severity === 'blocking' ? styles.badgeDanger : styles.badgeWarning}`}>
                          <AlertTriangle size={11} /> {p.validation_issue_count}
                        </span>
                      ) : (
                        <span className={`${styles.badge} ${styles.badgeSuccess}`}>Clear</span>
                      )}
                    </td>
                    <td>
                      <span className={`${styles.badge} ${getStatusClass(p.review_status)}`}>
                        {p.review_status}
                      </span>
                    </td>
                    <td onClick={(e) => e.stopPropagation()}>
                      <button 
                        onClick={() => router.push(`/products/${p.id}`)}
                        className={`${styles.btn} ${styles.btnSecondary}`}
                        style={{ padding: '4px 8px', fontSize: 11 }}
                      >
                        Inspect <ArrowRight size={12} style={{ marginLeft: 4 }} />
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        )}
      </div>
    </Shell>
  );
}
