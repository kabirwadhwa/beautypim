"use client";

import { useEffect, useState } from "react";
import Shell from "../../components/Shell";
import { API_URL } from "../../config";
import styles from "../page.module.css";

type Metrics = Record<string, number>;
type ImportJob = { id: string; source_name: string; filename: string; status: string; processed_rows: number; total_rows: number; imported_rows: number; failed_rows: number; conflicts_detected: number; created_at: string };
type Conflict = { id: string; field_name: string; conflict_type: string; values: unknown[]; status: string };
const auth = () => ({ Authorization: `Bearer ${localStorage.getItem("token")}` });

export default function KnowledgeCorpusPage() {
  const [metrics, setMetrics] = useState<Metrics>({});
  const [imports, setImports] = useState<ImportJob[]>([]);
  const [conflicts, setConflicts] = useState<Conflict[]>([]);
  const [error, setError] = useState("");
  const load = async () => {
    try {
      const [m, i, c] = await Promise.all([
        fetch(`${API_URL}/knowledge-corpus/metrics`, { headers: auth() }),
        fetch(`${API_URL}/knowledge-corpus/imports`, { headers: auth() }),
        fetch(`${API_URL}/knowledge-corpus/conflicts?limit=50`, { headers: auth() }),
      ]);
      if (!m.ok || !i.ok || !c.ok) throw new Error("Unable to load the internal corpus.");
      setMetrics(await m.json()); setImports(await i.json()); setConflicts(await c.json()); setError("");
    } catch (value) { setError(value instanceof Error ? value.message : "Unable to load the corpus."); }
  };
  useEffect(() => { load(); }, []);
  const review = async (id: string, decision: "accepted" | "dismissed") => {
    const response = await fetch(`${API_URL}/knowledge-corpus/conflicts/${id}/${decision}`, { method: "POST", headers: auth() });
    if (!response.ok) setError("Unable to review the conflict."); else load();
  };
  const labels: Record<string, string> = {
    raw_source_rows: "Source observations", normalized_product_identities: "Product identities",
    normalized_variants: "Variants", unique_normalized_eans: "Unique EANs", unique_brands: "Brands",
    formulations: "Formulations", market_observations: "Market observations", conflicts: "Open conflicts",
  };
  return <Shell>
    <div className={styles.pageHeader}><div className={styles.titleGroup}><h1>Knowledge Corpus</h1>
      <p>Internal evidence used by enrichment. These records never appear in the customer Product Grid.</p></div></div>
    {error && <div style={{ color: "#f87171", border: "1px solid #ef4444", padding: 12, marginBottom: 16 }}>{error}</div>}
    <div className={styles.metricsGrid}>{Object.entries(labels).map(([key, label]) => <div className={styles.metricCard} key={key}>
      <div className={styles.metricCardHeader}>{label}</div><div className={styles.metricValue}>{(metrics[key] || 0).toLocaleString()}</div>
    </div>)}</div>
    <section className={styles.mappingCard} style={{ marginTop: 20 }}><h3>Import history</h3><div style={{overflowX:"auto"}}>
      <table className={styles.dataTable}><thead><tr><th>Dataset</th><th>Status</th><th>Rows</th><th>Imported</th><th>Failed</th><th>Conflicts</th></tr></thead>
      <tbody>{imports.map(item => <tr key={item.id}><td>{item.source_name}<br/><small>{item.filename}</small></td><td>{item.status}</td>
        <td>{item.processed_rows.toLocaleString()} / {item.total_rows.toLocaleString()}</td><td>{item.imported_rows.toLocaleString()}</td>
        <td>{item.failed_rows.toLocaleString()}</td><td>{item.conflicts_detected.toLocaleString()}</td></tr>)}</tbody></table></div></section>
    <section className={styles.mappingCard} style={{ marginTop: 20 }}><h3>Evidence conflicts</h3>
      {conflicts.length === 0 ? <p>No open conflicts.</p> : conflicts.map(item => <div key={item.id} style={{borderBottom:"1px solid #334155", padding:"12px 0"}}>
        <strong>{item.field_name}</strong> · {item.conflict_type}<pre style={{whiteSpace:"pre-wrap", color:"#94a3b8"}}>{JSON.stringify(item.values, null, 2)}</pre>
        <button onClick={() => review(item.id, "accepted")}>Accept reviewed value</button>{" "}<button onClick={() => review(item.id, "dismissed")}>Dismiss</button>
      </div>)}</section>
  </Shell>;
}
