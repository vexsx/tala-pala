import { useState, type FormEvent } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { errorMessage } from '../api/client'
import { useAuth } from './AuthContext'

type Mode = 'login' | 'register'

export default function Login() {
  const { token, login, register } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const from = (location.state as { from?: string } | null)?.from ?? '/'

  const [mode, setMode] = useState<Mode>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (token) {
    return <Navigate to={from} replace />
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    if (mode === 'register') {
      if (password.length < 10) {
        setError('Password must be at least 10 characters.')
        return
      }
      if (password !== confirm) {
        setError('Passwords do not match.')
        return
      }
    }
    setBusy(true)
    try {
      if (mode === 'register') {
        await register(email, password)
      } else {
        await login(email, password)
      }
      navigate(from, { replace: true })
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <div className="card login-card">
        <h1 className="login-title">Iran Gold Predictor</h1>
        <p className="muted small">Sign in to view prices, forecasts and your portfolio.</p>

        <div className="tabs" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'login'}
            className={`tab ${mode === 'login' ? 'active' : ''}`}
            onClick={() => setMode('login')}
          >
            Sign in
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'register'}
            className={`tab ${mode === 'register' ? 'active' : ''}`}
            onClick={() => setMode('register')}
          >
            Register
          </button>
        </div>

        <form onSubmit={onSubmit} className="login-form">
          <div className="field">
            <label htmlFor="login-email">Email</label>
            <input
              id="login-email"
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="login-password">Password</label>
            <input
              id="login-password"
              type="password"
              required
              minLength={mode === 'register' ? 10 : undefined}
              autoComplete={mode === 'register' ? 'new-password' : 'current-password'}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          {mode === 'register' && (
            <div className="field">
              <label htmlFor="login-confirm">Confirm password</label>
              <input
                id="login-confirm"
                type="password"
                required
                autoComplete="new-password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
              />
            </div>
          )}

          {error && <div className="error-box">{error}</div>}

          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? 'Please wait…' : mode === 'register' ? 'Create account' : 'Sign in'}
          </button>
        </form>

        {mode === 'register' && (
          <p className="muted small">
            The first registered user becomes the admin. Later registrations may require an admin
            unless open registration is enabled. Passwords must be at least 10 characters.
          </p>
        )}

        <p className="muted small banner-inline">
          ⚠️ Predictions are uncertain estimates for decision support — not financial advice.
        </p>
      </div>
    </div>
  )
}
