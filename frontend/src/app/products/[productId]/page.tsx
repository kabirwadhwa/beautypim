"use client";

import React, { useEffect, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import Shell from '../../../components/Shell';
import { 
  ArrowLeft, CheckCircle2, ShieldAlert, AlertTriangle, 
  History, Settings, Sparkles, BookOpen, User 
} from 'lucide-react';
import styles from '../../page.module.css';

interface ProductDetail {
  id: string;
  product_name: string;
  brand_name: string;
  category_path: string | null;
  review_status: string;
  variants: Array<{ id: string; gtin: string | null; size: string | null; unit: string | null }>;
  formulations: Array<{ id: string; raw_inci_text: string; market: string | null }>;
  field_values: Array<{
    id: string;
    field_name: string;
    value: any;
    source_type: string;
    confidence_score: number | null;
    review_status: string;
    is_current: boolean;
    created_at: string;
  }>;
  validation_issues: Array<{
    id: string;
    field_name: string | null;
    severity: string;
    issue_type: string;
    message: string;
    resolved: boolean;
  }>;
}

export default function ProductDetailPage() {
  const params = useParams();
  const router = useRouter();
  const productId = params.productId as string;

  const [product, setProduct] = useState<ProductDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editingField, setEditingField] = useState<string | null>(null);
  const [editValue, setEditValue] = useState<any>('');
  const [editReason, setEditReason] = useState('');
  const [saveLoading, setSaveLoading] = useState(false);

  const fetchDetail = async () => {
    try {
      const token = localStorage.getItem("token");
      const headers = { "Authorization": `Bearer ${token}` };
      const resp = await fetch(`http://localhost:8000/api/products/${productId}`, { headers });
      if (!resp.ok) throw new Error("Product details not found.");
      const data = await resp.json();
      setProduct(data);
    } catch (e: any) {
      setError(e.message || "Failed to load.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchDetail();
  }, [productId]);

  const handleStatusChange = async (action: 'approve' | 'reject') => {
    try {
      const token = localStorage.getItem("token");
      const resp = await fetch(`http://localhost:8000/api/products/${productId}/${action}`, {
        method: "POST",
        headers: { "Authorization": `Bearer ${token}` }
      });
      if (!resp.ok) {
        const data = await resp.json();
        throw new Error(data.detail || "Failed to update review status.");
      }
      fetchDetail();
    } catch (e: any) {
      setError(e.message);
    }
  };

  const handleSaveField = async (fieldName: string) => {
    setSaveLoading(true);
    setError(null);
    try {
      const token = localStorage.getItem("token");
      const resp = await fetch(`http://localhost:8000/api/products/${productId}`, {
        method: "PUT",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          field_name: fieldName,
          value: editValue,
          reason: editReason
        })
      });
      if (!resp.ok) throw new Error("Failed to edit field value.");
      setEditingField(null);
      setEditReason('');
      fetchDetail();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSaveLoading(false);
    }
  };

  if (loading && !product) {
    return (
      <Shell>
        <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '50vh', color: '#64748b' }}>
          <span>Loading product details panel...</span>
        </div>
      </Shell>
    );
  }

  const currentValues = product?.field_values.filter(fv => fv.is_current) || [];
  const currentValDict = currentValues.reduce((acc, curr) => {
    acc[curr.field_name] = curr;
    return acc;
  }, {} as Record<string, typeof currentValues[0]>);

  const activeIssues = product?.validation_issues.filter(i => !i.resolved) || [];
  const blockingIssues = activeIssues.filter(i => i.severity === "blocking");

  return (
    <Shell>
      <div className={styles.pageHeader}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <button onClick={() => router.push("/products")} className={styles.btnSecondary} style={{ padding: 8 }}>
            <ArrowLeft size={18} />
          </button>
          <div className={styles.titleGroup}>
            <h1>{product?.product_name}</h1>
            <p>Brand: <span style={{ fontWeight: 600, color: '#f8fafc' }}>{product?.brand_name}</span> | Category: <span style={{ color: '#94a3b8' }}>{product?.category_path || "-"}</span></p>
          </div>
        </div>

        <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
          <span className={`${styles.badge} ${
            product?.review_status === 'approved' ? styles.badgeSuccess :
            product?.review_status === 'rejected' ? styles.badgeDanger :
            styles.badgeNeutral
          }`}>
            {product?.review_status.toUpperCase()}
          </span>

          <button 
            onClick={() => handleStatusChange('approve')} 
            className={`${styles.btn} ${styles.btnPrimary}`}
            disabled={blockingIssues.length > 0}
            style={{ opacity: blockingIssues.length > 0 ? 0.5 : 1 }}
          >
            <CheckCircle2 size={16} /> Approve
          </button>
          <button 
            onClick={() => handleStatusChange('reject')} 
            className={`${styles.btn} ${styles.btnSecondary}`}
            style={{ color: '#ef4444' }}
          >
            <ShieldAlert size={16} /> Reject
          </button>
        </div>
      </div>

      {error && (
        <div style={{ padding: 12, backgroundColor: 'rgba(239, 68, 68, 0.1)', border: '1px solid #ef4444', borderRadius: 4, color: '#ef4444', fontSize: 13, marginBottom: 20 }}>
          {error}
        </div>
      )}

      {blockingIssues.length > 0 && (
        <div style={{ padding: 12, backgroundColor: 'rgba(239, 68, 68, 0.1)', border: '1px solid #ef4444', borderRadius: 4, color: '#ef4444', fontSize: 13, marginBottom: 20, display: 'flex', alignItems: 'center', gap: 8 }}>
          <AlertTriangle size={18} />
          <span>Product cannot be approved until all blocking validation issues are resolved.</span>
        </div>
      )}

      <div className={styles.detailGrid}>
        <div>
          {/* AI Enrichment edit fields */}
          <div className={styles.panelCard}>
            <div className={styles.panelTitle}>
              <Sparkles size={18} color="#6366f1" />
              <span>AI Enriched Beauty Schema Attributes</span>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              {["subcategory", "product_type", "gender_target", "vegan", "cruelty_free", "fragrance_present"].map(field => {
                const fv = currentValDict[field];
                const isEditing = editingField === field;

                return (
                  <div key={field} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '12px 16px', backgroundColor: 'rgba(255,255,255,0.02)', borderRadius: 4, border: '1px solid #2e3c64' }}>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                      <span style={{ fontSize: 12, color: '#64748b', textTransform: 'capitalize' }}>{field.replace("_", " ")}</span>
                      {isEditing ? (
                        <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                          <input 
                            type="text" 
                            className={styles.inputField} 
                            value={editValue} 
                            onChange={(e) => setEditValue(e.target.value)}
                            style={{ width: '160px' }}
                          />
                          <input 
                            type="text" 
                            placeholder="Reason for override..." 
                            className={styles.inputField} 
                            value={editReason} 
                            onChange={(e) => setEditReason(e.target.value)}
                            style={{ width: '200px' }}
                          />
                          <button onClick={() => handleSaveField(field)} className={`${styles.btn} ${styles.btnPrimary}`} style={{ padding: '6px 12px', fontSize: 12 }} disabled={saveLoading}>
                            Save
                          </button>
                          <button onClick={() => setEditingField(null)} className={`${styles.btn} ${styles.btnSecondary}`} style={{ padding: '6px 12px', fontSize: 12 }}>
                            Cancel
                          </button>
                        </div>
                      ) : (
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          <span style={{ fontWeight: 600 }}>{fv ? String(fv.value) : 'UNKNOWN'}</span>
                          <span style={{ fontSize: 11, color: '#64748b', backgroundColor: '#1e294b', padding: '2px 6px', borderRadius: 2, textTransform: 'capitalize' }}>
                            Source: {fv ? fv.source_type.replace("_", " ") : 'None'}
                          </span>
                        </div>
                      )}
                    </div>

                    {!isEditing && (
                      <button 
                        onClick={() => {
                          setEditingField(field);
                          setEditValue(fv ? fv.value : '');
                        }} 
                        className={`${styles.btn} ${styles.btnSecondary}`}
                        style={{ padding: '4px 8px', fontSize: 11 }}
                      >
                        Override Value
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          {/* Warnings & Formulation Observations */}
          <div className={styles.panelCard}>
            <div className={styles.panelTitle}>
              <BookOpen size={18} color="#94a3b8" />
              <span>Warnings and Formulation Observations</span>
            </div>
            
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12, fontSize: 13, color: '#94a3b8' }}>
              <div>Raw INCI Ingredients List:</div>
              <div style={{ backgroundColor: '#0b0f19', padding: 12, borderRadius: 4, fontFamily: 'monospace', color: '#f8fafc', whiteSpace: 'pre-wrap' }}>
                {product?.formulations[0]?.raw_inci_text || "No ingredients list recorded for this product."}
              </div>
            </div>
          </div>
        </div>

        <div>
          {/* Validation Issues Panel */}
          <div className={styles.panelCard}>
            <div className={styles.panelTitle}>
              <ShieldAlert size={18} color="#ef4444" />
              <span>Validation Warning Alerts ({activeIssues.length})</span>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              {activeIssues.length === 0 ? (
                <div style={{ fontSize: 13, color: '#64748b', display: 'flex', alignItems: 'center', gap: 8 }}>
                  <CheckCircle2 size={16} color="#10b981" />
                  <span>Validation rules passed. Product contains no warnings.</span>
                </div>
              ) : (
                activeIssues.map(issue => (
                  <div key={issue.id} style={{ padding: 12, borderRadius: 4, border: '1px solid', borderColor: issue.severity === 'blocking' ? '#ef4444' : '#f59e0b', backgroundColor: issue.severity === 'blocking' ? 'rgba(239, 68, 68, 0.05)' : 'rgba(245, 158, 11, 0.05)', fontSize: 13 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                      <span style={{ fontWeight: 600, color: issue.severity === 'blocking' ? '#ef4444' : '#f59e0b', textTransform: 'uppercase' }}>
                        {issue.severity} | {issue.issue_type.replace("_", " ")}
                      </span>
                    </div>
                    <div style={{ color: '#f8fafc' }}>{issue.message}</div>
                  </div>
                ))
              )}
            </div>
          </div>

          {/* Change History audit log */}
          <div className={styles.panelCard}>
            <div className={styles.panelTitle}>
              <History size={18} color="#6366f1" />
              <span>Provenance Modification Logs</span>
            </div>

            <div className={styles.timeline}>
              {product?.field_values.map(fv => (
                <div key={fv.id} className={styles.timelineItem}>
                  <div className={styles.timelineDot} />
                  <div className={styles.timelineContent}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
                      <span style={{ fontWeight: 600, color: '#f8fafc' }}>{fv.field_name} set to '{String(fv.value)}'</span>
                      <span className={styles.timelineTime}>{new Date(fv.created_at).toLocaleDateString()}</span>
                    </div>
                    <div style={{ fontSize: 11, color: '#64748b', marginTop: 2 }}>
                      Source: {fv.source_type.replace("_", " ")} {fv.confidence_score ? `(Confidence: ${Math.round(fv.confidence_score*100)}%)` : ''}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </Shell>
  );
}
