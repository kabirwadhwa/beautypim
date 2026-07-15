"use client";

import React, { useEffect, useState } from 'react';
import { useRouter, usePathname } from 'next/navigation';
import Link from 'next/link';
import { 
  LayoutDashboard, FileInput, TableProperties, Download, 
  Settings, LogOut, ShieldAlert, Sparkles, Users 
} from 'lucide-react';
import styles from '../app/page.module.css';

interface ShellProps {
  children: React.ReactNode;
}

export default function Shell({ children }: ShellProps) {
  const router = useRouter();
  const pathname = usePathname();
  const [role, setRole] = useState<string | null>(null);
  const [email, setEmail] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const token = localStorage.getItem("token");
    const storedRole = localStorage.getItem("role");
    const storedEmail = localStorage.getItem("email");

    if (!token) {
      router.push("/login");
    } else {
      setRole(storedRole);
      setEmail(storedEmail);
      setLoading(false);
    }
  }, [router]);

  const handleLogout = () => {
    localStorage.clear();
    router.push("/login");
  };

  if (loading) {
    return <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh', color: '#64748b' }}>Authenticating session...</div>;
  }

  const navItems = [
    { name: 'Overview', path: '/dashboard', icon: LayoutDashboard },
    { name: 'Feeds Ingest', path: '/imports', icon: FileInput },
    { name: 'Product Grid', path: '/products', icon: TableProperties },
    { name: 'Export Center', path: '/exports', icon: Download },
    { name: 'Taxonomy Settings', path: '/settings/taxonomies', icon: Settings },
  ];

  if (role === 'admin') {
    navItems.push({ name: 'Team & Access', path: '/settings/team', icon: Users });
  }

  return (
    <div className={styles.shell}>
      <aside className={styles.sidebar}>
        <div className={styles.logo}>
          <Sparkles size={24} color="#6366f1" />
          <span>Beauty PIM</span>
        </div>
        <nav className={styles.nav}>
          {navItems.map((item) => {
            const Icon = item.icon;
            const isActive = pathname.startsWith(item.path);
            return (
              <Link key={item.path} href={item.path} className={isActive ? `${styles.navLink} ${styles.navLinkActive}` : styles.navLink}>
                <Icon size={18} />
                <span>{item.name}</span>
              </Link>
            );
          })}
        </nav>
        <div className={styles.sidebarFooter}>
          <div style={{ marginBottom: 12, fontSize: 12 }}>
            <div style={{ fontWeight: 600, color: '#f8fafc' }}>{email || 'User'}</div>
            <div style={{ color: '#64748b', textTransform: 'capitalize' }}>Role: {role || 'viewer'}</div>
          </div>
          <div className={styles.navLink} onClick={handleLogout} style={{ cursor: 'pointer', borderTop: '1px solid #2e3c64', paddingTop: 12 }}>
            <LogOut size={18} />
            <span>Sign Out</span>
          </div>
        </div>
      </aside>
      <main className={styles.mainContent}>
        {children}
      </main>
    </div>
  );
}
