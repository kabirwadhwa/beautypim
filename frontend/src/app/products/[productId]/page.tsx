"use client";
import { API_URL, BACKEND_URL } from '../../../config';

import React, { useEffect, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import Shell from '../../../components/Shell';
import { 
  ArrowLeft, CheckCircle2, ShieldAlert, AlertTriangle, 
  History, Settings, Sparkles, BookOpen, User,
  ChevronDown, ChevronUp, Info, ExternalLink, RefreshCw, AlertCircle
  , Download, Image as ImageIcon
} from 'lucide-react';
import styles from '../../page.module.css';

interface FieldValue {
  id: string;
  field_name: string;
  value: any;
  source_type: string;
  source_reference: string | null;
  confidence_score: number | null;
  review_status: string;
  reviewer_id: string | null;
  enrichment_run_id: string | null;
  is_current: boolean;
  created_at: string;
  updated_at: string | null;
  override_reason: string | null;
  evidence: Array<{
    source_field: string;
    supporting_text: string;
    evidence_type: string;
  }>;
  reasoning_summary: string | null;
  semantic_status: string | null;
  semantic_status_type: string | null;
  enrichment_run?: {
    provider: string;
    model: string;
    model_version: string;
    prompt_version: string;
    schema_version: string;
    created_at: string;
  } | null;
}

interface ProductDetail {
  id: string;
  internal_code: string;
  product_name: string;
  description: string | null;
  image_url: string | null;
  gtin: string | null;
  brand_id: string | null;
  brand_name: string | null;
  category_id: string | null;
  category_path: string | null;
  review_status: string;
  reviewer_id: string | null;
  is_deleted: boolean;
  created_at: string;
  updated_at: string;
  variants: Array<{ id: string; gtin: string | null; size: string | null; unit: string | null }>;
  formulations: Array<{ id: string; raw_inci_text: string; market: string | null }>;
  field_values: FieldValue[];
  validation_issues: Array<{
    id: string;
    field_name: string | null;
    severity: string;
    issue_type: string;
    message: string;
    resolved: boolean;
  }>;
  enrichment_metadata?: {
    provider: string;
    model: string;
    prompt_version: string;
    schema_version: string;
    status: string;
    tokens: number;
    processing_time_ms: number;
    created_at: string;
  } | null;
  key_ingredients?: Array<{
    name: string;
    normalized_inci_name: string;
    functions: string[];
    benefits: string[];
    is_key_ingredient: boolean;
    key_ingredient_status: string;
    source_type: string;
    evidence: any[];
    confidence: number | null;
    formulation_reference: string;
  }>;
  dynamic_concerns?: Array<{
    concern_name: string;
    targeting_status: string;
    evidence: any[];
    confidence: number | null;
    source: string;
  }>;
}

export default function ProductDetailPage() {
  const params = useParams();
  const router = useRouter();
  const productId = params.productId as string;

  const [product, setProduct] = useState<ProductDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  
  // Collapse states for issues
  const [collapseBlocking, setCollapseBlocking] = useState(false);
  const [collapseWarning, setCollapseWarning] = useState(false);
  const [collapseInfo, setCollapseInfo] = useState(true);

  // Field details accordion states
  const [expandedFields, setExpandedFields] = useState<Record<string, boolean>>({});

  // Override modal state
  const [showOverride, setShowOverride] = useState(false);
  const [overrideField, setOverrideField] = useState<string | null>(null);
  const [overrideValue, setOverrideValue] = useState<string>('');
  const [overrideReason, setOverrideReason] = useState<string>('');
  const [saveLoading, setSaveLoading] = useState(false);
  const [reEnrichLoading, setReEnrichLoading] = useState(false);
  const [pdfLoading, setPdfLoading] = useState(false);
  const [imageSaving, setImageSaving] = useState(false);
  const [imageUrlDraft, setImageUrlDraft] = useState('');
  const [imageLoadFailed, setImageLoadFailed] = useState(false);

  const fetchDetail = async () => {
    try {
      const token = localStorage.getItem("token");
      const headers = { "Authorization": `Bearer ${token}` };
      const resp = await fetch(`${API_URL}/products/${productId}`, { headers });
      if (!resp.ok) throw new Error("Product details not found.");
      const data = await resp.json();
      setProduct(data);
      setImageUrlDraft(data.image_url || '');
      setImageLoadFailed(false);
    } catch (e: any) {
      setError(e.message || "Failed to load.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchDetail();
  }, [productId]);

  const toggleFieldExpand = (fieldName: string) => {
    setExpandedFields(prev => ({ ...prev, [fieldName]: !prev[fieldName] }));
  };

  const handleStatusChange = async (action: 'approve' | 'reject') => {
    try {
      const token = localStorage.getItem("token");
      const resp = await fetch(`${API_URL}/products/${productId}/${action}`, {
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

  const handleReEnrich = async () => {
    setReEnrichLoading(true);
    setError(null);
    try {
      const token = localStorage.getItem("token");
      const resp = await fetch(`${API_URL}/products/${productId}/re-enrich`, {
        method: "POST",
        headers: { "Authorization": `Bearer ${token}` }
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "Re-enrichment failed.");
      setProduct(data);
    } catch (e: any) {
      setError(e.message || "Re-enrichment failed.");
    } finally {
      setReEnrichLoading(false);
    }
  };

  const handleDownloadPdf = async () => {
    setPdfLoading(true);
    setError(null);
    try {
      const token = localStorage.getItem("token");
      const resp = await fetch(`${API_URL}/products/${productId}/pdf`, {
        headers: { "Authorization": `Bearer ${token}` }
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail || "PDF generation failed.");
      }
      const blob = await resp.blob();
      const disposition = resp.headers.get("content-disposition") || "";
      const filename = disposition.match(/filename="([^"]+)"/)?.[1] || "beauty-pim-product-sheet.pdf";
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (e: any) {
      setError(e.message || "PDF generation failed.");
    } finally {
      setPdfLoading(false);
    }
  };

  const handleSaveImageUrl = async () => {
    setImageSaving(true);
    setError(null);
    try {
      const token = localStorage.getItem("token");
      const resp = await fetch(`${API_URL}/products/${productId}/image`, {
        method: "PUT",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ image_url: imageUrlDraft.trim() || null })
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "Image URL could not be saved.");
      setProduct(data);
      setImageUrlDraft(data.image_url || "");
      setImageLoadFailed(false);
    } catch (e: any) {
      setError(e.message || "Image URL could not be saved.");
    } finally {
      setImageSaving(false);
    }
  };

  const openOverrideModal = (fieldName: string, currentValue: any) => {
    setOverrideField(fieldName);
    setOverrideValue(currentValue !== null && currentValue !== undefined ? String(currentValue) : '');
    setOverrideReason('');
    setShowOverride(true);
  };

  const handleSaveField = async () => {
    if (!overrideField) return;
    setSaveLoading(true);
    setError(null);
    try {
      const token = localStorage.getItem("token");
      
      // Determine typed value based on registry matching
      let parsedValue: any = overrideValue;
      if (overrideValue.toLowerCase() === 'true') parsedValue = true;
      else if (overrideValue.toLowerCase() === 'false') parsedValue = false;
      
      const resp = await fetch(`${API_URL}/products/${productId}`, {
        method: "PUT",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          field_name: overrideField,
          value: parsedValue,
          reason: overrideReason
        })
      });
      if (!resp.ok) {
        const data = await resp.json();
        throw new Error(data.detail || "Failed to edit field value.");
      }
      setShowOverride(false);
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
  const warningIssues = activeIssues.filter(i => i.severity === "warning");
  const infoIssues = activeIssues.filter(i => i.severity === "informational" || i.severity === "info");

  // Editable fields registry listing
  const coreFields = [
    "subcategory", "product_type", "gender_target", "texture", "application_area",
    "target_audience", "vegan", "cruelty_free", "paraben_free", "sulfate_free",
    "silicone_free", "alcohol_free", "fragrance_present"
  ];
  const richFields = [
    "source_claims", "benefits", "directions", "skin_type_fit", "hair_type_fit",
    "fragrance_intelligence", "pregnancy_warning_observation",
    "allergen_warning_observation", "sensitivity_warning_observation"
  ];

  const prettyStructuredValue = (value: any): string[] => {
    if (value === null || value === undefined || value === "") return ["Not provided"];
    if (Array.isArray(value)) {
      if (value.length === 0) return ["Not provided"];
      return value.map(item => {
        if (typeof item !== "object") return String(item);
        return item.statement || item.value || item.name || item.ingredient_name ||
          Object.entries(item).filter(([, val]) => typeof val !== "object")
            .map(([key, val]) => `${key.replaceAll("_", " ")}: ${String(val)}`).join(" · ");
      });
    }
    if (typeof value === "object") {
      if (typeof value.value === "string") return [value.value];
      return Object.entries(value)
        .filter(([, val]) => val !== null && val !== "" && !Array.isArray(val) && typeof val !== "object")
        .map(([key, val]) => `${key.replaceAll("_", " ")}: ${String(val)}`);
    }
    return [String(value)];
  };

  const displayValue = (value: any, semanticStatus?: string | null) => {
    if (semanticStatus && ["unknown", "not_applicable"].includes(semanticStatus.toLowerCase())) return 'NOT PROVIDED';
    if (value === null || value === undefined || value === '') return 'NOT PROVIDED';
    if (typeof value === 'object') {
      return value.review_message || value.observation_type || 'STRUCTURED OBSERVATION';
    }
    const normalized = String(value).trim().toLowerCase();
    if (["unknown", "null", "none", "nan"].includes(normalized)) return 'NOT PROVIDED';
    return String(value).toUpperCase();
  };

  return (
    <Shell>
      <div className={styles.pageHeader}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <button onClick={() => router.push("/products")} className={styles.btnSecondary} style={{ padding: 8 }}>
            <ArrowLeft size={18} />
          </button>
          <div className={styles.titleGroup}>
            <h1>{product?.product_name}</h1>
            <p>Brand: <span style={{ fontWeight: 600, color: '#f8fafc' }}>{product?.brand_name || "Not provided"}</span> | Category: <span style={{ color: '#94a3b8' }}>{product?.category_path || "Not provided"}</span></p>
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
            onClick={handleDownloadPdf}
            className={`${styles.btn} ${styles.btnSecondary}`}
            disabled={pdfLoading}
            title="Download a catalogue-grounded product intelligence sheet"
          >
            <Download size={16} />
            {pdfLoading ? "Generating..." : "Generate PDF"}
          </button>

          <button
            onClick={handleReEnrich}
            className={`${styles.btn} ${styles.btnSecondary}`}
            disabled={reEnrichLoading}
            title="Regenerate enrichment from the latest imported source record"
          >
            <RefreshCw size={16} className={reEnrichLoading ? styles.spin : undefined} />
            {reEnrichLoading ? "Enriching..." : "Re-enrich"}
          </button>

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
        <div style={{ padding: 12, backgroundColor: 'rgba(239, 68, 68, 0.1)', border: '1px solid #ef4444', borderRadius: 6, color: '#ef4444', fontSize: 13, marginBottom: 20 }}>
          {error}
        </div>
      )}

      {blockingIssues.length > 0 && (
        <div style={{ padding: 12, backgroundColor: 'rgba(239, 68, 68, 0.1)', border: '1px solid #ef4444', borderRadius: 6, color: '#ef4444', fontSize: 13, marginBottom: 20, display: 'flex', alignItems: 'center', gap: 8 }}>
          <AlertTriangle size={18} />
          <span>Product cannot be approved until all blocking validation issues are resolved.</span>
        </div>
      )}

      <div className={styles.panelCard} style={{ marginBottom: 20 }}>
        <div style={{ display: 'grid', gridTemplateColumns: '180px minmax(0, 1fr)', gap: 20, alignItems: 'center' }}>
          <div style={{
            width: 180, height: 180, borderRadius: 10, overflow: 'hidden',
            border: '1px solid #334155', background: '#0b1220', display: 'flex',
            alignItems: 'center', justifyContent: 'center'
          }}>
            {product?.image_url && !imageLoadFailed ? (
              // Product imagery is supplied dynamically by catalogue users, so Next Image
              // cannot safely predeclare every remote host.
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={product.image_url}
                alt={`${product.product_name} product`}
                style={{ width: '100%', height: '100%', objectFit: 'contain', background: '#fff' }}
                onError={() => setImageLoadFailed(true)}
              />
            ) : (
              <div style={{ textAlign: 'center', color: '#64748b' }}>
                <ImageIcon size={34} style={{ marginBottom: 8 }} />
                <div style={{ fontSize: 12 }}>No product image</div>
              </div>
            )}
          </div>
          <div>
            <div className={styles.panelTitle} style={{ marginBottom: 10 }}>
              <ImageIcon size={18} color="#60a5fa" />
              <span>Product Image URL</span>
            </div>
            <p style={{ color: '#94a3b8', fontSize: 13, marginBottom: 12 }}>
              Add a public HTTPS image URL. It will appear on this page and in generated product PDFs.
            </p>
            <div style={{ display: 'flex', gap: 10 }}>
              <input
                type="url"
                value={imageUrlDraft}
                onChange={(event) => setImageUrlDraft(event.target.value)}
                placeholder="https://cdn.example.com/product-image.jpg"
                aria-label="Product image URL"
                style={{
                  flex: 1, minWidth: 0, background: '#0b1220', border: '1px solid #334155',
                  borderRadius: 6, color: '#e2e8f0', padding: '10px 12px', fontSize: 13
                }}
              />
              <button
                className={`${styles.btn} ${styles.btnPrimary}`}
                onClick={handleSaveImageUrl}
                disabled={imageSaving || imageUrlDraft === (product?.image_url || '')}
              >
                {imageSaving ? "Saving..." : "Save Image"}
              </button>
            </div>
            {product?.image_url && (
              <a
                href={product.image_url}
                target="_blank"
                rel="noreferrer"
                style={{ display: 'inline-flex', alignItems: 'center', gap: 5, marginTop: 9, color: '#60a5fa', fontSize: 12 }}
              >
                Open original image <ExternalLink size={12} />
              </a>
            )}
          </div>
        </div>
      </div>

      {/* Global AI Enrichment Run Metadata */}
      {product?.enrichment_metadata && (
        <div className={styles.panelCard} style={{ marginBottom: 20, background: 'linear-gradient(135deg, #131c35 0%, #0d1222 100%)', borderColor: '#3b82f633' }}>
          <div className={styles.panelTitle} style={{ borderBottom: '1px solid #3b82f622', paddingBottom: 10 }}>
            <Sparkles size={18} color="#3b82f6" />
            <span style={{ fontWeight: 600, color: '#93c5fd' }}>Active AI Enrichment Run Diagnostics</span>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 16, marginTop: 12, fontSize: 12 }}>
            <div>
              <div style={{ color: '#64748b', marginBottom: 2 }}>LLM Provider</div>
              <div style={{ fontWeight: 500, color: '#e2e8f0' }}>{product.enrichment_metadata.provider}</div>
            </div>
            <div>
              <div style={{ color: '#64748b', marginBottom: 2 }}>Model Engine</div>
              <div style={{ fontWeight: 500, color: '#e2e8f0' }}>{product.enrichment_metadata.model}</div>
            </div>
            <div>
              <div style={{ color: '#64748b', marginBottom: 2 }}>Prompt / Schema Version</div>
              <div style={{ fontWeight: 500, color: '#e2e8f0' }}>v{product.enrichment_metadata.prompt_version} / v{product.enrichment_metadata.schema_version}</div>
            </div>
            <div>
              <div style={{ color: '#64748b', marginBottom: 2 }}>Run Status</div>
              <div style={{ fontWeight: 500, color: product.enrichment_metadata.status === 'failed' ? '#ef4444' : '#10b981', display: 'flex', alignItems: 'center', gap: 4 }}>
                <CheckCircle2 size={12} /> {product.enrichment_metadata.provider === 'Deterministic Fallback' ? 'FALLBACK ACTIVE' : product.enrichment_metadata.status.toUpperCase()}
              </div>
            </div>
            <div>
              <div style={{ color: '#64748b', marginBottom: 2 }}>Tokens Consumed</div>
              <div style={{ fontWeight: 500, color: '#e2e8f0' }}>{product.enrichment_metadata.tokens} tokens</div>
            </div>
            <div>
              <div style={{ color: '#64748b', marginBottom: 2 }}>Processing Time</div>
              <div style={{ fontWeight: 500, color: '#e2e8f0' }}>{(product.enrichment_metadata.processing_time_ms / 1000).toFixed(2)}s</div>
            </div>
          </div>
        </div>
      )}

      <div className={styles.detailGrid}>
        <div>
          {/* AI Enriched Beauty Schema Attributes */}
          <div className={styles.panelCard}>
            <div className={styles.panelTitle} style={{ borderBottom: '1px solid #1e293b', paddingBottom: 10 }}>
              <Sparkles size={18} color="#6366f1" />
              <span>AI Enriched Beauty Schema Attributes</span>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 16, marginTop: 12 }}>
              {coreFields.map(field => {
                const fv = currentValDict[field];
                const isExpanded = !!expandedFields[field];

                return (
                  <div key={field} style={{ padding: 14, backgroundColor: 'rgba(255,255,255,0.01)', borderRadius: 6, border: '1px solid #2e3c64' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                        <span style={{ fontSize: 12, color: '#64748b', textTransform: 'capitalize', fontWeight: 600 }}>{field.replace("_", " ")}</span>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          <span style={{ fontSize: 15, fontWeight: 700, color: '#f1f5f9' }}>
                            {displayValue(fv?.value, fv?.semantic_status)}
                          </span>
                          <span style={{ fontSize: 11, color: '#94a3b8', backgroundColor: '#1e293b', padding: '2px 8px', borderRadius: 4, textTransform: 'capitalize' }}>
                            Source: {fv ? fv.source_type.replace("_", " ") : 'None'}
                          </span>
                          {fv?.confidence_score !== null && fv?.confidence_score !== undefined && (
                            <span style={{ fontSize: 11, color: fv.confidence_score >= 0.8 ? '#10b981' : '#f59e0b', fontWeight: 600 }}>
                              ({Math.round(fv.confidence_score * 100)}% Conf)
                            </span>
                          )}
                        </div>
                      </div>

                      <div style={{ display: 'flex', gap: 8 }}>
                        <button 
                          onClick={() => toggleFieldExpand(field)}
                          className={styles.btnSecondary}
                          style={{ padding: '6px 10px', fontSize: 11, display: 'flex', alignItems: 'center', gap: 4 }}
                        >
                          {isExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />} Evidence
                        </button>
                        <button 
                          onClick={() => openOverrideModal(field, fv?.value)} 
                          className={`${styles.btn} ${styles.btnSecondary}`}
                          style={{ padding: '6px 12px', fontSize: 11, borderColor: '#4f46e555' }}
                        >
                          Override
                        </button>
                      </div>
                    </div>

                    {isExpanded && (
                      <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px dashed #2e3c64', fontSize: 12, color: '#94a3b8' }}>
                        {fv?.reasoning_summary && (
                          <div style={{ marginBottom: 10 }}>
                            <div style={{ fontWeight: 600, color: '#e2e8f0', marginBottom: 2 }}>Reasoning Summary:</div>
                            <div style={{ fontStyle: 'italic', backgroundColor: '#0f172a', padding: 8, borderRadius: 4 }}>{fv.reasoning_summary}</div>
                          </div>
                        )}
                        {fv?.evidence && fv.evidence.length > 0 ? (
                          <div>
                            <div style={{ fontWeight: 600, color: '#e2e8f0', marginBottom: 4 }}>Evidence Source Quotes:</div>
                            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                              {fv.evidence.map((ev, idx) => (
                                <div key={idx} style={{ backgroundColor: '#0f172a', padding: 8, borderRadius: 4, borderLeft: '3px solid #6366f1' }}>
                                  <div style={{ fontWeight: 500, color: '#cbd5e1', marginBottom: 2 }}>
                                    &ldquo;{ev.supporting_text}&rdquo;
                                  </div>
                                  <div style={{ fontSize: 10, color: '#64748b' }}>
                                    Source Field: <span style={{ color: '#94a3b8' }}>{ev.source_field}</span> | Type: <span style={{ color: '#94a3b8' }}>{ev.evidence_type}</span>
                                  </div>
                                </div>
                              ))}
                            </div>
                          </div>
                        ) : (
                          <div style={{ color: '#64748b' }}>No factual quotes found in product source text.</div>
                        )}

                        {fv?.enrichment_run && (
                          <div style={{ marginTop: 10, display: 'flex', flexWrap: 'wrap', gap: 12, fontSize: 10, color: '#64748b', borderTop: '1px solid #1e293b', paddingTop: 8 }}>
                            <span>LLM Model: {fv.enrichment_run.model}</span>
                            <span>Prompt: v{fv.enrichment_run.prompt_version}</span>
                            <span>Schema: v{fv.enrichment_run.schema_version}</span>
                            <span>Run: {new Date(fv.enrichment_run.created_at).toLocaleString()}</span>
                          </div>
                        )}
                        
                        {fv?.override_reason && (
                          <div style={{ marginTop: 10, padding: 8, backgroundColor: 'rgba(245,158,11,0.05)', border: '1px solid #f59e0b33', borderRadius: 4 }}>
                            <div style={{ fontWeight: 600, color: '#f59e0b', marginBottom: 2 }}>Override Audit Log Reason:</div>
                            <div style={{ color: '#e2e8f0' }}>&ldquo;{fv.override_reason}&rdquo; (by User #{fv.reviewer_id})</div>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          {/* Complete structured enrichment, including the fields previously discarded. */}
          <div className={styles.panelCard}>
            <div className={styles.panelTitle} style={{ borderBottom: '1px solid #1e293b', paddingBottom: 10 }}>
              <Info size={18} color="#38bdf8" />
              <span>Claims, Usage, Suitability & Safety Observations</span>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 12, marginTop: 12 }}>
              {richFields.map(field => {
                const fv = currentValDict[field];
                return (
                  <div key={field} style={{ padding: 12, backgroundColor: 'rgba(255,255,255,0.01)', border: '1px solid #2e3c64', borderRadius: 6 }}>
                    <div style={{ color: '#7dd3fc', fontSize: 12, fontWeight: 700, textTransform: 'capitalize', marginBottom: 8 }}>
                      {field.replaceAll("_", " ")}
                    </div>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                      {prettyStructuredValue(fv?.value).map((line, index) => (
                        <div key={index} style={{ color: line === "Not provided" ? '#64748b' : '#e2e8f0', fontSize: 12, lineHeight: 1.5 }}>
                          {line}
                        </div>
                      ))}
                    </div>
                    {fv?.confidence_score !== null && fv?.confidence_score !== undefined && (
                      <div style={{ color: '#94a3b8', fontSize: 10, marginTop: 8 }}>
                        {Math.round(fv.confidence_score * 100)}% confidence · {fv.source_type.replaceAll("_", " ")}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          {/* Key Ingredients Provenance Panel */}
          {product?.key_ingredients && product.key_ingredients.length > 0 && (
            <div className={styles.panelCard}>
              <div className={styles.panelTitle} style={{ borderBottom: '1px solid #1e293b', paddingBottom: 10 }}>
                <BookOpen size={18} color="#10b981" />
                <span>Formulation Key Ingredients Provenance</span>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: 12, marginTop: 12 }}>
                {product.key_ingredients.map((ing, idx) => (
                  <div key={idx} style={{ padding: 12, backgroundColor: 'rgba(255,255,255,0.01)', border: '1px solid #2e3c64', borderRadius: 6 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <div>
                        <div style={{ fontWeight: 700, color: '#f1f5f9', fontSize: 14 }}>{ing.name}</div>
                        <div style={{ fontSize: 11, color: '#94a3b8' }}>INCI Name: {ing.normalized_inci_name}</div>
                      </div>
                      
                      {/*Provenance labeling badges */}
                      <span className={`${styles.badge} ${
                        ing.source_type === 'human_edit' ? styles.badgeSuccess :
                        ing.source_type === 'ai_inference' ? styles.badgeNeutral :
                        styles.badgeSecondary
                      }`} style={{ fontSize: 10, padding: '2px 8px' }}>
                        {ing.source_type === 'human_edit' ? 'HUMAN CONFIRMED' :
                         ing.source_type === 'ai_inference' ? 'AI INFERRED' :
                         ing.is_key_ingredient ? 'EXPLICIT KEY INGREDIENT' : 'PARSED INCI INGREDIENT'}
                      </span>
                    </div>

                    <div style={{ marginTop: 8, fontSize: 12, color: '#94a3b8' }}>
                      {ing.functions.length > 0 && (
                        <div>Functions: <span style={{ color: '#cbd5e1' }}>{ing.functions.join(', ')}</span></div>
                      )}
                      {ing.benefits.length > 0 && (
                        <div>Benefits: <span style={{ color: '#cbd5e1' }}>{ing.benefits.join(', ')}</span></div>
                      )}
                      {ing.confidence && (
                        <div style={{ marginTop: 4, fontSize: 11, color: '#f59e0b' }}>
                          Confidence: {Math.round(ing.confidence * 100)}%
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Raw INCI Ingredients */}
          <div className={styles.panelCard}>
            <div className={styles.panelTitle}>
              <BookOpen size={18} color="#94a3b8" />
              <span>Raw Ingredients Ingredients List</span>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12, fontSize: 13, color: '#94a3b8' }}>
              <div style={{ backgroundColor: '#0b0f19', padding: 12, borderRadius: 4, fontFamily: 'monospace', color: '#f8fafc', whiteSpace: 'pre-wrap' }}>
                {product?.formulations[0]?.raw_inci_text || "No ingredients list recorded for this product."}
              </div>
            </div>
          </div>
        </div>

        <div>
          {/* Validation Issues Panel (Collapsible severity groups) */}
          <div className={styles.panelCard}>
            <div className={styles.panelTitle} style={{ borderBottom: '1px solid #1e293b', paddingBottom: 10 }}>
              <ShieldAlert size={18} color="#ef4444" />
              <span>Validation Warning Alerts ({activeIssues.length})</span>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 12, marginTop: 12 }}>
              {activeIssues.length === 0 ? (
                <div style={{ fontSize: 13, color: '#64748b', display: 'flex', alignItems: 'center', gap: 8 }}>
                  <CheckCircle2 size={16} color="#10b981" />
                  <span>Validation rules passed. Product contains no warnings.</span>
                </div>
              ) : (
                <>
                  {/* Blocking Issues */}
                  <div style={{ border: '1px solid #ef444433', borderRadius: 6, overflow: 'hidden' }}>
                    <button 
                      onClick={() => setCollapseBlocking(!collapseBlocking)}
                      style={{ width: '100%', display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: 12, backgroundColor: 'rgba(239, 68, 68, 0.05)', color: '#f87171', border: 'none', fontWeight: 600, fontSize: 13, textAlign: 'left', cursor: 'pointer' }}
                    >
                      <span>Blocking Errors ({blockingIssues.length})</span>
                      {collapseBlocking ? <ChevronDown size={16} /> : <ChevronUp size={16} />}
                    </button>
                    {!collapseBlocking && (
                      <div style={{ padding: 12, backgroundColor: '#0d101a', display: 'flex', flexDirection: 'column', gap: 8 }}>
                        {blockingIssues.length === 0 ? (
                          <div style={{ color: '#64748b', fontSize: 12 }}>No active blocking issues.</div>
                        ) : (
                          blockingIssues.map(issue => (
                            <div key={issue.id} style={{ fontSize: 12, color: '#f8fafc', borderLeft: '3px solid #ef4444', paddingLeft: 8 }}>
                              <div style={{ fontWeight: 600, color: '#f87171' }}>{issue.issue_type.toUpperCase()}</div>
                              <div>{issue.message}</div>
                            </div>
                          ))
                        )}
                      </div>
                    )}
                  </div>

                  {/* Warning Issues */}
                  <div style={{ border: '1px solid #f59e0b33', borderRadius: 6, overflow: 'hidden' }}>
                    <button 
                      onClick={() => setCollapseWarning(!collapseWarning)}
                      style={{ width: '100%', display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: 12, backgroundColor: 'rgba(245, 158, 11, 0.05)', color: '#fbbf24', border: 'none', fontWeight: 600, fontSize: 13, textAlign: 'left', cursor: 'pointer' }}
                    >
                      <span>Warnings ({warningIssues.length})</span>
                      {collapseWarning ? <ChevronDown size={16} /> : <ChevronUp size={16} />}
                    </button>
                    {!collapseWarning && (
                      <div style={{ padding: 12, backgroundColor: '#0d101a', display: 'flex', flexDirection: 'column', gap: 8 }}>
                        {warningIssues.length === 0 ? (
                          <div style={{ color: '#64748b', fontSize: 12 }}>No active warning issues.</div>
                        ) : (
                          warningIssues.map(issue => (
                            <div key={issue.id} style={{ fontSize: 12, color: '#f8fafc', borderLeft: '3px solid #f59e0b', paddingLeft: 8 }}>
                              <div style={{ fontWeight: 600, color: '#fbbf24' }}>{issue.issue_type.toUpperCase()}</div>
                              <div>{issue.message}</div>
                            </div>
                          ))
                        )}
                      </div>
                    )}
                  </div>

                  {/* Info Issues */}
                  <div style={{ border: '1px solid #3b82f633', borderRadius: 6, overflow: 'hidden' }}>
                    <button 
                      onClick={() => setCollapseInfo(!collapseInfo)}
                      style={{ width: '100%', display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: 12, backgroundColor: 'rgba(59, 130, 246, 0.05)', color: '#60a5fa', border: 'none', fontWeight: 600, fontSize: 13, textAlign: 'left', cursor: 'pointer' }}
                    >
                      <span>Info Alerts ({infoIssues.length})</span>
                      {collapseInfo ? <ChevronDown size={16} /> : <ChevronUp size={16} />}
                    </button>
                    {!collapseInfo && (
                      <div style={{ padding: 12, backgroundColor: '#0d101a', display: 'flex', flexDirection: 'column', gap: 8 }}>
                        {infoIssues.length === 0 ? (
                          <div style={{ color: '#64748b', fontSize: 12 }}>No active informational issues.</div>
                        ) : (
                          infoIssues.map(issue => (
                            <div key={issue.id} style={{ fontSize: 12, color: '#f8fafc', borderLeft: '3px solid #3b82f6', paddingLeft: 8 }}>
                              <div style={{ fontWeight: 600, color: '#60a5fa' }}>{issue.issue_type.toUpperCase()}</div>
                              <div>{issue.message}</div>
                            </div>
                          ))
                        )}
                      </div>
                    )}
                  </div>
                </>
              )}
            </div>
          </div>

          {/* Dynamic Concerns Panel */}
          {product?.dynamic_concerns && product.dynamic_concerns.length > 0 && (
            <div className={styles.panelCard}>
              <div className={styles.panelTitle} style={{ borderBottom: '1px solid #1e293b', paddingBottom: 10 }}>
                <Sparkles size={18} color="#6366f1" />
                <span>Dynamic Concern Targeting</span>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: 12, marginTop: 12 }}>
                {product.dynamic_concerns.map((concern, idx) => (
                  <div key={idx} style={{ padding: 12, backgroundColor: 'rgba(255,255,255,0.01)', border: '1px solid #2e3c64', borderRadius: 6 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <div style={{ fontWeight: 700, color: '#f1f5f9', textTransform: 'capitalize', fontSize: 14 }}>{concern.concern_name}</div>
                      <span className={`${styles.badge} ${
                        concern.targeting_status === 'explicit' ? styles.badgeSuccess :
                        concern.targeting_status === 'inferred' ? styles.badgeNeutral :
                        styles.badgeSecondary
                      }`} style={{ fontSize: 10, padding: '2px 8px' }}>
                        {displayValue(concern.targeting_status)}
                      </span>
                    </div>
                    {concern.confidence && (
                      <div style={{ fontSize: 11, color: '#64748b', marginTop: 4 }}>
                        Confidence Score: {Math.round(concern.confidence * 100)}%
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

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
                      <span style={{ fontWeight: 600, color: '#f8fafc' }}>{fv.field_name} set to &ldquo;{displayValue(fv.value, fv.semantic_status)}&rdquo;</span>
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

      {/* Override Modal */}
      {showOverride && (
        <div style={{
          position: 'fixed',
          top: 0, left: 0, right: 0, bottom: 0,
          backgroundColor: 'rgba(0,0,0,0.7)',
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'center',
          zIndex: 1000
        }}>
          <div style={{
            width: '450px',
            backgroundColor: '#0d1325',
            border: '1px solid #3b82f644',
            borderRadius: 8,
            padding: 24,
            boxShadow: '0 20px 25px -5px rgba(0, 0, 0, 0.5)'
          }}>
            <h3 style={{ fontSize: 18, color: '#f8fafc', marginBottom: 8, textTransform: 'capitalize' }}>
              Override Enriched Field: {overrideField?.replace("_", " ")}
            </h3>
            <p style={{ fontSize: 12, color: '#64748b', marginBottom: 20 }}>
              Adjust this enriched attribute and log your correction reason.
            </p>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              <div>
                <label style={{ display: 'block', fontSize: 12, color: '#94a3b8', marginBottom: 6, fontWeight: 500 }}>
                  New Attribute Value
                </label>
                {/* Switch inputs dynamically for claims versus text fields */}
                {overrideField === 'vegan' || overrideField === 'cruelty_free' || overrideField === 'fragrance_present' ? (
                  <select 
                    className={styles.inputField}
                    value={overrideValue}
                    onChange={(e) => setOverrideValue(e.target.value)}
                    style={{ backgroundColor: '#0f172a', color: '#f1f5f9', border: '1px solid #334155' }}
                  >
                    <option value="">-- SELECT STATUS --</option>
                    <option value="true">True (Confirmed)</option>
                    <option value="false">False (Absent)</option>
                    <option value="unknown">Unknown</option>
                  </select>
                ) : (
                  <input 
                    type="text"
                    className={styles.inputField}
                    value={overrideValue}
                    onChange={(e) => setOverrideValue(e.target.value)}
                    placeholder="Enter value..."
                    style={{ backgroundColor: '#0f172a', color: '#f1f5f9', border: '1px solid #334155' }}
                  />
                )}
              </div>

              <div>
                <label style={{ display: 'block', fontSize: 12, color: '#94a3b8', marginBottom: 6, fontWeight: 500 }}>
                  Reason for Override
                </label>
                <textarea 
                  className={styles.inputField}
                  value={overrideReason}
                  onChange={(e) => setOverrideReason(e.target.value)}
                  placeholder="Explain why this change is necessary..."
                  rows={3}
                  style={{ backgroundColor: '#0f172a', color: '#f1f5f9', border: '1px solid #334155', resize: 'none' }}
                />
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 12 }}>
                <button 
                  onClick={() => setShowOverride(false)} 
                  className={styles.btnSecondary}
                >
                  Cancel
                </button>
                <button 
                  onClick={handleSaveField} 
                  className={`${styles.btn} ${styles.btnPrimary}`}
                  disabled={saveLoading || !overrideValue.trim() || !overrideReason.trim()}
                  style={{ opacity: (!overrideValue.trim() || !overrideReason.trim()) ? 0.5 : 1 }}
                >
                  {saveLoading ? "Saving..." : "Confirm Override"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </Shell>
  );
}
