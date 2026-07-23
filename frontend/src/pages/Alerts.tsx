import { useMemo, useState, type FormEvent } from 'react'
import { useApi } from '../hooks/useApi'
import { api, errorMessage } from '../api/client'
import {
  ALERT_TYPES,
  HORIZONS,
  SYMBOLS,
  SYMBOL_LABELS,
  type Alert,
  type AlertCondition,
  type AlertEvent,
  type AlertType,
  type Horizon
} from '../api/types'
import { unwrapList } from '../lib/unwrap'
import { useSettings } from '../lib/settings'
import { formatDateTime, formatGrouped, relativeTime } from '../lib/format'
import ConfirmDialog from '../components/ConfirmDialog'
import Loading from '../components/Loading'
import ErrorMessage from '../components/ErrorMessage'
import EmptyState from '../components/EmptyState'

interface AlertTypeSpec {
  value: AlertType
  label: string
  hint: string
  fields: Array<'symbol' | 'threshold' | 'horizon' | 'minutes' | 'provider'>
  thresholdLabel?: string
}

const TYPE_SPECS: AlertTypeSpec[] = [
  {
    value: 'price_above',
    label: 'Price above',
    hint: 'Fires when the symbol price rises above the threshold (IRT for local symbols).',
    fields: ['symbol', 'threshold'],
    thresholdLabel: 'Price threshold'
  },
  {
    value: 'price_below',
    label: 'Price below',
    hint: 'Fires when the symbol price falls below the threshold.',
    fields: ['symbol', 'threshold'],
    thresholdLabel: 'Price threshold'
  },
  {
    value: 'signal_change',
    label: 'Signal change',
    hint: 'Fires whenever the composite buy/hold/sell signal level changes.',
    fields: []
  },
  {
    value: 'confidence_above',
    label: 'Confidence above',
    hint: 'Fires when a prediction arrives with confidence above the threshold (0–1).',
    fields: ['horizon', 'threshold'],
    thresholdLabel: 'Confidence threshold (0–1)'
  },
  {
    value: 'volatility_spike',
    label: 'Volatility spike',
    hint: 'Fires when short-term volatility exceeds the threshold (%).',
    fields: ['threshold'],
    thresholdLabel: 'Volatility threshold (%)'
  },
  {
    value: 'premium_above',
    label: 'Premium above',
    hint: 'Fires when the local premium over the theoretical price exceeds the threshold (%).',
    fields: ['threshold'],
    thresholdLabel: 'Premium threshold (%)'
  },
  {
    value: 'stale_data',
    label: 'Stale data',
    hint: 'Fires when no fresh price has arrived for the given number of minutes.',
    fields: ['minutes']
  },
  {
    value: 'provider_failure',
    label: 'Provider failure',
    hint: 'Fires when a data provider keeps failing. Leave code empty to watch all providers.',
    fields: ['provider']
  },
  {
    value: 'model_degradation',
    label: 'Model degradation',
    hint: 'Fires when the active model for a horizon starts underperforming.',
    fields: ['horizon']
  }
]

function specFor(type: AlertType): AlertTypeSpec {
  return TYPE_SPECS.find((s) => s.value === type) ?? TYPE_SPECS[0]
}

function describeCondition(c: AlertCondition): string {
  const parts: string[] = []
  if (c.symbol) parts.push(`symbol: ${c.symbol}`)
  if (c.threshold !== undefined) parts.push(`threshold: ${formatGrouped(c.threshold, 4)}`)
  if (c.horizon) parts.push(`horizon: ${c.horizon}`)
  if (c.minutes !== undefined) parts.push(`minutes: ${c.minutes}`)
  if (c.provider) parts.push(`provider: ${c.provider}`)
  return parts.length > 0 ? parts.join(' · ') : '—'
}

interface AlertForm {
  alert_type: AlertType
  symbol: string
  threshold: string
  horizon: Horizon
  minutes: string
  provider: string
}

function emptyAlertForm(): AlertForm {
  return {
    alert_type: 'price_above',
    symbol: 'IR_GOLD_18K',
    threshold: '',
    horizon: '1d',
    minutes: '60',
    provider: ''
  }
}

export default function Alerts() {
  const { calendar } = useSettings()

  const alerts = useApi<unknown>('/alerts')
  const alertList = useMemo(() => unwrapList<Alert>(alerts.data, 'items', 'alerts'), [alerts.data])

  const [unackedOnly, setUnackedOnly] = useState(true)
  const events = useApi<unknown>(`/alerts/events${unackedOnly ? '?unacked=true' : ''}`, [unackedOnly])
  const eventList = useMemo(
    () => unwrapList<AlertEvent>(events.data, 'items', 'events'),
    [events.data]
  )

  const [form, setForm] = useState<AlertForm>(emptyAlertForm)
  const [formError, setFormError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [deleteId, setDeleteId] = useState<number | null>(null)
  const [rowError, setRowError] = useState<string | null>(null)

  const spec = specFor(form.alert_type)

  function buildCondition(): AlertCondition | string {
    const c: AlertCondition = {}
    if (spec.fields.includes('symbol')) {
      c.symbol = form.symbol
    }
    if (spec.fields.includes('threshold')) {
      const t = Number(form.threshold)
      if (form.threshold.trim() === '' || Number.isNaN(t)) return 'Enter a numeric threshold.'
      if (form.alert_type === 'confidence_above' && (t < 0 || t > 1)) {
        return 'Confidence threshold must be between 0 and 1.'
      }
      c.threshold = t
    }
    if (spec.fields.includes('horizon')) {
      c.horizon = form.horizon
    }
    if (spec.fields.includes('minutes')) {
      const m = Number(form.minutes)
      if (!(m > 0)) return 'Minutes must be a positive number.'
      c.minutes = m
    }
    if (spec.fields.includes('provider') && form.provider.trim() !== '') {
      c.provider = form.provider.trim()
    }
    return c
  }

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setFormError(null)
    const condition = buildCondition()
    if (typeof condition === 'string') {
      setFormError(condition)
      return
    }
    setSaving(true)
    try {
      await api('/alerts', {
        method: 'POST',
        body: { alert_type: form.alert_type, condition, enabled: true }
      })
      setForm(emptyAlertForm())
      alerts.reload()
    } catch (err) {
      setFormError(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  async function onToggle(a: Alert) {
    setRowError(null)
    try {
      await api(`/alerts/${a.id}`, {
        method: 'PUT',
        body: { alert_type: a.alert_type, condition: a.condition, enabled: !a.enabled }
      })
      alerts.reload()
    } catch (err) {
      setRowError(errorMessage(err))
    }
  }

  async function onDeleteConfirmed() {
    if (deleteId === null) return
    setRowError(null)
    try {
      await api(`/alerts/${deleteId}`, { method: 'DELETE' })
      alerts.reload()
    } catch (err) {
      setRowError(errorMessage(err))
    }
    setDeleteId(null)
  }

  async function onAck(ev: AlertEvent) {
    setRowError(null)
    try {
      await api(`/alerts/events/${ev.id}/ack`, { method: 'POST' })
      events.reload()
    } catch (err) {
      setRowError(errorMessage(err))
    }
  }

  return (
    <div className="page-body">
      <h2 className="page-title">Alerts</h2>

      {rowError && <ErrorMessage message={rowError} />}

      <div className="grid grid-2">
        <div className="card">
          <div className="card-title">Create alert</div>
          <form onSubmit={onCreate} className="form-grid">
            <div className="field field-full">
              <label htmlFor="alert-type">Alert type</label>
              <select
                id="alert-type"
                value={form.alert_type}
                onChange={(e) => setForm({ ...emptyAlertForm(), alert_type: e.target.value as AlertType })}
              >
                {ALERT_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {specFor(t).label}
                  </option>
                ))}
              </select>
              <span className="muted small">{spec.hint}</span>
            </div>

            {spec.fields.includes('symbol') && (
              <div className="field">
                <label htmlFor="alert-symbol">Symbol</label>
                <select
                  id="alert-symbol"
                  value={form.symbol}
                  onChange={(e) => setForm((f) => ({ ...f, symbol: e.target.value }))}
                >
                  {SYMBOLS.map((sym) => (
                    <option key={sym} value={sym}>
                      {SYMBOL_LABELS[sym]}
                    </option>
                  ))}
                </select>
              </div>
            )}

            {spec.fields.includes('threshold') && (
              <div className="field">
                <label htmlFor="alert-threshold">{spec.thresholdLabel ?? 'Threshold'}</label>
                <input
                  id="alert-threshold"
                  type="number"
                  step="any"
                  required
                  value={form.threshold}
                  onChange={(e) => setForm((f) => ({ ...f, threshold: e.target.value }))}
                />
              </div>
            )}

            {spec.fields.includes('horizon') && (
              <div className="field">
                <label htmlFor="alert-horizon">Horizon</label>
                <select
                  id="alert-horizon"
                  value={form.horizon}
                  onChange={(e) => setForm((f) => ({ ...f, horizon: e.target.value as Horizon }))}
                >
                  {HORIZONS.map((h) => (
                    <option key={h} value={h}>
                      {h}
                    </option>
                  ))}
                </select>
              </div>
            )}

            {spec.fields.includes('minutes') && (
              <div className="field">
                <label htmlFor="alert-minutes">Minutes without fresh data</label>
                <input
                  id="alert-minutes"
                  type="number"
                  min="1"
                  step="1"
                  required
                  value={form.minutes}
                  onChange={(e) => setForm((f) => ({ ...f, minutes: e.target.value }))}
                />
              </div>
            )}

            {spec.fields.includes('provider') && (
              <div className="field">
                <label htmlFor="alert-provider">Provider code (optional)</label>
                <input
                  id="alert-provider"
                  type="text"
                  placeholder="e.g. tgju"
                  value={form.provider}
                  onChange={(e) => setForm((f) => ({ ...f, provider: e.target.value }))}
                />
              </div>
            )}

            {formError && <div className="error-box field-full">{formError}</div>}
            <div className="row field-full">
              <button type="submit" className="btn btn-primary" disabled={saving}>
                {saving ? 'Creating…' : 'Create alert'}
              </button>
            </div>
          </form>
        </div>

        <div className="card">
          <div className="row space-between">
            <div className="card-title">Events</div>
            <label className="check-label">
              <input
                type="checkbox"
                checked={unackedOnly}
                onChange={(e) => setUnackedOnly(e.target.checked)}
              />{' '}
              Unacknowledged only
            </label>
          </div>
          {events.loading ? (
            <Loading label="Loading events…" />
          ) : events.error ? (
            <ErrorMessage message={events.error} onRetry={events.reload} />
          ) : eventList.length === 0 ? (
            <EmptyState title="No alert events" hint="Triggered alerts will show up here." />
          ) : (
            <ul className="event-list">
              {eventList.map((ev) => {
                const acked = ev.acknowledged === true
                return (
                  <li key={ev.id} className={`event-row ${acked ? 'event-acked' : ''}`}>
                    <div className="event-main">
                      {ev.alert_type && <span className="badge badge-info">{ev.alert_type}</span>}
                      <span>{ev.message}</span>
                    </div>
                    <div className="row">
                      <span className="muted small" title={formatDateTime(ev.triggered_at ?? ev.created_at, calendar)}>
                        {relativeTime(ev.triggered_at ?? ev.created_at)}
                      </span>
                      {!acked && (
                        <button
                          type="button"
                          className="btn btn-ghost btn-sm"
                          onClick={() => onAck(ev)}
                        >
                          Acknowledge
                        </button>
                      )}
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-title">Configured alerts</div>
        {alerts.loading ? (
          <Loading label="Loading alerts…" />
        ) : alerts.error ? (
          <ErrorMessage message={alerts.error} onRetry={alerts.reload} />
        ) : alertList.length === 0 ? (
          <EmptyState title="No alerts yet" hint="Create one on the left — all 9 types are supported." />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Type</th>
                  <th>Condition</th>
                  <th>Last triggered</th>
                  <th>Status</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {alertList.map((a) => (
                  <tr key={a.id}>
                    <td>{specFor(a.alert_type).label}</td>
                    <td className="mono small">{describeCondition(a.condition ?? {})}</td>
                    <td className="muted small">
                      {a.last_triggered_at ? relativeTime(a.last_triggered_at) : 'never'}
                    </td>
                    <td>
                      <span className={`badge ${a.enabled ? 'badge-ok' : 'badge-off'}`}>
                        {a.enabled ? 'enabled' : 'disabled'}
                      </span>
                    </td>
                    <td className="num">
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        onClick={() => onToggle(a)}
                      >
                        {a.enabled ? 'Disable' : 'Enable'}
                      </button>{' '}
                      <button
                        type="button"
                        className="btn btn-danger btn-sm"
                        onClick={() => setDeleteId(a.id)}
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <ConfirmDialog
        open={deleteId !== null}
        title="Delete alert?"
        message="The alert and its future notifications will be removed."
        confirmLabel="Delete"
        danger
        onConfirm={onDeleteConfirmed}
        onCancel={() => setDeleteId(null)}
      />
    </div>
  )
}
