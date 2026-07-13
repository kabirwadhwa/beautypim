"use client";

import React, { useState } from 'react';
import { useRouter } from 'next/navigation';
import { Sparkles, Lock, Mail } from 'lucide-react';
import styles from '../page.module.css';

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);

    try {
      // Build form body as required by OAuth2PasswordRequestForm
      const formData = new URLSearchParams();
      formData.append("username", email);
      formData.append("password", password);

      const resp = await fetch("http://localhost:8000/api/auth/token", {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded"
        },
        body: formData
      });

      if (!resp.ok) {
        // If login fails, check if we need to register first (bootstrap path)
        if (resp.status === 401) {
          // Attempt automatic bootstrap registration if no users exist
          const regResp = await fetch("http://localhost:8000/api/auth/register", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ email, password })
          });

          if (regResp.ok) {
            // Re-login after bootstrap
            const retryResp = await fetch("http://localhost:8000/api/auth/token", {
              method: "POST",
              headers: { "Content-Type": "application/x-www-form-urlencoded" },
              body: formData
            });
            if (retryResp.ok) {
              const retryData = await retryResp.json();
              localStorage.setItem("token", retryData.access_token);
              localStorage.setItem("role", retryData.role);
              localStorage.setItem("email", email);
              router.push("/dashboard");
              return;
            }
          }
        }
        throw new Error("Invalid credentials or registration error.");
      }

      const data = await resp.json();
      localStorage.setItem("token", data.access_token);
      localStorage.setItem("role", data.role);
      localStorage.setItem("email", email);
      router.push("/dashboard");
    } catch (err: any) {
      setError(err.message || "Failed to authenticate.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className={styles.loginContainer}>
      <div className={styles.loginCard}>
        <div style={{ textAlign: 'center', marginBottom: 32 }}>
          <div style={{ display: 'inline-flex', padding: 12, borderRadius: '50%', backgroundColor: 'rgba(99, 102, 241, 0.1)', marginBottom: 16 }}>
            <Sparkles size={32} color="#6366f1" />
          </div>
          <h2>Beauty PIM Hub</h2>
          <p style={{ color: '#94a3b8', fontSize: 13, marginTop: 4 }}>Enter credentials to access catalog tools</p>
        </div>

        {error && (
          <div style={{ padding: 12, backgroundColor: 'rgba(239, 68, 68, 0.1)', border: '1px solid #ef4444', borderRadius: 4, color: '#ef4444', fontSize: 13, marginBottom: 20 }}>
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit}>
          <div className={styles.formGroup}>
            <label>Work Email Address</label>
            <input 
              type="email" 
              className={styles.inputField} 
              placeholder="name@beautybrand.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </div>

          <div className={styles.formGroup}>
            <label>Password</label>
            <input 
              type="password" 
              className={styles.inputField} 
              placeholder="••••••••"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>

          <button 
            type="submit" 
            className={styles.btn} 
            style={{ width: '100%', justifyContent: 'center', backgroundColor: '#6366f1', color: 'white', marginTop: 12 }}
            disabled={loading}
          >
            {loading ? "Signing In..." : "Access PIM Center"}
          </button>
        </form>

        <div style={{ marginTop: 24, paddingTop: 16, borderTop: '1px solid #2e3c64', fontSize: 11, color: '#64748b', textAlign: 'center' }}>
          * For first-time setup: entering an email and password will automatically register your account as the initial administrator.
        </div>
      </div>
    </div>
  );
}
