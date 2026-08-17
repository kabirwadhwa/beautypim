"use client";
import { API_URL, BACKEND_URL } from '../../config';

import React, { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import Shell from '../../components/Shell';
import { Search, Filter, AlertTriangle, ArrowRight, X, Sparkles } from 'lucide-react';
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
  const [loading, setLoading] = useState(true);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [actionLoading, setActionLoading] = useState(false);
  const [bulkCategory, setBulkCategory] = useState('');
  const [bulkSubcategory, setBulkSubcategory] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [bulkMessage, setBulkMessage] = useState<string | null>(null);

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
  const visibleProducts = products.filter(product =>
    (!categoryFilter || product.product_category === categoryFilter) &&
    (!productTypeFilter || product.product_type === productTypeFilter)
  );
  const visibleVariantCount = visibleProducts.reduce((total, product) => total + (product.variant_count || 0), 0);

  const handleSelectAll = (checked: boolean) => {
    if (checked) {
      setSelectedIds(visibleProducts.map(p => p.id));
    } else {
      setSelectedIds([]);
    }
  };

  const handleBulkAction = async (action: 'approve' | 'reject' | 're_enrich' | 'set_classification') => {
    if (selectedIds.length === 0) return;
    setActionLoading(true);
    try {
      const token = localStorage.getItem("token");
      const chunkSize = action === 're_enrich' ? 2 : selectedIds.length;
      let successful = 0;
      const failures: string[] = [];
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
            subcategory: action === 'set_classification' ? bulkSubcategory : undefined
          })
        });
        const data = await resp.json().catch(() => null);
        if (!resp.ok) throw new Error(data?.detail || `Bulk ${action} failed.`);
        successful += data.success_count || 0;
        if (data.failed_count) failures.push(...(data.errors || []).map((item: any) => item.error));
      }
      if (failures.length) throw new Error(`${successful} updated; ${failures.length} failed. ${failures[0]}`);
      setSelectedIds([]);
      await fetchProducts();
    } catch (e: any) {
      setError(e?.message || `Bulk ${action} failed.`);
    } finally {
      setActionLoading(false);
    }
  };

  const handleBulkImprove = async () => {
    if (selectedIds.length === 0) return;
    setActionLoading(true);
    setError(null);
    setBulkMessage(`Queueing ${selectedIds.length} selected products…`);
    try {
      const token = localStorage.getItem("token");
      const resp = await fetch(`${API_URL}/products/bulk/actions/improve`, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ product_ids: selectedIds, mode: "missing_only" })
      });
      const data = await resp.json().catch(() => null);
      if (!resp.ok) throw new Error(data?.detail || "Bulk Improve failed.");
      const message = `${data.queued_count || 0} queued for background improvement` +
        `${data.skipped_count ? ` · ${data.skipped_count} already complete` : ''}` +
        `${data.failed_count ? ` · ${data.failed_count} failed` : ''}.`;
      setBulkMessage(message);
      if (data.failed_count) {
        const firstFailure = (data.items || []).find((item: any) => item.status === 'failed');
        setError(`${message} ${firstFailure?.error || ''}`.trim());
      }
      setSelectedIds([]);
      await fetchProducts();
    } catch (e: any) {
      setBulkMessage(null);
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
            <button 
              onClick={() => handleBulkAction('approve')} 
              className={`${styles.btn} ${styles.btnPrimary}`}
              disabled={actionLoading}
            >
              Bulk Approve ({selectedIds.length})
            </button>
            <button
              onClick={handleBulkImprove}
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
          {(search || statusFilter || issueFilter !== null || categoryFilter || productTypeFilter) && (
            <button
              type="button"
              className={`${styles.btn} ${styles.btnSecondary}`}
              onClick={() => {
                setSearch('');
                setStatusFilter('');
                setIssueFilter(null);
                setCategoryFilter('');
                setProductTypeFilter('');
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
          {bulkMessage} You can continue working while the queue runs.
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
                <th>Issues</th>
                <th>Review State</th>
                <th style={{ width: 80 }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {visibleProducts.length === 0 ? (
                <tr>
                  <td colSpan={12} style={{ textAlign: 'center', color: '#64748b', padding: 24 }}>
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
