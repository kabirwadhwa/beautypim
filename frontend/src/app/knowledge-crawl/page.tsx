"use client";

import React, { FormEvent, useCallback, useEffect, useState } from "react";
import Shell from "../../components/Shell";
import { API_URL } from "../../config";
import styles from "../page.module.css";

type CrawlJob = {
  id: string; domain: string; crawl_mode: string; status: string;
  pages_discovered: number; pages_fetched: number; product_pages_found: number;
  products_persisted: number; products_failed: number; current_queue_size: number;
  error_summary?: string;
};

const auth = () => ({ Authorization: `Bearer ${localStorage.getItem("token")}` });

export default function KnowledgeCrawlPage() {
  const [jobs, setJobs] = useState<CrawlJob[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [products, setProducts] = useState<any[]>([]);
  const [conflicts, setConflicts] = useState<any[]>([]);
  const [domain, setDomain] = useState("");
  const [startUrls, setStartUrls] = useState("");
  const [sitemapUrl, setSitemapUrl] = useState("");
  const [mode, setMode] = useState("full_domain");
  const [maxPages, setMaxPages] = useState(500);
  const [maxProducts, setMaxProducts] = useState(250);
  const [allowed, setAllowed] = useState("");
  const [denied, setDenied] = useState("");
  const [browser, setBrowser] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await fetch(`${API_URL}/crawl-jobs`, { headers: auth() });
      if (!response.ok) throw new Error("Unable to load crawl history.");
      setJobs(await response.json());
    } catch (err: any) { setError(err.message); }
  }, []);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 4000);
    return () => window.clearInterval(timer);
  }, [load]);

  const inspect = async (id: string) => {
    setSelected(id);
    const [productResponse, conflictResponse] = await Promise.all([
      fetch(`${API_URL}/crawl-jobs/${id}/products`, { headers: auth() }),
      fetch(`${API_URL}/crawl-jobs/${id}/conflicts`, { headers: auth() }),
    ]);
    setProducts(productResponse.ok ? await productResponse.json() : []);
    setConflicts(conflictResponse.ok ? await conflictResponse.json() : []);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true); setError(null);
    const payload = {
      domain: domain.trim().replace(/^https?:\/\//, "").split("/")[0],
      starting_urls: startUrls.split(/\n|,/).map(v => v.trim()).filter(Boolean),
      sitemap_url: sitemapUrl.trim() || null, crawl_mode: mode,
      maximum_pages: maxPages, maximum_product_pages: maxProducts,
      allowed_url_patterns: allowed.split(/\n/).map(v => v.trim()).filter(Boolean),
      denied_url_patterns: denied.split(/\n/).map(v => v.trim()).filter(Boolean),
      use_browser_rendering: browser,
    };
    try {
      const response = await fetch(`${API_URL}/crawl-jobs`, {
        method: "POST", headers: { ...auth(), "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || "Unable to create crawl.");
      await load();
      setSelected(body.id);
    } catch (err: any) { setError(err.message); } finally { setBusy(false); }
  };

  const action = async (id: string, command: string) => {
    const response = await fetch(`${API_URL}/crawl-jobs/${id}/${command}`, { method: "POST", headers: auth() });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      setError(body.detail || `Unable to ${command} crawl.`);
    }
    await load();
  };

  const review = async (id: string, decision: "accept" | "reject") => {
    const response = await fetch(`${API_URL}/crawl-jobs/conflicts/${id}/${decision}`, { method: "POST", headers: auth() });
    if (!response.ok) setError("Unable to review conflict.");
    if (selected) await inspect(selected);
  };
  const reviewMatch = async (id: string, decision: "accept" | "reject") => {
    const response = await fetch(`${API_URL}/crawl-jobs/observations/${id}/match/${decision}`, { method: "POST", headers: auth() });
    if (!response.ok) setError("Unable to review possible product match.");
    if (selected) await inspect(selected);
  };

  const field = { width: "100%", background: "#0b1220", color: "#e2e8f0", border: "1px solid #334155", borderRadius: 6, padding: 10 };
  return <Shell>
    <div className={styles.pageHeader}><div className={styles.titleGroup}>
      <h1>Knowledge Crawl</h1><p>Acquire traceable product knowledge from approved retailer and brand websites.</p>
    </div></div>
    {error && <div style={{ color: "#f87171", border: "1px solid #ef4444", padding: 12, marginBottom: 16 }}>{error}</div>}
    <div style={{ display: "grid", gridTemplateColumns: "minmax(320px, 1fr) 2fr", gap: 20 }}>
      <form onSubmit={submit} className={styles.mappingCard}>
        <h3>New crawl</h3>
        <label>Approved domain<input style={field} value={domain} onChange={e => setDomain(e.target.value)} placeholder="catalogue.example" required /></label>
        <label>Starting URLs<textarea style={field} rows={3} value={startUrls} onChange={e => setStartUrls(e.target.value)} placeholder="One URL per line" /></label>
        <label>Sitemap URL<input style={field} value={sitemapUrl} onChange={e => setSitemapUrl(e.target.value)} /></label>
        <label>Mode<select style={field} value={mode} onChange={e => setMode(e.target.value)}>
          {["single_url","multiple_urls","category","brand_catalogue","sitemap","sitemap_index","full_domain"].map(value => <option key={value}>{value}</option>)}
        </select></label>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
          <label>Max pages<input style={field} type="number" value={maxPages} onChange={e => setMaxPages(+e.target.value)} /></label>
          <label>Max products<input style={field} type="number" value={maxProducts} onChange={e => setMaxProducts(+e.target.value)} /></label>
        </div>
        <label>Allowed URL patterns<textarea style={field} rows={2} value={allowed} onChange={e => setAllowed(e.target.value)} /></label>
        <label>Denied URL patterns<textarea style={field} rows={2} value={denied} onChange={e => setDenied(e.target.value)} /></label>
        <label><input type="checkbox" checked={browser} onChange={e => setBrowser(e.target.checked)} /> Use browser rendering when required</label>
        <button className={styles.primaryBtn} disabled={busy}>{busy ? "Creating…" : "Start crawl"}</button>
      </form>
      <section className={styles.mappingCard}>
        <h3>Crawl history</h3>
        <div style={{ overflowX: "auto" }}><table className={styles.dataTable}><thead><tr>
          <th>Domain</th><th>Status</th><th>Progress</th><th>Products</th><th>Actions</th>
        </tr></thead><tbody>{jobs.map(job => <tr key={job.id}>
          <td>{job.domain}<br/><small>{job.crawl_mode}</small></td><td>{job.status}{job.error_summary && <><br/><small style={{color:"#fca5a5"}}>{job.error_summary}</small></>}</td>
          <td>{job.pages_fetched}/{job.pages_discovered}<br/><small>{job.current_queue_size} queued</small></td>
          <td>{job.products_persisted}<br/><small>{job.products_failed} failed</small></td>
          <td><button onClick={() => inspect(job.id)}>View</button>{" "}
            {["queued","discovering","crawling","parsing"].includes(job.status) && <button onClick={() => action(job.id,"pause")}>Pause</button>}
            {["paused","partially_completed","failed","blocked"].includes(job.status) && <button onClick={() => action(job.id,"resume")}>Resume</button>}{" "}
            {job.status === "completed" && <button onClick={() => action(job.id,"recrawl")}>Recrawl</button>}{" "}
            {!["completed","cancelled"].includes(job.status) && <button onClick={() => action(job.id,"cancel")}>Cancel</button>}
          </td>
        </tr>)}</tbody></table></div>
      </section>
    </div>
    {selected && <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:20, marginTop:20 }}>
      <section className={styles.mappingCard}><h3>Recently discovered products</h3>{products.map(item =>
        <div key={item.id} style={{borderBottom:"1px solid #334155",padding:"10px 0"}}><strong>{item.product.product_name || "Unnamed"}</strong> — {item.product.brand || "Unknown brand"}<br/><small>{item.match_status} · {item.source_url}</small>
          {item.match_status === "possible_match" && <div><small>Candidate: {item.possible_match_product_id}</small><br/><button onClick={() => reviewMatch(item.id,"accept")}>Merge match</button>{" "}<button onClick={() => reviewMatch(item.id,"reject")}>Keep separate</button></div>}</div>)}</section>
      <section className={styles.mappingCard}><h3>Review conflicts</h3>{conflicts.filter(c => c.status === "pending").map(conflict =>
        <div key={conflict.id} style={{borderBottom:"1px solid #334155",padding:"10px 0"}}><strong>{conflict.field_name}</strong><br/><small>Current: {JSON.stringify(conflict.current_value)}<br/>Observed: {JSON.stringify(conflict.observed_value)}</small><br/>
          <button onClick={() => review(conflict.id,"accept")}>Accept</button>{" "}<button onClick={() => review(conflict.id,"reject")}>Reject</button></div>)}</section>
    </div>}
  </Shell>;
}
