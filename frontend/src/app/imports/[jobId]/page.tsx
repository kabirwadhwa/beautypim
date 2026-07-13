"use client";

import React, { useEffect, useState, useRef } from 'react';
import { useParams, useRouter } from 'next/navigation';
import Shell from '../../../components/Shell';
import { Loader2, CheckCircle2, XCircle, AlertTriangle, ArrowRight } from 'lucide-react';
import styles from '../../page.module.css';

interface Job {
  id: string;
  filename: string;
  status: string;
  total_rows: number;
  processed_rows: number;
  error_message: string | null;
  created_at: string;
}

interface JobItem {
  id: string;
  source_row_number: int;
  status: string;
  match_status: string;
  duplicate_score: number;
  enrichment_status: string;
  failure_message: string | null;
}

export default function JobProgressPage() {
  const params = useParams();
  const router = useRouter();
  const jobId = params.jobId as string;
  
  const [job, setJob] = useState<Job | null>(null);
  const [items, setItems] = useState<JobItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<NodeJS.Timeout | null>(null);

  const fetchJobStatus = async () => {
    try {
      const token = localStorage.getItem("token");
      const headers = { "Authorization": f"Bearer {token}" };

      const jobResp = await fetch(`http://localhost:8000/api/feeds/jobs/${jobId}`, { headers });
      if (!jobResp.ok) throw new Error("Failed to load job status.");
      const jobData = await jobResp.json();
      setJob(jobData);

      const itemsResp = await fetch(`http://localhost:8000/api/feeds/jobs/${jobId}/items`, { headers });
      if (itemsResp.ok) {
        const itemsData = await itemsResp.json();
        setItems(itemsData || []);
      }

      // Check if job completed or failed to stop polling
      if (["completed", "failed", "cancelled"].includes(jobData.status.toLowerCase())) {
        if (timerRef.current) clearInterval(timerRef.current);
      }
    } catch (e: any) {
      setError(e.message || "Failed to fetch data.");
      if (timerRef.current) clearInterval(timerRef.current);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchJobStatus();
    timerRef.current = setInterval(fetchJobStatus, 2000);

    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [jobId]);

  const handleCancelJob = async () => {
    try {
      const token = localStorage.getItem("token");
      await fetch(`http://localhost:8000/api/feeds/jobs/${jobId}/cancel`, {
        method: "POST",
        headers: { "Authorization": f"Bearer {token}" }
      });
      fetchJobStatus();
    } catch (e) {
      console.error(e);
    }
  };

  if (loading && !job) {
    return (
      <Shell>
        <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '50vh', color: '#64748b' }}>
          <Loader2 className="animate-spin" size={24} style={{ marginRight: 12 }} />
          <span>Fetching Ingestion Progress Metrics...</span>
        </div>
      </Shell>
    );
  }

  const processedCount = job?.processed_rows || 0;
  const totalCount = job?.total_rows || 1;
  const pct = Math.min(100, Math.round((processedCount / totalCount) * 100));

  const failedItems = items.filter(i => i.status === "failed");
  const matchingReviews = items.filter(i => i.status === "awaiting_match_review");

  return (
    <Shell>
      <div className={styles.pageHeader}>
        <div className={styles.titleGroup}>
          <h1>Job Processing Panel</h1>
          <p>Task ID: <span style={{ fontFamily: 'monospace', color: '#6366f1' }}>{jobId}</span> | File: <span style={{ color: '#f8fafc' }}>{job?.filename}</span></p>
        </div>
        
        {job && ["pending", "processing"].includes(job.status.toLowerCase()) && (
          <button onClick={handleCancelJob} className={`${styles.btn} ${styles.btnSecondary}`} style={{ color: '#ef4444' }}>
            Cancel Queue Ingest
          </button>
        )}
      </div>

      <div className={styles.panelCard}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <span style={{ fontWeight: 600 }}>Pipeline Progress Status: {job?.status.toUpperCase()}</span>
          <span>{pct}% ({processedCount} / {totalCount} rows)</span>
        </div>
        
        <div style={{ width: '100%', height: 8, backgroundColor: '#1e294b', borderRadius: 4, overflow: 'hidden', marginBottom: 16 }}>
          <div style={{ width: `${pct}%`, height: '100%', backgroundColor: '#6366f1', transition: 'width 0.4s ease' }} />
        </div>

        {job?.error_message && (
          <div style={{ padding: 12, backgroundColor: 'rgba(239, 68, 68, 0.1)', border: '1px solid #ef4444', borderRadius: 4, color: '#ef4444', fontSize: 13 }}>
            Job Ingest Error: {job.error_message}
          </div>
        )}
      </div>

      <div className={styles.panelCard}>
        <div className={styles.panelTitle}>
          <span>Row Ingestion Execution Logs ({items.length} records)</span>
        </div>

        <div className={styles.tableContainer}>
          <table className={styles.denseTable}>
            <thead>
              <tr>
                <th>Row #</th>
                <th>Status</th>
                <th>Matching Outcome</th>
                <th>Match Similarity</th>
                <th>Enrichment Status</th>
                <th>Error Details</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  <td>{item.source_row_number}</td>
                  <td>
                    <span className={`${styles.badge} ${
                      item.status === 'completed' ? styles.badgeSuccess : 
                      item.status === 'processing' || item.status === 'enriching' ? styles.badgeWarning :
                      item.status === 'awaiting_match_review' ? styles.badgeWarning :
                      styles.badgeDanger
                    }`}>
                      {item.status}
                    </span>
                  </td>
                  <td style={{ textTransform: 'capitalize' }}>
                    {item.match_status.replace("_", " ")}
                  </td>
                  <td>
                    {item.match_status !== 'skipped' ? (
                      <span style={{ fontWeight: 600, color: item.duplicate_score > 0.85 ? '#10b981' : '#f8fafc' }}>
                        {Math.round(item.duplicate_score * 100)}%
                      </span>
                    ) : "-"}
                  </td>
                  <td>
                    <span className={`${styles.badge} ${
                      item.enrichment_status === 'succeeded' ? styles.badgeSuccess :
                      item.enrichment_status === 'not_requested' ? styles.badgeNeutral :
                      styles.badgeDanger
                    }`}>
                      {item.enrichment_status}
                    </span>
                  </td>
                  <td style={{ color: '#ef4444', maxWidth: '300px', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {item.failure_message || "-"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      
      {job && ["completed", "failed", "cancelled"].includes(job.status.toLowerCase()) && (
        <button 
          onClick={() => router.push("/products")} 
          className={`${styles.btn} ${styles.btnPrimary}`} 
          style={{ width: '100%', justifyContent: 'center' }}
        >
          Proceed to Product review grid <ArrowRight size={18} style={{ marginLeft: 8 }} />
        </button>
      )}
    </Shell>
  );
}
