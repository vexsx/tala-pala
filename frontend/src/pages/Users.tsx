import { useState, type FormEvent } from 'react'
import { useApi } from '../hooks/useApi'
import { api, errorMessage } from '../api/client'
import type { AdminUser } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { useSettings } from '../lib/settings'
import { formatDateTime } from '../lib/format'
import ConfirmDialog from '../components/ConfirmDialog'
import Loading from '../components/Loading'
import ErrorMessage from '../components/ErrorMessage'
import EmptyState from '../components/EmptyState'

interface UsersResponse {
  items: AdminUser[]
}

/** Admin-only user management: registration is closed, accounts live here. */
export default function Users() {
  const { user: me } = useAuth()
  const { calendar } = useSettings()
  const users = useApi<UsersResponse>('/admin/users')

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<'user' | 'admin'>('user')
  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<AdminUser | null>(null)
  const [resetting, setResetting] = useState<AdminUser | null>(null)
  const [newPassword, setNewPassword] = useState('')

  if (me?.role !== 'admin') {
    return (
      <div className="page-body">
        <h2 className="page-title">Users</h2>
        <EmptyState title="Admin only" hint="User management requires the admin role." />
      </div>
    )
  }

  const flash = (msg: string) => {
    setNotice(msg)
    setActionError(null)
    window.setTimeout(() => setNotice(null), 4000)
  }
  const fail = (err: unknown) => {
    setActionError(errorMessage(err))
    setNotice(null)
  }

  const createUser = async (e: FormEvent) => {
    e.preventDefault()
    setFormError(null)
    if (password.length < 10) {
      setFormError('Password must be at least 10 characters.')
      return
    }
    setBusy(true)
    try {
      await api('/admin/users', { method: 'POST', body: { email, password, role } })
      setEmail('')
      setPassword('')
      setRole('user')
      flash(`Created ${email}.`)
      users.reload()
    } catch (err) {
      setFormError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const changeRole = async (u: AdminUser, nextRole: string) => {
    if (nextRole === u.role) return
    try {
      await api(`/admin/users/${u.id}`, { method: 'PUT', body: { role: nextRole } })
      flash(`${u.email} is now ${nextRole}.`)
      users.reload()
    } catch (err) {
      fail(err)
      users.reload() // reset the select to the server truth
    }
  }

  const confirmReset = async () => {
    if (!resetting) return
    const target = resetting
    setResetting(null)
    try {
      await api(`/admin/users/${target.id}`, { method: 'PUT', body: { password: newPassword } })
      flash(`Password reset for ${target.email}.`)
    } catch (err) {
      fail(err)
    } finally {
      setNewPassword('')
    }
  }

  const confirmDelete = async () => {
    if (!deleting) return
    const target = deleting
    setDeleting(null)
    try {
      await api(`/admin/users/${target.id}`, { method: 'DELETE' })
      flash(`Deleted ${target.email}.`)
      users.reload()
    } catch (err) {
      fail(err)
    }
  }

  const items = users.data?.items ?? []

  return (
    <div className="page-body">
      <h2 className="page-title">Users</h2>
      <p className="muted">
        Self-registration is disabled — every account is created here. You cannot delete your own
        account or remove the last admin.
      </p>

      {notice && <div className="callout">{notice}</div>}
      {actionError && <ErrorMessage message={actionError} />}

      <div className="card">
        <div className="card-title">Create user</div>
        <form onSubmit={createUser} className="login-form" style={{ maxWidth: 480 }}>
          <div className="field">
            <label htmlFor="nu-email">Email</label>
            <input
              id="nu-email"
              type="email"
              required
              autoComplete="off"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="nu-password">Password (min 10 chars)</label>
            <input
              id="nu-password"
              type="password"
              required
              minLength={10}
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="nu-role">Role</label>
            <select id="nu-role" value={role} onChange={(e) => setRole(e.target.value as 'user' | 'admin')}>
              <option value="user">user</option>
              <option value="admin">admin</option>
            </select>
          </div>
          {formError && <div className="error-box">{formError}</div>}
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? 'Creating…' : 'Create user'}
          </button>
        </form>
      </div>

      <div className="card">
        <div className="card-title">Accounts ({items.length})</div>
        {users.loading ? (
          <Loading label="Loading users…" />
        ) : users.error ? (
          <ErrorMessage message={users.error} onRetry={users.reload} />
        ) : items.length === 0 ? (
          <EmptyState title="No users" />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Email</th>
                  <th>Role</th>
                  <th>Created</th>
                  <th className="num">Portfolio txs</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {items.map((u) => {
                  const isSelf = u.id === me?.id
                  return (
                    <tr key={u.id}>
                      <td className="mono">
                        {u.email}
                        {isSelf && <span className="badge" style={{ marginLeft: 6 }}>you</span>}
                      </td>
                      <td>
                        <select
                          value={u.role}
                          aria-label={`Role for ${u.email}`}
                          disabled={isSelf}
                          onChange={(e) => changeRole(u, e.target.value)}
                        >
                          <option value="user">user</option>
                          <option value="admin">admin</option>
                        </select>
                      </td>
                      <td className="small">{formatDateTime(u.created_at, calendar)}</td>
                      <td className="num mono">{u.transactions}</td>
                      <td>
                        <button
                          type="button"
                          className="btn btn-ghost btn-sm"
                          onClick={() => {
                            setNewPassword('')
                            setResetting(u)
                          }}
                        >
                          Reset password
                        </button>{' '}
                        <button
                          type="button"
                          className="btn btn-ghost btn-sm"
                          disabled={isSelf}
                          title={isSelf ? 'You cannot delete your own account' : undefined}
                          onClick={() => setDeleting(u)}
                        >
                          Delete
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <ConfirmDialog
        open={deleting !== null}
        title={`Delete ${deleting?.email ?? ''}?`}
        message={
          deleting && deleting.transactions > 0
            ? `This account has ${deleting.transactions} portfolio transaction(s) which will be permanently deleted with it.`
            : 'The account and all its data will be permanently deleted.'
        }
        confirmLabel="Delete"
        danger
        onConfirm={confirmDelete}
        onCancel={() => setDeleting(null)}
      />

      {resetting && (
        <div className="modal-overlay" role="presentation" onClick={() => setResetting(null)}>
          <div
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-label={`Reset password for ${resetting.email}`}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="modal-title">Reset password — {resetting.email}</h3>
            <div className="field">
              <label htmlFor="rp-password">New password (min 10 chars)</label>
              <input
                id="rp-password"
                type="password"
                minLength={10}
                autoComplete="new-password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
              />
            </div>
            <div className="modal-actions">
              <button type="button" className="btn btn-ghost" onClick={() => setResetting(null)}>
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={newPassword.length < 10}
                onClick={confirmReset}
              >
                Reset password
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
