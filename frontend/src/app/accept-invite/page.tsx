"use client";

import React, { useEffect, useState, Suspense } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { ShieldCheck, Lock, AlertTriangle, CheckCircle } from 'lucide-react';
import styles from '../page.module.css';
import { API_URL } from '../../config';

function AcceptInviteContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const token = searchParams.get('token');

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [email, setEmail] = useState<string>('');
  const [role, setRole] = useState<string>('');
  
  const [password, setPassword] = useState('');
  const [passwordConfirm, setPasswordConfirm] = useState('');
  const [submitLoading, setSubmitLoading] = useState(false);
  const [success, setSuccess] = useState(false);

  useEffect(() => {
    if (!token) {
      setError("No invitation token provided.");
      setLoading(false);
      return;
    }

    const validateToken = async () => {
      try {
        const resp = await fetch(`${API_URL}/auth/invitations/validate`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json"
          },
          body: JSON.stringify({ token })
        });

        if (!resp.ok) {
          throw new Error("Invalid or expired invitation link");
        }

        const data = await resp.json();
        setEmail(data.email);
        setRole(data.role);
      } catch (err: any) {
        setError(err.message || "Invalid or expired invitation link. This link may have been revoked, expired, or already accepted.");
      } finally {
        setLoading(false);
      }
    };

    validateToken();
  }, [token]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (password !== passwordConfirm) {
      setError("Passwords do not match.");
      return;
    }

    if (password.length < 12) {
      setError("Password must be at least 12 characters long.");
      return;
    }

    setSubmitLoading(true);

    try {
      const resp = await fetch(`${API_URL}/auth/invitations/accept`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          token,
          password,
          password_confirm: passwordConfirm
        })
      });

      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}));
        throw new Error(errData.detail || "Invalid or expired invitation link");
      }

      setSuccess(true);
      setTimeout(() => {
        router.push("/login");
      }, 3000);
    } catch (err: any) {
      setError(err.message || "Failed to accept invitation.");
    } finally {
      setSubmitLoading(false);
    }
  };

  if (loading) {
    return (
      <div className={styles.loginContainer}>
        <div className={styles.loginCard} style={{ textAlign: 'center' }}>
          <p style={{ color: '#94a3b8' }}>Verifying your secure invitation link...</p>
        </div>
      </div>
    );
  }

  if (error && !email) {
    return (
      <div className={styles.loginContainer}>
        <div className={styles.loginCard} style={{ textAlign: 'center' }}>
          <div style={{ display: 'inline-flex', padding: 12, borderRadius: '50%', backgroundColor: 'rgba(239, 68, 68, 0.1)', marginBottom: 16 }}>
            <AlertTriangle size={32} color="#ef4444" />
          </div>
          <h2 style={{ color: '#f8fafc' }}>Invitation Error</h2>
          <p style={{ color: '#94a3b8', fontSize: 13, marginTop: 12, lineHeight: 1.6 }}>{error}</p>
          <button 
            className={`${styles.btn} ${styles.btnPrimary}`} 
            style={{ marginTop: 24, width: '100%', justifyContent: 'center' }}
            onClick={() => router.push("/login")}
          >
            Go to Login
          </button>
        </div>
      </div>
    );
  }

  if (success) {
    return (
      <div className={styles.loginContainer}>
        <div className={styles.loginCard} style={{ textAlign: 'center' }}>
          <div style={{ display: 'inline-flex', padding: 12, borderRadius: '50%', backgroundColor: 'rgba(16, 185, 129, 0.1)', marginBottom: 16 }}>
            <CheckCircle size={32} color="#10b981" />
          </div>
          <h2 style={{ color: '#f8fafc' }}>Account Created!</h2>
          <p style={{ color: '#94a3b8', fontSize: 13, marginTop: 8 }}>Your account has been set up successfully. Redirecting you to login...</p>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.loginContainer}>
      <div className={styles.loginCard}>
        <div style={{ textAlign: 'center', marginBottom: 24 }}>
          <div style={{ display: 'inline-flex', padding: 12, borderRadius: '50%', backgroundColor: 'rgba(99, 102, 241, 0.1)', marginBottom: 16 }}>
            <ShieldCheck size={32} color="#6366f1" />
          </div>
          <h2>Join Beauty PIM</h2>
          <p style={{ color: '#94a3b8', fontSize: 13, marginTop: 4 }}>
            Setting up account for <strong>{email}</strong>
          </p>
          <span style={{ 
            display: 'inline-block', 
            marginTop: 8, 
            padding: '2px 8px', 
            borderRadius: 4, 
            fontSize: 11, 
            fontWeight: 600, 
            backgroundColor: '#1e1b4b', 
            color: '#c7d2fe',
            textTransform: 'uppercase' 
          }}>
            Invited Role: {role}
          </span>
        </div>

        {error && (
          <div style={{ padding: 12, backgroundColor: 'rgba(239, 68, 68, 0.1)', border: '1px solid #ef4444', borderRadius: 4, color: '#ef4444', fontSize: 13, marginBottom: 20 }}>
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit}>
          <div className={styles.formGroup}>
            <label>Choose Password</label>
            <div style={{ position: 'relative' }}>
              <input 
                type="password" 
                className={styles.inputField} 
                placeholder="Minimum 12 characters"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                style={{ paddingLeft: 38 }}
              />
              <Lock size={16} color="#64748b" style={{ position: 'absolute', left: 12, top: 12 }} />
            </div>
          </div>

          <div className={styles.formGroup}>
            <label>Confirm Password</label>
            <div style={{ position: 'relative' }}>
              <input 
                type="password" 
                className={styles.inputField} 
                placeholder="Confirm password"
                value={passwordConfirm}
                onChange={(e) => setPasswordConfirm(e.target.value)}
                required
                style={{ paddingLeft: 38 }}
              />
              <Lock size={16} color="#64748b" style={{ position: 'absolute', left: 12, top: 12 }} />
            </div>
          </div>

          <button 
            type="submit" 
            className={`${styles.btn} ${styles.btnPrimary}`} 
            style={{ width: '100%', justifyContent: 'center', marginTop: 8 }}
            disabled={submitLoading}
          >
            {submitLoading ? "Setting up account..." : "Accept Invitation & Join"}
          </button>
        </form>
      </div>
    </div>
  );
}

export default function AcceptInvitePage() {
  return (
    <Suspense fallback={
      <div className={styles.loginContainer}>
        <div className={styles.loginCard} style={{ textAlign: 'center' }}>
          <p style={{ color: '#94a3b8' }}>Loading invitation details...</p>
        </div>
      </div>
    }>
      <AcceptInviteContent />
    </Suspense>
  );
}
