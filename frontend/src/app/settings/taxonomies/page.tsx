"use client";
import { API_URL, BACKEND_URL } from '../../../config';

import React, { useState, useEffect } from 'react';
import Shell from '../../../components/Shell';
import { Plus, FolderTree, Pencil, Trash2, Save, X, Search } from 'lucide-react';
import styles from '../../page.module.css';

interface Category {
  id: string;
  name: string;
  level: number;
  path: string;
  product_count: number;
}

export default function TaxonomiesPage() {
  const [categories, setCategories] = useState<Category[]>([]);
  const [newCatName, setNewCatName] = useState('');
  const [newCatParent, setNewCatParent] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState('');

  const fetchCategories = async () => {
    setLoading(true);
    setError(null);
    try {
      const token = localStorage.getItem("token");
      const resp = await fetch(`${API_URL}/settings/categories`, {
        headers: { "Authorization": `Bearer ${token}` }
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => null);
        throw new Error(body?.detail || "Failed to load taxonomy.");
      }
      const data = await resp.json();
      setCategories(data || []);
    } catch (e: any) {
      setError(e?.message || "Failed to load taxonomy.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchCategories();
  }, []);

  const handleCreateCategory = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSuccess(null);
    if (!newCatName.trim()) return;
    setSaving(true);

    try {
      const token = localStorage.getItem("token");
      const resp = await fetch(`${API_URL}/settings/categories`, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          name: newCatName,
          parent_id: newCatParent || null
        })
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => null);
        throw new Error(body?.detail || "Failed to create category node.");
      }
      setNewCatName('');
      setNewCatParent('');
      setSuccess("Category created.");
      await fetchCategories();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  const handleRename = async (categoryId: string) => {
    if (!editingName.trim()) return;
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      const token = localStorage.getItem("token");
      const resp = await fetch(`${API_URL}/settings/categories/${categoryId}`, {
        method: "PUT",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ name: editingName })
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => null);
        throw new Error(body?.detail || "Failed to rename category.");
      }
      setEditingId(null);
      setEditingName('');
      setSuccess("Category renamed.");
      await fetchCategories();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (category: Category) => {
    if (!window.confirm(`Delete taxonomy path “${category.path}”?`)) return;
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      const token = localStorage.getItem("token");
      const resp = await fetch(`${API_URL}/settings/categories/${category.id}`, {
        method: "DELETE",
        headers: { "Authorization": `Bearer ${token}` }
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => null);
        throw new Error(body?.detail || "Failed to delete category.");
      }
      setSuccess("Category deleted.");
      await fetchCategories();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  const visibleCategories = categories.filter(category =>
    category.path.toLowerCase().includes(query.trim().toLowerCase())
  );

  return (
    <Shell>
      <div className={styles.pageHeader}>
        <div className={styles.titleGroup}>
          <h1>Taxonomy Categories Tree</h1>
          <p>Configure product category taxonomy levels to enforce mapping validation compatibility rules</p>
        </div>
      </div>

      {error && (
        <div style={{ padding: 12, backgroundColor: 'rgba(239, 68, 68, 0.1)', border: '1px solid #ef4444', borderRadius: 4, color: '#ef4444', fontSize: 13, marginBottom: 20 }}>
          {error}
        </div>
      )}
      {success && (
        <div role="status" style={{ padding: 12, backgroundColor: 'rgba(16,185,129,.1)', border: '1px solid #10b981', borderRadius: 4, color: '#6ee7b7', fontSize: 13, marginBottom: 20 }}>
          {success}
        </div>
      )}

      <div className={styles.mappingGrid} style={{ gridTemplateColumns: '2fr 3fr' }}>
        <div className={styles.mappingCard}>
          <h3 style={{ marginBottom: 16, fontSize: 15 }}>Create Category Node</h3>
          
          <form onSubmit={handleCreateCategory}>
            <div className={styles.formGroup}>
              <label>Category Node Name</label>
              <input 
                type="text" 
                placeholder="e.g. Daily Cleanser, Facial Serum"
                value={newCatName}
                onChange={(e) => setNewCatName(e.target.value)}
                className={styles.inputField}
                required
              />
            </div>

            <div className={styles.formGroup}>
              <label>Parent Category (Optional)</label>
              <select
                value={newCatParent}
                onChange={(e) => setNewCatParent(e.target.value)}
                className={styles.inputField}
                style={{ backgroundColor: '#0b0f19' }}
              >
                <option value="">-- Root Level Category --</option>
                {categories.map(c => (
                  <option key={c.id} value={c.id}>{c.path}</option>
                ))}
              </select>
            </div>

            <button disabled={saving} type="submit" className={`${styles.btn} ${styles.btnPrimary}`} style={{ width: '100%', justifyContent: 'center', marginTop: 12 }}>
              <Plus size={16} /> {saving ? "Saving…" : "Add Category Node"}
            </button>
          </form>
        </div>

        <div className={styles.panelCard} style={{ margin: 0 }}>
          <div className={styles.panelTitle}>
            <FolderTree size={18} color="#6366f1" />
            <span>Active Taxonomy Paths ({categories.length})</span>
          </div>

          <div style={{ position: 'relative', marginBottom: 14 }}>
            <Search size={16} color="#64748b" style={{ position: 'absolute', left: 11, top: 11 }} />
            <input
              className={styles.inputField}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Filter taxonomy paths…"
              style={{ paddingLeft: 34 }}
            />
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 12, overflowY: 'auto', maxHeight: '450px', paddingRight: 8 }}>
            {loading ? (
              <div style={{ color: '#64748b', fontSize: 13 }}>Loading taxonomy paths...</div>
            ) : categories.length === 0 ? (
              <div style={{ color: '#64748b', fontSize: 13 }}>No categories configured. Seeded standard trees: Skincare, Haircare, Makeup, Fragrance.</div>
            ) : visibleCategories.length === 0 ? (
              <div style={{ color: '#64748b', fontSize: 13 }}>No taxonomy paths match this filter.</div>
            ) : (
              visibleCategories.map(c => (
                <div key={c.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', backgroundColor: 'rgba(255,255,255,0.02)', border: '1px solid #2e3c64', borderRadius: 4, fontSize: 12 }}>
                  <span style={{ color: '#64748b' }}>[{c.level}]</span>
                  {editingId === c.id ? (
                    <input
                      autoFocus
                      className={styles.inputField}
                      value={editingName}
                      onChange={(event) => setEditingName(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") handleRename(c.id);
                        if (event.key === "Escape") setEditingId(null);
                      }}
                      style={{ padding: '5px 8px', flex: 1 }}
                    />
                  ) : (
                    <span style={{ color: '#a5b4fc', flex: 1, fontFamily: 'monospace' }}>{c.path}</span>
                  )}
                  <span className={`${styles.badge} ${styles.badgeNeutral}`}>{c.product_count} products</span>
                  {editingId === c.id ? (
                    <>
                      <button disabled={saving} title="Save category name" className={`${styles.btn} ${styles.btnPrimary}`} style={{ padding: 6 }} onClick={() => handleRename(c.id)}><Save size={14} /></button>
                      <button title="Cancel editing" className={`${styles.btn} ${styles.btnSecondary}`} style={{ padding: 6 }} onClick={() => setEditingId(null)}><X size={14} /></button>
                    </>
                  ) : (
                    <>
                      <button title="Rename category" className={`${styles.btn} ${styles.btnSecondary}`} style={{ padding: 6 }} onClick={() => { setEditingId(c.id); setEditingName(c.name); }}><Pencil size={14} /></button>
                      <button disabled={saving} title="Delete category" className={`${styles.btn} ${styles.btnSecondary}`} style={{ padding: 6, color: '#f87171' }} onClick={() => handleDelete(c)}><Trash2 size={14} /></button>
                    </>
                  )}
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </Shell>
  );
}
