import { useMemo } from 'react'
import { useApi } from '../hooks/useApi'
import {
  HORIZON_LABELS,
  type HorizonPerformance,
  type ModelVersion,
  type TrainingRun
} from '../api/types'
import { unwrapField, unwrapList } from '../lib/unwrap'
import { useSettings } from '../lib/settings'
import { formatDateTime, formatPct, relativeTime } from '../lib/format'
import Loading from '../components/Loading'
import ErrorMessage from '../components/ErrorMessage'
import EmptyState from '../components/EmptyState'

function fmtMetric(v: number | undefined, digits = 3): string {
  return v === undefined || v === null || Number.isNaN(v) ? '—' : v.toFixed(digits)
}

function normAccuracy(v: number | undefined): number | null {
  if (v === undefined || v === null || Number.isNaN(v)) return null
  return v > 1 ? v / 100 : v
}

function healthOf(r: HorizonPerformance): { label: string; cls: string } {
  if (r.degraded === true) return { label: 'Degraded', cls: 'badge-bad' }
  const modelSmape = r.metrics?.smape
  const baselineSmape = r.baseline?.smape
  if (modelSmape !== undefined && baselineSmape !== undefined && modelSmape > baselineSmape) {
    return { label: 'Below baseline', cls: 'badge-warn' }
  }
  const liveAcc = normAccuracy(r.live_accuracy?.directional_accuracy)
  if (liveAcc !== null && (r.live_accuracy?.n ?? 0) >= 10 && liveAcc < 0.5) {
    return { label: 'Drift risk', cls: 'badge-warn' }
  }
  return { label: 'Healthy', cls: 'badge-ok' }
}

export default function Models() {
  const { calendar } = useSettings()

  const models = useApi<unknown>('/models')
  const versions = useMemo(
    () => unwrapList<ModelVersion>(models.data, 'items', 'models', 'model_versions'),
    [models.data]
  )

  const perf = useApi<unknown>('/models/performance')
  const perfRows = useMemo(
    () => unwrapList<HorizonPerformance>(perf.data, 'horizons', 'items'),
    [perf.data]
  )
  const lastRun = unwrapField<TrainingRun>(perf.data, 'last_training_run')

  const activeVersions = versions.filter((v) => v.active)
  const lastTrainedAt =
    lastRun?.finished_at ??
    lastRun?.started_at ??
    (activeVersions.length > 0
      ? activeVersions
          .map((v) => v.trained_at)
          .sort()
          .slice(-1)[0]
      : undefined)

  if (models.loading && perf.loading) return <Loading label="Loading model registry…" />

  const warningRows = perfRows.filter((r) => (r.warnings?.length ?? 0) > 0 || r.degraded === true)

  return (
    <div className="page-body">
      <h2 className="page-title">Models</h2>

      {models.error && <ErrorMessage message={models.error} onRetry={models.reload} />}
      {perf.error && <ErrorMessage message={perf.error} onRetry={perf.reload} />}

      <div className="row wrap">
        <div className="kv">
          <span className="muted">Last training</span>
          <span className="mono">
            {lastTrainedAt ? `${formatDateTime(lastTrainedAt, calendar)} (${relativeTime(lastTrainedAt)})` : '—'}
          </span>
        </div>
        {lastRun?.status && (
          <span className={`badge ${lastRun.status === 'success' || lastRun.status === 'completed' ? 'badge-ok' : 'badge-warn'}`}>
            {lastRun.status}
          </span>
        )}
      </div>

      {warningRows.map((r) => (
        <div key={`warn-${r.horizon}`} className="callout callout-warn">
          <strong>{HORIZON_LABELS[r.horizon] ?? r.horizon}:</strong>{' '}
          {r.degraded === true && 'model flagged as degraded. '}
          {(r.warnings ?? []).join(' ')}
        </div>
      ))}

      <div className="card">
        <div className="card-title">Active model per horizon — metrics vs naive baseline</div>
        {perf.loading ? (
          <Loading label="Loading performance…" />
        ) : perfRows.length === 0 ? (
          <EmptyState
            title="No performance data"
            hint="Run training first; only models that beat the naive baseline are activated."
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Horizon</th>
                  <th>Model</th>
                  <th className="num">sMAPE</th>
                  <th className="num">sMAPE (naive)</th>
                  <th className="num">MAE</th>
                  <th className="num">Dir. accuracy</th>
                  <th className="num">Live accuracy</th>
                  <th>Health</th>
                </tr>
              </thead>
              <tbody>
                {perfRows.map((r) => {
                  const health = healthOf(r)
                  const dirAcc = normAccuracy(r.metrics?.directional_accuracy)
                  const liveAcc = normAccuracy(r.live_accuracy?.directional_accuracy)
                  const beatsBaseline =
                    r.metrics?.smape !== undefined &&
                    r.baseline?.smape !== undefined &&
                    r.metrics.smape <= r.baseline.smape
                  return (
                    <tr key={r.horizon}>
                      <td>{HORIZON_LABELS[r.horizon] ?? r.horizon}</td>
                      <td className="mono">
                        {r.model_name}
                        {r.version ? ` (${r.version})` : ''}
                      </td>
                      <td className={`num mono ${beatsBaseline ? 'pos' : ''}`}>
                        {fmtMetric(r.metrics?.smape)}
                      </td>
                      <td className="num mono muted">{fmtMetric(r.baseline?.smape)}</td>
                      <td className="num mono">{fmtMetric(r.metrics?.mae, 0)}</td>
                      <td className="num mono">
                        {dirAcc !== null ? formatPct(dirAcc * 100, { sign: false, digits: 1 }) : '—'}
                      </td>
                      <td className="num mono">
                        {liveAcc !== null
                          ? `${formatPct(liveAcc * 100, { sign: false, digits: 1 })} (n=${r.live_accuracy?.n ?? 0})`
                          : '—'}
                      </td>
                      <td>
                        <span className={`badge ${health.cls}`}>{health.label}</span>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card">
        <div className="card-title">Model versions (active &amp; recent)</div>
        {models.loading ? (
          <Loading label="Loading models…" />
        ) : versions.length === 0 ? (
          <EmptyState title="No model versions" hint="Trigger a training run to populate the registry." />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Horizon</th>
                  <th>Model</th>
                  <th>Version</th>
                  <th>Trained</th>
                  <th className="num">sMAPE</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {versions.map((v) => (
                  <tr key={v.id}>
                    <td>{HORIZON_LABELS[v.horizon] ?? v.horizon}</td>
                    <td className="mono">{v.model_name}</td>
                    <td className="mono">{v.version}</td>
                    <td className="muted small">{formatDateTime(v.trained_at, calendar)}</td>
                    <td className="num mono">{fmtMetric(v.metrics?.smape)}</td>
                    <td>
                      {v.active ? (
                        <span className="badge badge-ok">active</span>
                      ) : (
                        <span className="badge badge-off">archived</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
