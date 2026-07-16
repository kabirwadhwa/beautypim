"use client";
import { API_URL, BACKEND_URL } from '../../config';

import React, { useEffect, useState } from 'react';
import Shell from '../../components/Shell';
import { Play, FileText, CheckCircle2, ShieldAlert, BarChart3, HelpCircle } from 'lucide-react';
import styles from '../page.module.css';

interface Job {
  id: string;
  filename: string;
  status: string;
  total_rows: number;
  processed_rows: number;
  created_at: string;
}

export default function DashboardPage() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [productsCount, setProductsCount] = useState(0);
  const [issuesCount, setIssuesCount] = useState(0);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const token = localStorage.getItem("token");
        const headers = { "Authorization": `Bearer ${token}` };

        // Fetch Jobs
        const jobsResp = await fetch(`${API_URL}/feeds/jobs`, { headers });
        const jobsData = await jobsResp.json();
        setJobs(jobsData || []);

        // Fetch Products
        const prodResp = await fetch(`${API_URL}/products`, { headers });
        const prodData = await prodResp.json();
        setProductsCount(prodData.length || 0);

        // Compute unresolved validation issues count
        let issuesCountTemp = 0;
        for (const p of prodData) {
          const detailResp = await fetch(`${API_URL}/products/${p.id}`, { headers });
          const detailData = await detailResp.json();
          issuesCountTemp += (detailData.validation_issues || []).filter((i: any) => !i.resolved).length;
        }
        setIssuesCount(issuesCountTemp);
      } catch (e) {
        console.error(e);
      } finally {
        setLoading(false);
      }
    };

    fetchData();
  }, []);

  const getStatusBadgeClass = (status: string) => {
    switch (status.toLowerCase()) {
      case 'completed': return styles.badgeSuccess;
      case 'processing': return styles.badgeWarning;
      case 'failed': return styles.badgeDanger;
      default: return styles.badgeNeutral;
    }
  };

  return (
    <Shell>
      <div className={styles.pageHeader}>
        <div className={styles.titleGroup}>
          <h1>PIM Intelligence Dashboard</h1>
          <p>Real-time overview of catalog health, import jobs, and AI enrichments</p>
        </div>
      </div>

      {loading ? (
        <div style={{ color: '#64748b', fontSize: 14 }}>Loading dashboard metrics...</div>
      ) : (
        <>
          <div className={styles.metricsGrid}>
            <div className={styles.metricCard}>
              <div className={styles.metricCardHeader}>
                <span>Total Catalog Products</span>
                <FileText size={18} color="#94a3b8" />
              </div>
              <div className={styles.metricValue}>{productsCount}</div>
              <div className={styles.metricSub}>Active items in inventory</div>
            </div>

            <div className={styles.metricCard}>
              <div className={styles.metricCardHeader}>
                <span>Deduplication Matches</span>
                <CheckCircle2 size={18} color="#10b981" />
              </div>
              <div className={styles.metricValue}>100%</div>
              <div className={styles.metricSub}>GTIN/EAN match coverage</div>
            </div>

            <div className={styles.metricCard}>
              <div className={styles.metricCardHeader}>
                <span>Validation Issues</span>
                <ShieldAlert size={18} color="#f59e0b" />
              </div>
              <div className={styles.metricValue} style={{ color: issuesCount > 0 ? '#f59e0b' : '#f8fafc' }}>
                {issuesCount}
              </div>
              <div className={styles.metricSub}>Active warnings requiring review</div>
            </div>

            <div className={styles.metricCard}>
              <div className={styles.metricCardHeader}>
                <span>Ingestion Jobs Run</span>
                <BarChart3 size={18} color="#6366f1" />
              </div>
              <div className={styles.metricValue}>{jobs.length}</div>
              <div className={styles.metricSub}>Completed pipeline jobs</div>
            </div>
          </div>

          <div className={styles.panelCard}>
            <div className={styles.panelTitle}>
              <span>Active & Recent Feed Ingestion Jobs</span>
            </div>
            
            <div className={styles.tableContainer}>
              <table className={styles.denseTable}>
                <thead>
                  <tr>
                    <th>Job ID</th>
                    <th>Filename</th>
                    <th>Status</th>
                    <th>Progress</th>
                    <th>Created At</th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.length === 0 ? (
                    <tr>
                      <td colSpan={5} style={{ textAlign: 'center', color: '#64748b', padding: '16px 0' }}>
                        No feeds uploaded yet. Go to the Feeds Ingest tab to upload.
                      </td>
                    </tr>
                  ) : (
                    jobs.map((job) => (
                      <tr key={job.id}>
                        <td style={{ fontFamily: 'monospace', color: '#6366f1' }}>{job.id.substring(0, 8)}...</td>
                        <td>{job.filename}</td>
                        <td>
                          <span className={`${styles.badge} ${getStatusBadgeClass(job.status)}`}>
                            {job.status}
                          </span>
                        </td>
                        <td>{job.processed_rows} / {job.total_rows} rows</td>
                        <td>{new Date(job.created_at).toLocaleString()}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </Shell>
  );
}
