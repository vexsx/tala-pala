import { useMemo, useState } from 'react'
import { useApi } from '../hooks/useApi'
import {
  HORIZONS,
  HORIZON_LABELS,
  type Horizon,
  type Prediction,
  type PriceHistoryResponse
} from '../api/types'
import { unwrapList } from '../lib/unwrap'
import { useSettings } from '../lib/settings'
import {
  confidencePct,
  formatDateTime,
  formatPct,
  formatToman,
  pctClass,
  shortDate,
  formatTime
} from '../lib/format'
import { buildForecastChartData, type ForecastChartPoint } from '../lib/forecastChart'
import PriceChart, { type ChartPoint } from '../components/PriceChart'
import GaugeBar from '../components/GaugeBar'
import Loading from '../components/Loading'
import ErrorMessage from '../components/ErrorMessage'
import EmptyState from '../components/EmptyState'

const DAY_MS = 24 * 60 * 60 * 1000

const DIRECTION_ARROWS: Record<string, string> = { up: '▲', down: '▼', flat: '▶' }

/** The live API emits point_forecast/predicted_at; older payloads used predicted_value/created_at. */
function normalizePrediction(p: Prediction): Prediction {
  return {
    ...p,
    predicted_value: typeof p.point_forecast === 'number' ? p.point_forecast : p.predicted_value,
    created_at: p.created_at ?? p.predicted_at ?? ''
  }
}

function sortByHorizon(predictions: Prediction[]): Prediction[] {
  return predictions
    .slice()
    .map(normalizePrediction)
    .sort((a, b) => HORIZONS.indexOf(a.horizon) - HORIZONS.indexOf(b.horizon))
}

export default function Forecast() {
  const { unit, calendar } = useSettings()

  const latest = useApi<unknown>('/predictions')
  const predictions = useMemo(
    () => sortByHorizon(unwrapList<Prediction>(latest.data, 'predictions', 'items')),
    [latest.data]
  )

  const [selected, setSelected] = useState<Horizon | null>(null)
  const active: Horizon | null = selected ?? predictions[0]?.horizon ?? null
  const activePrediction = predictions.find((p) => p.horizon === active) ?? null

  const history = useApi<unknown>(active ? `/predictions/${active}?limit=50` : null)
  const historyItems = useMemo(
    () => unwrapList<Prediction>(history.data, 'items', 'predictions').map(normalizePrediction),
    [history.data]
  )

  const pricePath = useMemo(() => {
    const from = new Date(Date.now() - 14 * DAY_MS).toISOString()
    return `/prices/history?symbol=IR_GOLD_18K&interval=hourly&page_size=500&from=${encodeURIComponent(from)}`
  }, [])
  const priceHistory = useApi<PriceHistoryResponse>(pricePath)

  const toChartPoints = useMemo(
    () =>
      (merged: ForecastChartPoint[]): ChartPoint[] =>
        merged.map((p) => {
          const d = new Date(p.t)
          return {
            label: `${shortDate(d, calendar)} ${formatTime(d)}`,
            actual: p.actual,
            forecast: p.forecast,
            band: p.band
          }
        }),
    [calendar]
  )

  const chartData: ChartPoint[] = useMemo(
    () =>
      toChartPoints(
        buildForecastChartData(
          priceHistory.data?.items ?? [],
          activePrediction ? [activePrediction] : []
        )
      ),
    [priceHistory.data, activePrediction, toChartPoints]
  )

  const allHorizonsData: ChartPoint[] = useMemo(
    () => toChartPoints(buildForecastChartData(priceHistory.data?.items ?? [], predictions)),
    [priceHistory.data, predictions, toChartPoints]
  )

  const fmt = (v: number) => formatToman(v, unit)

  if (latest.loading) return <Loading label="Loading predictions…" />
  if (latest.error) return <ErrorMessage message={latest.error} onRetry={latest.reload} />
  if (predictions.length === 0) {
    return (
      <EmptyState
        title="No predictions yet"
        hint="Predictions appear once models have been trained and the hourly prediction job has run."
      />
    )
  }

  return (
    <div className="page-body">
      <h2 className="page-title">Forecast</h2>

      <div className="card">
        <div className="card-title">All horizons — combined forecast fan (IR_GOLD_18K)</div>
        {priceHistory.loading ? (
          <Loading label="Loading price history…" />
        ) : priceHistory.error ? (
          <ErrorMessage message={priceHistory.error} onRetry={priceHistory.reload} />
        ) : allHorizonsData.length > 0 ? (
          <>
            <PriceChart data={allHorizonsData} format={fmt} height={300} />
            <div className="chart-legend">
              <span className="chart-legend-item">
                <span className="legend-swatch legend-actual" aria-hidden="true" /> history
              </span>
              <span className="chart-legend-item">
                <span className="legend-swatch legend-forecast" aria-hidden="true" /> forecast (band =
                90% interval)
              </span>
            </div>
          </>
        ) : (
          <EmptyState title="No price history" />
        )}
      </div>

      <div className="tabs" role="tablist">
        {predictions.map((p) => (
          <button
            key={p.horizon}
            type="button"
            role="tab"
            aria-selected={p.horizon === active}
            className={`tab ${p.horizon === active ? 'active' : ''}`}
            onClick={() => setSelected(p.horizon)}
          >
            {HORIZON_LABELS[p.horizon] ?? p.horizon}
          </button>
        ))}
      </div>

      {activePrediction && (
        <>
          {(activePrediction.warnings ?? []).map((w, i) => (
            <div key={i} className="callout callout-warn">
              {w}
            </div>
          ))}

          <div className="grid">
            <div className="card stat-card">
              <div className="card-title">
                Predicted value · {HORIZON_LABELS[activePrediction.horizon]}
              </div>
              <div className="stat-value">
                <span className={`direction-arrow ${pctClass(activePrediction.expected_change_pct)}`}>
                  {DIRECTION_ARROWS[activePrediction.direction] ?? '•'}
                </span>{' '}
                {formatToman(activePrediction.predicted_value, unit)}
              </div>
              <div className={`delta ${pctClass(activePrediction.expected_change_pct)}`}>
                {formatPct(activePrediction.expected_change_pct)} expected
              </div>
              <div className="stat-sub">
                <div className="kv">
                  <span className="muted">Interval</span>
                  <span className="mono">
                    {formatToman(activePrediction.lower_bound, unit, false)} –{' '}
                    {formatToman(activePrediction.upper_bound, unit)}
                  </span>
                </div>
                <div className="kv">
                  <span className="muted">Target time</span>
                  <span className="mono">
                    {formatDateTime(activePrediction.target_time, calendar)}
                  </span>
                </div>
                <div className="kv">
                  <span className="muted">Model</span>
                  <span className="mono">
                    {activePrediction.model_name}
                    {activePrediction.model_version ? ` (${activePrediction.model_version})` : ''}
                  </span>
                </div>
              </div>
              <GaugeBar value={confidencePct(activePrediction.confidence)} label="Confidence" />
            </div>

            <div className="card">
              <div className="card-title">Drivers</div>
              {activePrediction.drivers && activePrediction.drivers.length > 0 ? (
                <ul className="driver-list">
                  {activePrediction.drivers.map((d) => (
                    <li key={d.name} className="driver-row" title={d.description}>
                      <span className="driver-name">{d.name.replace(/_/g, ' ')}</span>
                      <span className={`mono ${pctClass(d.impact)}`}>
                        {d.impact > 0 ? '+' : ''}
                        {d.impact.toFixed(2)}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <EmptyState title="No driver breakdown" hint="This model did not report feature attributions." />
              )}
            </div>
          </div>

          <div className="card">
            <div className="card-title">Recent actuals &amp; forecast band (IR_GOLD_18K)</div>
            {priceHistory.loading ? (
              <Loading label="Loading price history…" />
            ) : priceHistory.error ? (
              <ErrorMessage message={priceHistory.error} onRetry={priceHistory.reload} />
            ) : chartData.length > 0 ? (
              <PriceChart data={chartData} format={fmt} height={320} />
            ) : (
              <EmptyState title="No price history" />
            )}
          </div>
        </>
      )}

      <div className="card">
        <div className="card-title">Forecast vs actual · {active ? HORIZON_LABELS[active] : ''}</div>
        {history.loading ? (
          <Loading label="Loading history…" />
        ) : history.error ? (
          <ErrorMessage message={history.error} onRetry={history.reload} />
        ) : historyItems.length === 0 ? (
          <EmptyState title="No prediction history for this horizon" />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Made at</th>
                  <th>Target</th>
                  <th className="num">Predicted</th>
                  <th className="num">Actual</th>
                  <th className="num">Error</th>
                  <th>Direction</th>
                </tr>
              </thead>
              <tbody>
                {historyItems.map((p) => {
                  const errPct =
                    p.actual_value !== null && p.actual_value !== 0
                      ? ((p.predicted_value - p.actual_value) / p.actual_value) * 100
                      : null
                  const actualDir =
                    p.actual_value !== null
                      ? p.actual_value > p.base_value
                        ? 'up'
                        : p.actual_value < p.base_value
                          ? 'down'
                          : 'flat'
                      : null
                  const hit = actualDir !== null ? actualDir === p.direction : null
                  return (
                    <tr key={p.id}>
                      <td>{formatDateTime(p.created_at, calendar)}</td>
                      <td>{formatDateTime(p.target_time, calendar)}</td>
                      <td className="num mono">{formatToman(p.predicted_value, unit, false)}</td>
                      <td className="num mono">
                        {p.actual_value !== null ? formatToman(p.actual_value, unit, false) : '—'}
                      </td>
                      <td className={`num mono ${errPct === null ? '' : Math.abs(errPct) > 2 ? 'neg' : ''}`}>
                        {errPct !== null ? formatPct(errPct) : '—'}
                      </td>
                      <td>
                        <span className={pctClass(p.direction === 'up' ? 1 : p.direction === 'down' ? -1 : 0)}>
                          {DIRECTION_ARROWS[p.direction] ?? '•'} {p.direction}
                        </span>{' '}
                        {hit !== null && (
                          <span className={`badge ${hit ? 'badge-ok' : 'badge-bad'}`}>
                            {hit ? 'hit' : 'miss'}
                          </span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
