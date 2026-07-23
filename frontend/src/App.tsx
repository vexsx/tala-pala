import { useEffect } from 'react'
import {
  BrowserRouter,
  Navigate,
  NavLink,
  Outlet,
  Route,
  Routes,
  useLocation
} from 'react-router-dom'
import { AuthProvider, useAuth } from './auth/AuthContext'
import ProtectedRoute from './auth/ProtectedRoute'
import Login from './auth/Login'
import { SettingsProvider, useSettings } from './lib/settings'
import { useApi } from './hooks/useApi'
import type { CurrentPricesResponse } from './api/types'
import { formatPct, formatToman, pctClass } from './lib/format'
import DataFreshness from './components/DataFreshness'
import ErrorBoundary from './components/ErrorBoundary'
import ThemeToggle from './components/ThemeToggle'
import Overview from './pages/Overview'
import Brief from './pages/Brief'
import TradePanel from './pages/TradePanel'
import Forecast from './pages/Forecast'
import Technical from './pages/Technical'
import Drivers from './pages/Drivers'
import Portfolio from './pages/Portfolio'
import Alerts from './pages/Alerts'
import Models from './pages/Models'
import Issues from './pages/Issues'
import Users from './pages/Users'

const NAV_ITEMS: Array<{ to: string; label: string; end?: boolean; adminOnly?: boolean }> = [
  { to: '/', label: 'Overview', end: true },
  { to: '/brief', label: 'Brief' },
  { to: '/trade', label: 'Trade' },
  { to: '/forecast', label: 'Forecast' },
  { to: '/technical', label: 'Technical' },
  { to: '/drivers', label: 'Drivers' },
  { to: '/portfolio', label: 'Portfolio' },
  { to: '/alerts', label: 'Alerts' },
  { to: '/models', label: 'Models' },
  { to: '/issues', label: 'Issues', adminOnly: true },
  { to: '/users', label: 'Users', adminOnly: true }
]

function Sidebar() {
  const { user } = useAuth()
  const items = NAV_ITEMS.filter((item) => !item.adminOnly || user?.role === 'admin')
  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">◈</span>
        <span>
          Iran Gold <strong>Predictor</strong>
        </span>
      </div>
      <nav className="side-nav" aria-label="Main navigation">
        {items.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}
          >
            {item.label}
          </NavLink>
        ))}
      </nav>
    </aside>
  )
}

function TopBar() {
  const { unit, setUnit, calendar, setCalendar } = useSettings()
  const { user, logout } = useAuth()
  const current = useApi<CurrentPricesResponse>('/prices/current')

  // Keep the ticker fresh.
  useEffect(() => {
    const id = window.setInterval(() => current.reload(), 60_000)
    return () => window.clearInterval(id)
  }, [current.reload]) // eslint-disable-line react-hooks/exhaustive-deps

  const gold = current.data?.prices?.IR_GOLD_18K

  return (
    <header className="topbar">
      <div className="ticker" aria-live="off">
        {gold ? (
          <>
            <span className="ticker-label">18k</span>
            <span className="ticker-value mono">{formatToman(gold.value, unit, false)}</span>
            <span className={`delta ${pctClass(gold.change_24h_pct)}`}>
              {formatPct(gold.change_24h_pct)}
            </span>
            <DataFreshness
              timestamp={gold.observed_at}
              stale={gold.stale}
              marketState={gold.market_state}
            />
          </>
        ) : (
          <span className="muted small">
            {current.loading ? 'Loading price…' : 'Price unavailable'}
          </span>
        )}
      </div>
      <div className="topbar-controls">
        <div className="toggle-group" role="group" aria-label="Currency display">
          <button
            type="button"
            className={unit === 'IRT' ? 'active' : ''}
            onClick={() => setUnit('IRT')}
            title="Display in toman (IRT)"
          >
            تومان
          </button>
          <button
            type="button"
            className={unit === 'IRR' ? 'active' : ''}
            onClick={() => setUnit('IRR')}
            title="Display in rial (IRR, ×10)"
          >
            ریال
          </button>
        </div>
        <div className="toggle-group" role="group" aria-label="Calendar">
          <button
            type="button"
            className={calendar === 'jalali' ? 'active' : ''}
            onClick={() => setCalendar('jalali')}
          >
            Jalali
          </button>
          <button
            type="button"
            className={calendar === 'gregorian' ? 'active' : ''}
            onClick={() => setCalendar('gregorian')}
          >
            Gregorian
          </button>
        </div>
        <ThemeToggle />
        {user && <span className="muted small user-email">{user.email}</span>}
        <button type="button" className="btn btn-ghost btn-sm" onClick={logout}>
          Sign out
        </button>
      </div>
    </header>
  )
}

function Layout() {
  const location = useLocation()
  return (
    <div className="app">
      <Sidebar />
      <div className="main-col">
        <TopBar />
        <div className="banner" role="note">
          ⚠️ Predictions are uncertain estimates for decision support — not financial advice.
        </div>
        <main className="page">
          <ErrorBoundary key={location.pathname}>
            <Outlet />
          </ErrorBoundary>
        </main>
      </div>
    </div>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <SettingsProvider>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route
              element={
                <ProtectedRoute>
                  <Layout />
                </ProtectedRoute>
              }
            >
              <Route index element={<Overview />} />
              <Route path="/brief" element={<Brief />} />
              <Route path="/trade" element={<TradePanel />} />
              <Route path="/forecast" element={<Forecast />} />
              <Route path="/technical" element={<Technical />} />
              <Route path="/drivers" element={<Drivers />} />
              <Route path="/portfolio" element={<Portfolio />} />
              <Route path="/alerts" element={<Alerts />} />
              <Route path="/models" element={<Models />} />
              <Route path="/issues" element={<Issues />} />
              <Route path="/users" element={<Users />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </SettingsProvider>
      </AuthProvider>
    </BrowserRouter>
  )
}
