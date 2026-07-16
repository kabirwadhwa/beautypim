"use client";

import React, { useEffect, useState } from 'react';
import Shell from '../../../components/Shell';
import styles from '../../page.module.css';
import { API_URL } from '../../../config';
import { 
  UserPlus, Shield, UserX, RefreshCw, X, 
  CheckCircle2, Search, Info, AlertTriangle, AlertCircle
} from 'lucide-react';

interface TeamUser {
  id: string;
  email: string;
  role: string;
  is_active: boolean;
  last_login_at: string | null;
  accepted_invitation_at: string | null;
  disabled_at: string | null;
  created_at: string;
  invited_by: string | null;
}

interface TeamInvitation {
  id: string;
  email: string;
  role: string;
  status: string;
  expires_at: string;
  last_sent_at: string;
  resend_count: number;
  email_delivery_status: string | null;
  email_delivery_error: string | null;
  created_at: string;
  invited_by: string | null;
}

export default function TeamAccessPage() {
  const [isAdmin, setIsAdmin] = useState(false);
  const [loading, setLoading] = useState(true);
  
  // Lists
  const [users, setUsers] = useState<TeamUser[]>([]);
  const [invitations, setInvitations] = useState<TeamInvitation[]>([]);
  
  // Filters & Search
  const [search, setSearch] = useState('');
  const [roleFilter, setRoleFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [userPage, setUserPage] = useState(1);
  const [totalUsers, setTotalUsers] = useState(0);
  
  // Invite Modal
  const [inviteModalOpen, setInviteModalOpen] = useState(false);
  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteRole, setInviteRole] = useState('viewer');
  const [invitePassword, setInvitePassword] = useState('');
  const [inviteLoading, setInviteLoading] = useState(false);
  const [inviteError, setInviteError] = useState<string | null>(null);
  
  // Confirmation Modal
  const [confirmModal, setConfirmModal] = useState<{
    open: boolean;
    title: string;
    message: string;
    action: () => void;
    danger: boolean;
  }>({
    open: false,
    title: '',
    message: '',
    action: () => {},
    danger: false
  });
  
  const [feedbackMsg, setFeedbackMsg] = useState<{ text: string; error: boolean } | null>(null);

  const fetchUsers = async () => {
    try {
      const token = localStorage.getItem("token");
      let url = `${API_URL}/admin/users?page=${userPage}&limit=10`;
      if (search) url += `&search=${encodeURIComponent(search)}`;
      if (roleFilter) url += `&role=${encodeURIComponent(roleFilter)}`;
      if (statusFilter) url += `&status_filter=${encodeURIComponent(statusFilter)}`;
      
      const resp = await fetch(url, {
        headers: { "Authorization": `Bearer ${token}` }
      });
      if (resp.status === 403) {
        setIsAdmin(false);
        setLoading(false);
        return;
      }
      if (!resp.ok) throw new Error("Failed to load users");
      const data = await resp.json();
      setUsers(data.users);
      setTotalUsers(data.total);
      setIsAdmin(true);
    } catch (err) {
      console.error(err);
    }
  };

  const fetchInvitations = async () => {
    try {
      const token = localStorage.getItem("token");
      const resp = await fetch(`${API_URL}/admin/invitations?page=1&limit=20`, {
        headers: { "Authorization": `Bearer ${token}` }
      });
      if (resp.ok) {
        const data = await resp.json();
        setInvitations(data.invitations);
      }
    } catch (err) {
      console.error(err);
    }
  };

  useEffect(() => {
    const token = localStorage.getItem("token");
    const role = localStorage.getItem("role");
    if (role !== 'admin') {
      setIsAdmin(false);
      setLoading(false);
      return;
    }
    
    const init = async () => {
      setLoading(true);
      await fetchUsers();
      await fetchInvitations();
      setLoading(false);
    };
    init();
  }, [userPage, search, roleFilter, statusFilter]);

  const triggerFeedback = (text: string, isError = false) => {
    setFeedbackMsg({ text, error: isError });
    setTimeout(() => {
      setFeedbackMsg(null);
    }, 4000);
  };

  const handleAddUser = async (e: React.FormEvent) => {
    e.preventDefault();
    setInviteError(null);
    setInviteLoading(true);

    try {
      const token = localStorage.getItem("token");
      const resp = await fetch(`${API_URL}/admin/users`, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ email: inviteEmail, role: inviteRole, password: invitePassword })
      });

      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail || "Failed to create user");
      }

      triggerFeedback("User account created and activated successfully!");
      setInviteEmail('');
      setInvitePassword('');
      setInviteModalOpen(false);
      fetchUsers();
      fetchInvitations();
    } catch (err: any) {
      setInviteError(err.message || "Failed to create user.");
    } finally {
      setInviteLoading(false);
    }
  };

  const handleResend = (invite: TeamInvitation) => {
    setConfirmModal({
      open: true,
      title: "Resend Invitation",
      message: `Are you sure you want to resend the invitation to ${invite.email}? The previous invitation link will stop working immediately.`,
      danger: false,
      action: async () => {
        try {
          const token = localStorage.getItem("token");
          const resp = await fetch(`${API_URL}/admin/invitations/${invite.id}/resend`, {
            method: "POST",
            headers: { "Authorization": `Bearer ${token}` }
          });
          if (!resp.ok) throw new Error("Failed to resend");
          triggerFeedback("Invitation link resent!");
          fetchInvitations();
        } catch (err) {
          triggerFeedback("Failed to resend invitation", true);
        }
        setConfirmModal(prev => ({ ...prev, open: false }));
      }
    });
  };

  const handleRevoke = (invite: TeamInvitation) => {
    setConfirmModal({
      open: true,
      title: "Revoke Invitation",
      message: `Are you sure you want to revoke the invitation for ${invite.email}? They will no longer be able to set up their account.`,
      danger: true,
      action: async () => {
        try {
          const token = localStorage.getItem("token");
          const resp = await fetch(`${API_URL}/admin/invitations/${invite.id}/revoke`, {
            method: "POST",
            headers: { "Authorization": `Bearer ${token}` }
          });
          if (!resp.ok) throw new Error("Failed to revoke");
          triggerFeedback("Invitation revoked!");
          fetchInvitations();
        } catch (err) {
          triggerFeedback("Failed to revoke invitation", true);
        }
        setConfirmModal(prev => ({ ...prev, open: false }));
      }
    });
  };

  const handleRoleChange = (user: TeamUser, newRole: string) => {
    const isGrantingAdmin = newRole === "admin";
    const isRemovingAdmin = user.role === "admin" && newRole !== "admin";
    
    if (!isGrantingAdmin && !isRemovingAdmin) {
      // Viewer <-> Editor doesn't need confirmation
      executeRoleChange(user.id, newRole);
      return;
    }

    setConfirmModal({
      open: true,
      title: isGrantingAdmin ? "Grant Admin Access" : "Revoke Admin Access",
      message: isGrantingAdmin
        ? `Are you sure you want to grant Administrator access to ${user.email}? Admins have full access to team management, system settings, and exports.`
        : `Are you sure you want to revoke Administrator access from ${user.email}? They will lose admin-only page views.`,
      danger: isRemovingAdmin,
      action: () => {
        executeRoleChange(user.id, newRole);
        setConfirmModal(prev => ({ ...prev, open: false }));
      }
    });
  };

  const executeRoleChange = async (userId: string, newRole: string) => {
    try {
      const token = localStorage.getItem("token");
      const resp = await fetch(`${API_URL}/admin/users/${userId}/role`, {
        method: "PATCH",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ role: newRole })
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail || "Failed to update role");
      }
      triggerFeedback("User role updated!");
      fetchUsers();
    } catch (err: any) {
      triggerFeedback(err.message || "Failed to change user role", true);
    }
  };

  const handleDisableToggle = (user: TeamUser) => {
    const actionName = user.is_active ? "disable" : "enable";
    const title = user.is_active ? "Disable User Access" : "Enable User Access";
    const message = user.is_active
      ? `Are you sure you want to disable ${user.email}? They will be logged out and blocked from future logins immediately.`
      : `Are you sure you want to reactivate access for ${user.email}? They will be able to log in again.`;

    setConfirmModal({
      open: true,
      title,
      message,
      danger: user.is_active,
      action: async () => {
        try {
          const token = localStorage.getItem("token");
          const resp = await fetch(`${API_URL}/admin/users/${user.id}/${actionName}`, {
            method: "POST",
            headers: { "Authorization": `Bearer ${token}` }
          });
          if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            throw new Error(data.detail || `Failed to ${actionName} user`);
          }
          triggerFeedback(`User access ${actionName}d successfully!`);
          fetchUsers();
        } catch (err: any) {
          triggerFeedback(err.message || `Failed to modify user access`, true);
        }
        setConfirmModal(prev => ({ ...prev, open: false }));
      }
    });
  };

  if (loading) {
    return (
      <Shell>
        <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '60vh', color: '#94a3b8' }}>
          Loading Team settings...
        </div>
      </Shell>
    );
  }

  if (!isAdmin) {
    return (
      <Shell>
        <div className={styles.loginCard} style={{ margin: '80px auto', maxWidth: 500, textAlign: 'center' }}>
          <div style={{ display: 'inline-flex', padding: 12, borderRadius: '50%', backgroundColor: 'rgba(239, 68, 68, 0.1)', marginBottom: 16 }}>
            <AlertCircle size={32} color="#ef4444" />
          </div>
          <h2 style={{ color: '#f8fafc' }}>Access Denied</h2>
          <p style={{ color: '#94a3b8', fontSize: 13, marginTop: 12, lineHeight: 1.6 }}>
            Only administrators are authorized to access the Team & Access settings panel.
          </p>
        </div>
      </Shell>
    );
  }

  return (
    <Shell>
      <div className={styles.pageHeader}>
        <div className={styles.titleGroup}>
          <h1>Team & Access Management</h1>
          <p>Add organization members directly, modify permission levels, and manage login access controls.</p>
        </div>
        <button 
          className={`${styles.btn} ${styles.btnPrimary}`}
          onClick={() => setInviteModalOpen(true)}
        >
          <UserPlus size={18} /> Add User Directly
        </button>
      </div>

      {feedbackMsg && (
        <div style={{ 
          padding: 12, 
          backgroundColor: feedbackMsg.error ? 'rgba(239, 68, 68, 0.1)' : 'rgba(16, 185, 129, 0.1)', 
          border: feedbackMsg.error ? '1px solid #ef4444' : '1px solid #10b981', 
          borderRadius: 4, 
          color: feedbackMsg.error ? '#ef4444' : '#10b981', 
          fontSize: 13, 
          marginBottom: 20 
        }}>
          {feedbackMsg.text}
        </div>
      )}

      {/* Roles helper info */}
      <div style={{ 
        display: 'flex', 
        alignItems: 'center', 
        gap: 12, 
        padding: 12, 
        backgroundColor: 'rgba(99, 102, 241, 0.05)', 
        border: '1px solid #2e3c64', 
        borderRadius: 6, 
        color: '#94a3b8', 
        fontSize: 12, 
        marginBottom: 24 
      }}>
        <Info size={20} color="#6366f1" style={{ flexShrink: 0 }} />
        <div>
          <strong>Role Permissions Check:</strong> 
          <span style={{ marginLeft: 8 }}>
            <strong>Admin</strong>: Full controls, settings, and team access. | 
            <strong>Editor</strong>: Full imports, edits, and overrides; cannot manage team. | 
            <strong>Viewer</strong>: Read-only access to catalog; cannot override or edit.
          </span>
        </div>
      </div>

      {/* Filter and Search Bar */}
      <div className={styles.searchBar} style={{ gap: 12, flexWrap: 'wrap', marginBottom: 20 }}>
        <div style={{ position: 'relative', flex: 1, minWidth: 260 }}>
          <input 
            type="text" 
            className={styles.inputField} 
            placeholder="Search team by email..."
            value={search}
            onChange={(e) => { setSearch(e.target.value); setUserPage(1); }}
            style={{ paddingLeft: 38 }}
          />
          <Search size={16} color="#64748b" style={{ position: 'absolute', left: 12, top: 12 }} />
        </div>
        
        <select 
          value={roleFilter} 
          onChange={(e) => { setRoleFilter(e.target.value); setUserPage(1); }} 
          className={styles.inputField}
          style={{ width: 140, backgroundColor: '#0b0f19' }}
        >
          <option value="">All Roles</option>
          <option value="admin">Admin</option>
          <option value="editor">Editor</option>
          <option value="viewer">Viewer</option>
        </select>

        <select 
          value={statusFilter} 
          onChange={(e) => { setStatusFilter(e.target.value); setUserPage(1); }} 
          className={styles.inputField}
          style={{ width: 140, backgroundColor: '#0b0f19' }}
        >
          <option value="">All Statuses</option>
          <option value="active">Active</option>
          <option value="disabled">Disabled</option>
        </select>
      </div>

      {/* Active Users Table */}
      <div className={styles.mappingCard} style={{ marginBottom: 32 }}>
        <h3 style={{ marginBottom: 16, fontSize: 15 }}>Organization Members</h3>
        <table className={styles.productTable}>
          <thead>
            <tr>
              <th>Email</th>
              <th>Role</th>
              <th>Status</th>
              <th>Invited By</th>
              <th>Joined Date</th>
              <th>Last Login</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.length === 0 ? (
              <tr>
                <td colSpan={7} style={{ textAlign: 'center', color: '#64748b' }}>No users match the active search filters.</td>
              </tr>
            ) : (
              users.map((user) => (
                <tr key={user.id}>
                  <td style={{ fontWeight: 500, color: '#f8fafc' }}>{user.email}</td>
                  <td>
                    <select
                      value={user.role}
                      onChange={(e) => handleRoleChange(user, e.target.value)}
                      style={{ 
                        backgroundColor: '#0b0f19', 
                        color: '#c7d2fe', 
                        border: '1px solid #2e3c64', 
                        borderRadius: 4, 
                        fontSize: 12,
                        padding: '2px 8px' 
                      }}
                    >
                      <option value="viewer">Viewer</option>
                      <option value="editor">Editor</option>
                      <option value="admin">Admin</option>
                    </select>
                  </td>
                  <td>
                    <span style={{ 
                      padding: '2px 6px', 
                      borderRadius: 4, 
                      fontSize: 11, 
                      fontWeight: 600, 
                      backgroundColor: user.is_active ? 'rgba(16, 185, 129, 0.1)' : 'rgba(239, 68, 68, 0.1)', 
                      color: user.is_active ? '#10b981' : '#ef4444' 
                    }}>
                      {user.is_active ? "Active" : "Disabled"}
                    </span>
                  </td>
                  <td style={{ color: '#94a3b8' }}>{user.invited_by || 'System'}</td>
                  <td style={{ color: '#64748b' }}>
                    {new Date(user.created_at).toLocaleDateString()}
                  </td>
                  <td style={{ color: '#64748b' }}>
                    {user.last_login_at ? new Date(user.last_login_at).toLocaleString() : 'Never'}
                  </td>
                  <td>
                    <button 
                      onClick={() => handleDisableToggle(user)}
                      className={`${styles.btn} ${user.is_active ? styles.btnDanger : styles.btnSuccess}`}
                      style={{ padding: '4px 8px', fontSize: 11 }}
                    >
                      <UserX size={12} /> {user.is_active ? "Disable" : "Enable"}
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
        
        {/* Pagination controls */}
        {totalUsers > 10 && (
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 12, marginTop: 16 }}>
            <button 
              disabled={userPage === 1}
              onClick={() => setUserPage(p => p - 1)}
              className={styles.btn}
              style={{ padding: '6px 12px' }}
            >
              Previous
            </button>
            <span style={{ alignSelf: 'center', fontSize: 13, color: '#94a3b8' }}>Page {userPage}</span>
            <button 
              disabled={userPage * 10 >= totalUsers}
              onClick={() => setUserPage(p => p + 1)}
              className={styles.btn}
              style={{ padding: '6px 12px' }}
            >
              Next
            </button>
          </div>
        )}
      </div>

      {/* Pending Invitations Section */}
      <div className={styles.mappingCard}>
        <h3 style={{ marginBottom: 16, fontSize: 15 }}>Pending User Invitations</h3>
        <table className={styles.productTable}>
          <thead>
            <tr>
              <th>Invited Email</th>
              <th>Role</th>
              <th>Invited By</th>
              <th>Sent Date</th>
              <th>Expiry</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {invitations.length === 0 ? (
              <tr>
                <td colSpan={7} style={{ textAlign: 'center', color: '#64748b' }}>No pending or archived invitations.</td>
              </tr>
            ) : (
              invitations.map((inv) => (
                <tr key={inv.id}>
                  <td style={{ color: '#e2e8f0' }}>{inv.email}</td>
                  <td>
                    <span style={{ textTransform: 'capitalize', color: '#a5b4fc', fontSize: 12 }}>{inv.role}</span>
                  </td>
                  <td style={{ color: '#94a3b8' }}>{inv.invited_by || 'System'}</td>
                  <td style={{ color: '#64748b' }}>
                    {new Date(inv.last_sent_at).toLocaleString()}
                  </td>
                  <td style={{ color: '#64748b' }}>
                    {new Date(inv.expires_at).toLocaleString()}
                  </td>
                  <td>
                    <span style={{ 
                      padding: '2px 6px', 
                      borderRadius: 4, 
                      fontSize: 11, 
                      fontWeight: 600, 
                      backgroundColor: inv.status === 'pending' ? 'rgba(99, 102, 241, 0.1)' : 
                                      inv.status === 'accepted' ? 'rgba(16, 185, 129, 0.1)' : 'rgba(239, 68, 68, 0.1)',
                      color: inv.status === 'pending' ? '#818cf8' : 
                             inv.status === 'accepted' ? '#10b981' : '#ef4444',
                      textTransform: 'uppercase'
                    }}>
                      {inv.status}
                    </span>
                    {inv.email_delivery_status === 'failed' && (
                      <span style={{ display: 'block', fontSize: 10, color: '#ef4444', marginTop: 4 }}>
                        Email delivery failed
                      </span>
                    )}
                  </td>
                  <td>
                    {inv.status === 'pending' && (
                      <div style={{ display: 'flex', gap: 8 }}>
                        <button 
                          onClick={() => handleResend(inv)}
                          className={styles.btn}
                          style={{ padding: '4px 8px', fontSize: 11 }}
                        >
                          <RefreshCw size={12} /> Resend
                        </button>
                        <button 
                          onClick={() => handleRevoke(inv)}
                          className={`${styles.btn} ${styles.btnDanger}`}
                          style={{ padding: '4px 8px', fontSize: 11 }}
                        >
                          <X size={12} /> Revoke
                        </button>
                      </div>
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Invite User Modal */}
      {inviteModalOpen && (
        <div style={{ 
          position: 'fixed', top: 0, left: 0, width: '100%', height: '100%', 
          backgroundColor: 'rgba(0,0,0,0.7)', backdropFilter: 'blur(5px)',
          display: 'flex', justifyContent: 'center', alignItems: 'center', zIndex: 1000 
        }}>
          <div className={styles.mappingCard} style={{ width: '100%', maxWidth: 450, position: 'relative' }}>
            <button 
              onClick={() => setInviteModalOpen(false)}
              style={{ position: 'absolute', right: 16, top: 16, background: 'none', border: 'none', color: '#64748b', cursor: 'pointer' }}
            >
              <X size={20} />
            </button>
            <h3 style={{ fontSize: 16, marginBottom: 8 }}>Add Team Member Directly</h3>
            <p style={{ color: '#94a3b8', fontSize: 12, lineHeight: 1.5, marginBottom: 16 }}>
              No email will be sent. The account is activated immediately; share the temporary password privately.
            </p>

            {inviteError && (
              <div style={{ padding: 12, backgroundColor: 'rgba(239, 68, 68, 0.1)', border: '1px solid #ef4444', borderRadius: 4, color: '#ef4444', fontSize: 13, marginBottom: 16 }}>
                {inviteError}
              </div>
            )}

            <form onSubmit={handleAddUser}>
              <div className={styles.formGroup}>
                <label>Email Address</label>
                <input 
                  type="email" 
                  className={styles.inputField} 
                  required
                  placeholder="colleague@brand.com"
                  value={inviteEmail}
                  onChange={(e) => setInviteEmail(e.target.value)}
                />
              </div>

              <div className={styles.formGroup}>
                <label>System Role</label>
                <select 
                  value={inviteRole}
                  onChange={(e) => setInviteRole(e.target.value)}
                  className={styles.inputField}
                  style={{ backgroundColor: '#0b0f19' }}
                >
                  <option value="viewer">Viewer (Read-only)</option>
                  <option value="editor">Editor (Enrichment, edits, overrides)</option>
                  <option value="admin">Administrator (Full settings control)</option>
                </select>
              </div>

              <div className={styles.formGroup}>
                <label>Temporary Password</label>
                <input
                  type="password"
                  className={styles.inputField}
                  required
                  minLength={12}
                  autoComplete="new-password"
                  placeholder="At least 12 characters"
                  value={invitePassword}
                  onChange={(e) => setInvitePassword(e.target.value)}
                />
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 12, marginTop: 24 }}>
                <button 
                  type="button" 
                  className={styles.btn}
                  onClick={() => setInviteModalOpen(false)}
                  disabled={inviteLoading}
                >
                  Cancel
                </button>
                <button 
                  type="submit" 
                  className={`${styles.btn} ${styles.btnPrimary}`}
                  disabled={inviteLoading}
                >
                  {inviteLoading ? "Creating user..." : "Create Active User"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Confirmation Dialog Modal */}
      {confirmModal.open && (
        <div style={{ 
          position: 'fixed', top: 0, left: 0, width: '100%', height: '100%', 
          backgroundColor: 'rgba(0,0,0,0.8)', backdropFilter: 'blur(5px)',
          display: 'flex', justifyContent: 'center', alignItems: 'center', zIndex: 1010 
        }}>
          <div className={styles.mappingCard} style={{ width: '100%', maxWidth: 450, border: confirmModal.danger ? '1px solid #ef4444' : '1px solid #6366f1' }}>
            <h3 style={{ fontSize: 16, marginBottom: 12, color: confirmModal.danger ? '#ef4444' : '#f8fafc' }}>
              {confirmModal.title}
            </h3>
            <p style={{ color: '#94a3b8', fontSize: 13, lineHeight: 1.6, marginBottom: 24 }}>
              {confirmModal.message}
            </p>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 12 }}>
              <button 
                className={styles.btn}
                onClick={() => setConfirmModal(prev => ({ ...prev, open: false }))}
              >
                Cancel
              </button>
              <button 
                className={`${styles.btn} ${confirmModal.danger ? styles.btnDanger : styles.btnPrimary}`}
                onClick={confirmModal.action}
              >
                Confirm Action
              </button>
            </div>
          </div>
        </div>
      )}
    </Shell>
  );
}
