"use client";

import React, { useState, useEffect } from 'react';
import Shell from '../../../components/Shell';
import { Settings, Plus, FolderTree } from 'lucide-react';
import styles from '../../page.module.css';

interface Category {
  id: string;
  name: string;
  level: number;
  path: string;
}

export default function TaxonomiesPage() {
  const [categories, setCategories] = useState<Category[]>([]);
  const [newCatName, setNewCatName] = useState('');
  const [newCatParent, setNewCatParent] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchCategories = async () => {
    try {
      const token = localStorage.getItem("token");
      const resp = await fetch("http://localhost:8000/api/settings/categories", {
        headers: { "Authorization": `Bearer ${token}` }
      });
      if (resp.ok) {
        const data = await resp.json();
        setCategories(data || []);
      }
    } catch (e) {
      console.error(e);
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
    if (!newCatName) return;

    try {
      const token = localStorage.getItem("token");
      const resp = await fetch("http://localhost:8000/api/settings/categories", {
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
      if (!resp.ok) throw new Error("Failed to create category node.");
      setNewCatName('');
      setNewCatParent('');
      fetchCategories();
    } catch (err: any) {
      setError(err.message);
    }
  };

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

            <button type="submit" className={`${styles.btn} ${styles.btnPrimary}`} style={{ width: '100%', justifyContent: 'center', marginTop: 12 }}>
              <Plus size={16} /> Add Category Node
            </button>
          </form>
        </div>

        <div className={styles.panelCard} style={{ margin: 0 }}>
          <div className={styles.panelTitle}>
            <FolderTree size={18} color="#6366f1" />
            <span>Active Taxonomy Paths ({categories.length})</span>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 12, overflowY: 'auto', maxHeight: '450px', paddingRight: 8 }}>
            {loading ? (
              <div style={{ color: '#64748b', fontSize: 13 }}>Loading taxonomy paths...</div>
            ) : categories.length === 0 ? (
              <div style={{ color: '#64748b', fontSize: 13 }}>No categories configured. Seeded standard trees: Skincare, Haircare, Makeup, Fragrance.</div>
            ) : (
              categories.map(c => (
                <div key={c.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', backgroundColor: 'rgba(255,255,255,0.02)', border: '1px solid #2e3c64', borderRadius: 4, fontFamily: 'monospace', fontSize: 12 }}>
                  <span style={{ color: '#64748b' }}>[{c.level}]</span>
                  <span style={{ color: '#6366f1' }}>{c.path}</span>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </Shell>
  );
}
