"use client";

import React, { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import Shell from '../../components/Shell';
import { UploadCloud, CheckCircle, AlertTriangle } from 'lucide-react';
import styles from '../page.module.css';

interface Template {
  id: string;
  name: string;
  column_mapping: Record<string, string>;
}

export default function ImportsPage() {
  const router = useRouter();
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<any>(null);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [selectedTemplate, setSelectedTemplate] = useState<string>('');
  
  # Mapping selections states
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [saveTemplate, setSaveTemplate] = useState(false);
  const [templateName, setTemplateName] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canonicalFields = [
    { key: "product_name", label: "Product Name (Required)" },
    { key: "brand", label: "Brand (Required)" },
    { key: "ean", label: "GTIN / EAN / UPC Barcode" },
    { key: "size", label: "Unit Size (e.g. 50ml, 30ml)" },
    { key: "price", label: "List Price" },
    { key: "description", label: "Product Description" },
    { key: "ingredients", label: "Raw INCI Ingredients List" }
  ];

  useEffect(() => {
    const fetchTemplates = async () => {
      try {
        const token = localStorage.getItem("token");
        const resp = await fetch("http://localhost:8000/api/feeds/templates", {
          headers: { "Authorization": f"Bearer {token}" }
        });
        if (resp.ok) {
          const data = await resp.json();
          setTemplates(data || []);
        }
      } catch (e) {
        console.error(e);
      }
    };
    fetchTemplates();
  }, []);

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const uploadedFile = e.target.files?.[0];
    if (!uploadedFile) return;

    setFile(uploadedFile);
    setError(null);
    setLoading(true);

    try {
      const token = localStorage.getItem("token");
      const formData = new FormData();
      formData.append("file", uploadedFile);

      const resp = await fetch("http://localhost:8000/api/feeds/upload", {
        method: "POST",
        headers: { "Authorization": f"Bearer {token}" },
        body: formData
      });

      if (!resp.ok) {
        const data = await resp.json();
        throw new Error(data.detail || "Failed to upload file.");
      }

      const data = await resp.json();
      setPreview(data);
      setMapping(data.suggested_mapping || {});
    } catch (err: any) {
      setError(err.message || "Failed to upload.");
      setFile(null);
    } finally {
      setLoading(false);
    }
  };

  const handleTemplateChange = (templateId: string) => {
    setSelectedTemplate(templateId);
    const tmpl = templates.find(t => t.id === templateId);
    if (tmpl) {
      setMapping(tmpl.column_mapping);
    }
  };

  const handleStartProcess = async () => {
    if (!file || !preview) return;
    setLoading(true);
    setError(null);

    // Validate required fields mapping
    if (!mapping.product_name || !mapping.brand) {
      setError("Product Name and Brand fields mapping are required before starting the import.");
      setLoading(false);
      return;
    }

    try {
      const token = localStorage.getItem("token");
      const payload = {
        filename: file.name,
        file_hash: preview.file_hash,
        column_mapping: mapping,
        save_template: saveTemplate,
        template_name: saveTemplate ? templateName : null,
        source_name: file.name
      };

      const resp = await fetch("http://localhost:8000/api/feeds/process", {
        method: "POST",
        headers: {
          "Authorization": f"Bearer {token}",
          "Content-Type": "application/json"
        },
        body: JSON.stringify(payload)
      });

      if (!resp.ok) {
        const data = await resp.json();
        throw new Error(data.detail || "Failed to process import job.");
      }

      const job = await resp.json();
      router.push(`/imports/${job.id}`);
    } catch (err: any) {
      setError(err.message || "Failed to start processor.");
      setLoading(false);
    }
  };

  return (
    <Shell>
      <div className={styles.pageHeader}>
        <div className={styles.titleGroup}>
          <h1>Feed Ingestion Wizard</h1>
          <p>Import beauty product catalog datasets (CSV, Excel, or JSON formats)</p>
        </div>
      </div>

      {error && (
        <div style={{ padding: 12, backgroundColor: 'rgba(239, 68, 68, 0.1)', border: '1px solid #ef4444', borderRadius: 4, color: '#ef4444', fontSize: 13, marginBottom: 20 }}>
          {error}
        </div>
      )}

      {!file ? (
        <div className={styles.panelCard} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: '64px 0', borderStyle: 'dashed' }}>
          <UploadCloud size={48} color="#64748b" style={{ marginBottom: 16 }} />
          <p style={{ fontWeight: 500, fontSize: 15, marginBottom: 4 }}>Drag and drop your feed file here</p>
          <p style={{ color: '#64748b', fontSize: 13, marginBottom: 20 }}>Supports UTF-8 CSV, JSON, or Excel files up to 50MB</p>
          <input 
            type="file" 
            id="file-upload" 
            style={{ display: 'none' }} 
            onChange={handleFileUpload}
            accept=".csv,.xlsx,.json"
          />
          <label htmlFor="file-upload" className={`${styles.btn} ${styles.btnPrimary}`} style={{ cursor: 'pointer' }}>
            {loading ? "Reading File Preview..." : "Browse Local Files"}
          </label>
        </div>
      ) : (
        preview && (
          <div className={styles.mappingGrid}>
            <div className={styles.mappingCard}>
              <h3 style={{ marginBottom: 16, fontSize: 15 }}>Configure Field Mapping</h3>
              
              <div className={styles.formGroup} style={{ marginBottom: 24 }}>
                <label>Load Saved Template</label>
                <select 
                  value={selectedTemplate} 
                  onChange={(e) => handleTemplateChange(e.target.value)}
                  className={styles.inputField}
                  style={{ backgroundColor: '#0b0f19' }}
                >
                  <option value="">-- Apply a saved header template --</option>
                  {templates.map(t => (
                    <option key={t.id} value={t.id}>{t.name}</option>
                  ))}
                </select>
              </div>

              {canonicalFields.map(field => (
                <div className={styles.formGroup} key={field.key}>
                  <label>{field.label}</label>
                  <select
                    value={mapping[field.key] || ''}
                    onChange={(e) => setMapping({ ...mapping, [field.key]: e.target.value })}
                    className={styles.inputField}
                    style={{ backgroundColor: '#0b0f19' }}
                  >
                    <option value="">-- Select raw spreadsheet column --</option>
                    {preview.headers.map((h: string) => (
                      <option key={h} value={h}>{h}</option>
                    ))}
                  </select>
                </div>
              ))}

              <div style={{ marginTop: 24, paddingTop: 16, borderTop: '1px solid #2e3c64' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
                  <input 
                    type="checkbox" 
                    id="save-template" 
                    checked={saveTemplate}
                    onChange={(e) => setSaveTemplate(e.target.checked)}
                  />
                  <label htmlFor="save-template" style={{ fontSize: 13, color: '#f8fafc', cursor: 'pointer' }}>Save this configuration as template</label>
                </div>

                {saveTemplate && (
                  <div className={styles.formGroup}>
                    <input 
                      type="text" 
                      placeholder="Template Name (e.g. Douglas Catalog FR)"
                      value={templateName}
                      onChange={(e) => setTemplateName(e.target.value)}
                      className={styles.inputField}
                      required
                    />
                  </div>
                )}
              </div>

              <button 
                onClick={handleStartProcess}
                className={`${styles.btn} ${styles.btnPrimary}`} 
                style={{ width: '100%', justifyContent: 'center', marginTop: 20 }}
                disabled={loading}
              >
                {loading ? "Initializing Queue Job..." : "Validate and Ingest Catalog"}
              </button>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
              <div className={styles.panelCard} style={{ margin: 0 }}>
                <h3 style={{ marginBottom: 12, fontSize: 15 }}>File Properties Summary</h3>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8, fontSize: 13, color: '#94a3b8' }}>
                  <div>Filename: <span style={{ color: '#f8fafc' }}>{preview.filename}</span></div>
                  <div>Format: <span style={{ color: '#f8fafc', textTransform: 'uppercase' }}>{preview.file_type}</span></div>
                  <div>Total Rows Detected: <span style={{ color: '#f8fafc' }}>{preview.total_rows} rows</span></div>
                </div>
              </div>

              <div className={styles.panelCard} style={{ flex: 1, margin: 0, overflow: 'hidden' }}>
                <h3 style={{ marginBottom: 12, fontSize: 15 }}>Raw File Row Previews</h3>
                <div className={styles.tableContainer} style={{ overflow: 'auto', maxHeight: '400px' }}>
                  <table className={styles.denseTable}>
                    <thead>
                      <tr>
                        {preview.headers.map((h: string) => (
                          <th key={h}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {preview.preview_rows.map((row: any, idx: number) => (
                        <tr key={idx}>
                          {preview.headers.map((h: string) => (
                            <td key={h}>{row[h]}</td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          </div>
        )
      )}
    </Shell>
  );
}
