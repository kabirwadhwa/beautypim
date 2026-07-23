"use client";
import { API_URL, BACKEND_URL } from '../../config';

import React, { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import Shell from '../../components/Shell';
import { Search, Filter, AlertTriangle, ArrowRight, X } from 'lucide-react';
import styles from '../page.module.css';

interface Product {
  id: string;
  internal_code: string;
  product_name: string;
  brand_name: string;
  category_path: string | null;
  gtin: string | null;
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
  const [loading, setLoading] = useState(true);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [actionLoading, setActionLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchProducts = async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const token = localStorage.getItem("token");
      const headers = { "Authorization": `Bearer ${token}` };

      let url = `${API_URL}/products?limit=100`;
      if (debouncedSearch) url += `&search=${encodeURIComponent(debouncedSearch)}`;
      if (statusFilter) url += `&status_filter=${encodeURIComponent(statusFilter)}`;
      if (issueFilter !== null) url += `&issue_filter=${issueFilter}`;

      const resp = await fetch(url, { headers, signal });
      if (!resp.ok) {
        const body = await resp.json().catch(() => null);
        throw new Error(body?.detail || "Unable to load products.");
      }
      const data = await resp.json();
      setProducts(data || []);
      setSelectedIds(previous => previous.filter(id => data.some((product: Product) => product.id === id)));
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

  const handleSelectAll = (checked: boolean) => {
    if (checked) {
      setSelectedIds(products.map(p => p.id));
    } else {
      setSelectedIds([]);
    }
  };

  const handleBulkAction = async (action: 'approve' | 'reject') => {
    if (selectedIds.length === 0) return;
    setActionLoading(true);
    try {
      const token = localStorage.getItem("token");
      const resp = await fetch(`${API_URL}/products/bulk-action`, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ product_ids: selectedIds, action })
      });
      const data = await resp.json().catch(() => null);
      if (!resp.ok) throw new Error(data?.detail || `Bulk ${action} failed.`);
      if (data.failed_count) throw new Error(`${data.success_count} updated; ${data.failed_count} failed.`);
      setSelectedIds([]);
      await fetchProducts();
    } catch (e: any) {
      setError(e?.message || `Bulk ${action} failed.`);
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
          <div style={{ display: 'flex', gap: 12 }}>
            <button 
              onClick={() => handleBulkAction('approve')} 
              className={`${styles.btn} ${styles.btnPrimary}`}
              disabled={actionLoading}
            >
              Bulk Approve ({selectedIds.length})
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
          {(search || statusFilter || issueFilter !== null) && (
            <button
              type="button"
              className={`${styles.btn} ${styles.btnSecondary}`}
              onClick={() => {
                setSearch('');
                setStatusFilter('');
                setIssueFilter(null);
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
                    checked={products.length > 0 && selectedIds.length === products.length}
                    onChange={(e) => handleSelectAll(e.target.checked)}
                  />
                </th>
                <th>ICN</th>
                <th>Brand Name</th>
                <th>Product Name</th>
                <th>GTIN / EAN</th>
                <th>Taxonomy Category</th>
                <th>Issues</th>
                <th>Review State</th>
                <th style={{ width: 80 }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {products.length === 0 ? (
                <tr>
                  <td colSpan={9} style={{ textAlign: 'center', color: '#64748b', padding: 24 }}>
                    No products found matching active filter parameters.
                  </td>
                </tr>
              ) : (
                products.map((p) => (
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
                    <td style={{ color: '#94a3b8' }}>{p.category_path || "-"}</td>
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
