"use client";
import { API_URL, BACKEND_URL } from '../../config';

import React, { useState } from 'react';
import Shell from '../../components/Shell';
import { Download, Info, RefreshCw } from 'lucide-react';
import styles from '../page.module.css';

export default function ExportsPage() {
  const [mode, setMode] = useState('business');
  const [format, setFormat] = useState('json');
  const [includeInferred, setIncludeInferred] = useState(false);
  const [webhookUrl, setWebhookUrl] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);

  const downloadExport = async (downloadUrl: string) => {
    const token = localStorage.getItem("token");
    const response = await fetch(`${BACKEND_URL}${downloadUrl}`, {
      headers: { "Authorization": `Bearer ${token}` }
    });
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      throw new Error(body?.detail || "The export file could not be downloaded.");
    }
    const blob = await response.blob();
    const disposition = response.headers.get("content-disposition") || "";
    const filenameMatch = disposition.match(/filename="?([^"]+)"?/i);
    const filename = filenameMatch?.[1] || `beauty_pim_export_${mode}.${format}`;
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(objectUrl);
  };

  const handleRunExport = async () => {
    setLoading(true);
    setError(null);
    setResult(null);

    try {
      if (webhookUrl) {
        const parsed = new URL(webhookUrl);
        if (!["http:", "https:"].includes(parsed.protocol)) throw new Error("Webhook must use HTTP or HTTPS.");
      }
      const token = localStorage.getItem("token");
      const resp = await fetch(`${API_URL}/exports/run`, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          export_mode: mode,
          file_format: format,
          include_inferred: includeInferred,
          webhook_url: webhookUrl || null
        })
      });

      if (!resp.ok) {
        const body = await resp.json().catch(() => null);
        throw new Error(body?.detail || "Failed to generate export catalog.");
      }
      const data = await resp.json();
      setResult(data);
      await downloadExport(data.download_url);
    } catch (err: any) {
      setError(err.message || "Failed to run export.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Shell>
      <div className={styles.pageHeader}>
        <div className={styles.titleGroup}>
          <h1>Export Center</h1>
          <p>Export enriched beauty catalog data to CSV, Excel, JSON or distribute via webhook APIs</p>
        </div>
      </div>

      {error && (
        <div style={{ padding: 12, backgroundColor: 'rgba(239, 68, 68, 0.1)', border: '1px solid #ef4444', borderRadius: 4, color: '#ef4444', fontSize: 13, marginBottom: 20 }}>
          {error}
        </div>
      )}

      <div className={styles.mappingGrid} style={{ gridTemplateColumns: '1fr 1fr' }}>
        <div className={styles.mappingCard}>
          <h3 style={{ marginBottom: 16, fontSize: 15 }}>Configure Export Preferences</h3>

          <div className={styles.formGroup}>
            <label>Export Profile Mode</label>
            <select 
              value={mode} 
              onChange={(e) => setMode(e.target.value)}
              className={styles.inputField}
              style={{ backgroundColor: '#0b0f19' }}
            >
              <option value="business">Business Export (Approved values only)</option>
              <option value="audit">Audit Export (Detailed provenance + warnings history)</option>
            </select>
          </div>

          <div className={styles.formGroup}>
            <label>Output File Format</label>
            <select 
              value={format} 
              onChange={(e) => setFormat(e.target.value)}
              className={styles.inputField}
              style={{ backgroundColor: '#0b0f19' }}
            >
              <option value="json">JSON format (.json)</option>
              <option value="csv">Semicolon Delimited CSV (.csv)</option>
              <option value="xlsx">Excel Spreadsheet (.xlsx)</option>
            </select>
          </div>

          {mode === 'business' && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 20 }}>
              <input 
                type="checkbox" 
                id="include-inferred" 
                checked={includeInferred}
                onChange={(e) => setIncludeInferred(e.target.checked)}
              />
              <label htmlFor="include-inferred" style={{ fontSize: 13, color: '#f8fafc', cursor: 'pointer' }}>
                Include AI inferred values (falls back to blank/unknown if unchecked)
              </label>
            </div>
          )}

          <div className={styles.formGroup} style={{ borderTop: '1px solid #2e3c64', paddingTop: 16 }}>
            <label>API Distribution Webhook Target (Optional)</label>
            <input 
              type="url" 
              placeholder="https://api.retailer.com/v1/ingest"
              value={webhookUrl}
              onChange={(e) => setWebhookUrl(e.target.value)}
              className={styles.inputField}
            />
          </div>

          <button 
            onClick={handleRunExport}
            className={`${styles.btn} ${styles.btnPrimary}`} 
            style={{ width: '100%', justifyContent: 'center', marginTop: 12 }}
            disabled={loading}
          >
            <Download size={18} /> {loading ? "Generating Download File..." : "Generate and Download Catalog"}
          </button>

          {result && (
            <button
              type="button"
              onClick={() => downloadExport(result.download_url).catch((err) => setError(err.message))}
              className={`${styles.btn} ${styles.btnSecondary}`}
              style={{ width: '100%', justifyContent: 'center', marginTop: 10 }}
            >
              <RefreshCw size={16} /> Download Again
            </button>
          )}
        </div>

        <div>
          <div className={styles.panelCard} style={{ margin: 0, height: '100%' }}>
            <h3 style={{ marginBottom: 12, fontSize: 15 }}>Export Profiles Overview</h3>
            
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16, fontSize: 13, color: '#94a3b8' }}>
              <div style={{ display: 'flex', gap: 12 }}>
                <Info size={24} color="#6366f1" style={{ flexShrink: 0 }} />
                <div>
                  <div style={{ fontWeight: 600, color: '#f8fafc', marginBottom: 4 }}>Business Profile</div>
                  <span>Filters catalog to approved products only. Resolves conflicts, normalizes nested arrays to flat strings, and formats records for downstream retailers like Marionnaud or Sephora.</span>
                </div>
              </div>

              <div style={{ display: 'flex', gap: 12, borderTop: '1px solid #2e3c64', paddingTop: 16 }}>
                <Info size={24} color="#f59e0b" style={{ flexShrink: 0 }} />
                <div>
                  <div style={{ fontWeight: 600, color: '#f8fafc', marginBottom: 4 }}>Audit Profile</div>
                  <span>Outputs all catalog elements (including pending reviews or rejected duplicates) with raw provenance history logs, token prices, and active warnings list.</span>
                </div>
              </div>

              {result && (
                <div style={{ padding: 12, backgroundColor: 'rgba(16, 185, 129, 0.1)', border: '1px solid #10b981', borderRadius: 4, color: '#10b981', marginTop: 16 }}>
                  Export completed with {result.row_count} product{result.row_count === 1 ? "" : "s"}.
                  {result.row_count === 0 && mode === "business" && " Approve products in Product Grid to include them in a business export."}
                  {result.webhook_triggered && " Webhook API trigger sent successfully."}
                  {webhookUrl && !result.webhook_triggered && " The file was generated, but the webhook was not delivered."}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </Shell>
  );
}
