"use client";

import React, { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import Shell from '../../components/Shell';
import { Search, Filter, AlertTriangle, ArrowRight, ShieldCheck } from 'lucide-react';
import styles from '../page.module.css';

interface Product {
  id: string;
  product_name: string;
  brand_name: string;
  category_path: string | null;
  review_status: string;
}

export default function ProductsPage() {
  const router = useRouter();
  const [products, setProducts] = useState<Product[]>([]);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [issueFilter, setIssueFilter] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [actionLoading, setActionLoading] = useState(false);

  const fetchProducts = async () => {
    setLoading(true);
    try {
      const token = localStorage.getItem("token");
      const headers = { "Authorization": f"Bearer {token}" };

      let url = `http://localhost:8000/api/products?limit=100`;
      if (search) url += `&search=${encodeURIComponent(search)}`;
      if (statusFilter) url += `&status_filter=${statusFilter}`;
      if (issueFilter !== null) url += `&issue_filter=${issueFilter}`;

      const resp = await fetch(url, { headers });
      if (resp.ok) {
        const data = await resp.json();
        setProducts(data || []);
      }
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchProducts();
  }, [search, statusFilter, issueFilter]);

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
      const resp = await fetch("http://localhost:8000/api/products/bulk-action", {
        method: "POST",
        headers: {
          "Authorization": f"Bearer {token}",
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ product_ids: selectedIds, action })
      });
      if (resp.ok) {
        setSelectedIds([]);
        fetchProducts();
      }
    } catch (e) {
      console.error(e);
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
            placeholder="Search products by brand or title..." 
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
            <option value="approved">Approved</option>
            <option value="rejected">Rejected</option>
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
        </div>
      </div>

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
                <th>Brand Name</th>
                <th>Product Name</th>
                <th>Taxonomy Category</th>
                <th>Review State</th>
                <th style={{ width: 80 }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {products.length === 0 ? (
                <tr>
                  <td colSpan={6} style={{ textAlign: 'center', color: '#64748b', padding: 24 }}>
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
                    <td style={{ fontWeight: 600 }}>{p.brand_name}</td>
                    <td>{p.product_name}</td>
                    <td style={{ color: '#94a3b8' }}>{p.category_path || "-"}</td>
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
