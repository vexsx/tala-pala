import { useMemo, useState } from 'react'
import { useApi } from '../hooks/useApi'
import { parseCustomDays, useCustomForecast } from '../hooks/useCustomForecast'
import {
  HORIZONS,
  HORIZON_LABELS,
  type CustomForecast,
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
  formatUsd,
  pctClass,
  shortDate,
  formatTime
} from '../lib/format'
import {
  buildForecastChartData,
  normalizePrediction,
  pointForecastOf,
  type ForecastChartPoint
} from '../lib/forecastChart'
import PriceChart, { type ChartPoint } from '../components/PriceChart'
import GaugeBar from '../components/GaugeBar'
import Loading from '../components/Loading'
import ErrorMessage from '../components/ErrorMessage'
import EmptyState from '../components/EmptyState'

const DAY_MS = 24 * 60 * 60 * 1000

const DIRECTION_ARROWS: Record<string, string> = { up: '▲', down: '▼', flat: '▶' }

const LEAN_LABELS: Record<CustomForecast['decision_lean'], string> = {
  buy: 'Buy lean',
  hold: 'Hold',
  sell: 'Sell lean'
}

/** "How many days ahead should I decide for?" — on-demand forecast card. */
function CustomHorizonCard({ fmt }: { fmt: (v: number) => string }) {
  const [days, setDays] = useState('7')
  const [inputError, setInputError] = useState<string | null>(null)
  const { result, loading, error: fetchError, run: runFetch } = useCustomForecast()
  const error = inputError ?? fetchError

  const run = () => {
    const n = parseCustomDays(days)
    if (n === null) {
      setInputError('Enter a whole number of days between 1 and 90.')
      return
    }
    setInputError(null)
    runFetch(n)
  }

  const leanClass =
    result?.decision_lean === 'buy' ? 'pos' : result?.decision_lean === 'sell' ? 'neg' : ''

  return (
    <div className="card">
      <div className="card-title">Decide over a custom horizon</div>
      <p className="muted small">
        Pick any number of days (1–90). Fast models are validated live at exactly that horizon —
        this takes a few seconds and is not stored.
      </p>
      <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', flexWrap: 'wrap' }}>
        <input
          type="number"
          min={1}
          max={90}
          step={1}
          value={days}
          onChange={(e) => setDays(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !loading) run()
          }}
          aria-label="Horizon in days"
          style={{ width: '6rem' }}
        />
        <span className="muted">days</span>
        <button type="button" className="btn btn-sm" onClick={run} disabled={loading}>
          {loading ? 'Computing…' : 'Compute forecast'}
        </button>
      </div>
      {error && <ErrorMessage message={error} />}
      {result && !loading && (
        <>
          {(result.warnings ?? []).map((w, i) => (
            <div key={i} className="callout callout-warn">
              {w}
            </div>
          ))}
          <div className="stat-value" style={{ marginTop: '0.75rem' }}>
            <span className={`direction-arrow ${pctClass(result.expected_change_pct)}`}>
              {DIRECTION_ARROWS[result.direction] ?? '•'}
            </span>{' '}
            {fmt(result.point_forecast)}
          </div>
          <div className={`delta ${pctClass(result.expected_change_pct)}`}>
            {formatPct(result.expected_change_pct)} expected over {result.horizon_days} day
            {result.horizon_days > 1 ? 's' : ''}
          </div>
          <div className="stat-sub">
            <div className="kv">
              <span className="muted">90% interval</span>
              <span className="mono">
                {fmt(result.lower_bound)} – {fmt(result.upper_bound)}
              </span>
            </div>
            <div className="kv">
              <span className="muted">Model</span>
              <span className="mono">
                {result.model_name}
                {result.beats_naive ? '' : ' (naive baseline won)'}
              </span>
            </div>
            <div className="kv">
              <span className="muted">Regime</span>
              <span className="mono">{result.regime}</span>
            </div>
            {result.provider_gap_pct !== null && (
              <div className="kv">
                <span className="muted">Provider gap now</span>
                <span className="mono">{formatPct(result.provider_gap_pct)}</span>
              </div>
            )}
          </div>
          <GaugeBar value={confidencePct(result.confidence)} label="Confidence" />
          {result.monte_carlo && (
            <div className="stat-sub">
              <div className="kv">
                <span className="muted">Simulated odds (bootstrap, {result.monte_carlo.n_paths} paths)</span>
                <span className="mono">
                  <span className="pos">{Math.round(result.monte_carlo.p_gain_over_cost * 100)}%</span>
                  {' beats costs · '}
                  <span className="neg">{Math.round(result.monte_carlo.p_loss_over_cost * 100)}%</span>
                  {' loses more than costs'}
                </span>
              </div>
              <div className="kv">
                <span className="muted">Simulated range (5–95%)</span>
                <span className="mono">
                  {formatPct(result.monte_carlo.sim_p05_pct)} … {formatPct(result.monte_carlo.sim_p95_pct)}
                </span>
              </div>
            </div>
          )}
          <div className={`callout ${result.decision_lean === 'hold' ? '' : 'callout-warn'}`}>
            <strong className={leanClass}>{LEAN_LABELS[result.decision_lean]}</strong> —{' '}
            {result.decision_note} Costs assumed ≈{result.round_trip_cost_pct}% round-trip. Not
            financial advice.
          </div>
        </>
      )}
    </div>
  )
}

function sortByHorizon(predictions: Prediction[]): Prediction[] {
  return predictions
    .slice()
    .map(normalizePrediction)
    .sort((a, b) => HORIZONS.indexOf(a.horizon) - HORIZONS.indexOf(b.horizon))
}

type ForecastSymbol = 'IR_GOLD_18K' | 'XAUUSD'

const FORECAST_SYMBOL_LABELS: Record<ForecastSymbol, string> = {
  IR_GOLD_18K: 'Tehran 18k (تومان)',
  XAUUSD: 'Global gold (XAU/USD)'
}

export default function Forecast() {
  const { unit, calendar } = useSettings()
  const [symbol, setSymbol] = useState<ForecastSymbol>('IR_GOLD_18K')

  const latest = useApi<unknown>(`/predictions?symbol=${symbol}`)
  const predictions = useMemo(
    () => sortByHorizon(unwrapList<Prediction>(latest.data, 'predictions', 'items')),
    [latest.data]
  )

  const [selected, setSelected] = useState<Horizon | null>(null)
  const active: Horizon | null = selected ?? predictions[0]?.horizon ?? null
  const activePrediction = predictions.find((p) => p.horizon === active) ?? null

  const history = useApi<unknown>(
    active ? `/predictions/${active}?symbol=${symbol}&limit=50` : null
  )
  const historyItems = useMemo(
    () => unwrapList<Prediction>(history.data, 'items', 'predictions').map(normalizePrediction),
    [history.data]
  )

  const pricePath = useMemo(() => {
    const from = new Date(Date.now() - 14 * DAY_MS).toISOString()
    return `/prices/history?symbol=${symbol}&interval=hourly&page_size=500&from=${encodeURIComponent(from)}`
  }, [symbol])
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

  const fmt = (v: number) => (symbol === 'XAUUSD' ? formatUsd(v) : formatToman(v, unit))

  if (latest.loading) return <Loading label="Loading predictions…" />
  if (latest.error) return <ErrorMessage message={latest.error} onRetry={latest.reload} />
  if (predictions.length === 0) {
    return (
      <div className="page-body">
        <h2 className="page-title">Forecast</h2>
        <div className="toggle-group" role="group" aria-label="Forecast symbol">
          {(Object.keys(FORECAST_SYMBOL_LABELS) as ForecastSymbol[]).map((s) => (
            <button
              key={s}
              type="button"
              className={symbol === s ? 'active' : ''}
              onClick={() => {
                setSymbol(s)
                setSelected(null)
              }}
            >
              {FORECAST_SYMBOL_LABELS[s]}
            </button>
          ))}
        </div>
        <EmptyState
          title={`No ${symbol} predictions yet`}
          hint="Predictions appear once models have been trained and the hourly prediction job has run."
        />
      </div>
    )
  }

  return (
    <div className="page-body">
      <h2 className="page-title">Forecast</h2>

      <div className="toggle-group" role="group" aria-label="Forecast symbol">
        {(Object.keys(FORECAST_SYMBOL_LABELS) as ForecastSymbol[]).map((s) => (
          <button
            key={s}
            type="button"
            className={symbol === s ? 'active' : ''}
            onClick={() => {
              setSymbol(s)
              setSelected(null)
            }}
          >
            {FORECAST_SYMBOL_LABELS[s]}
          </button>
        ))}
      </div>

      <div className="card">
        <div className="card-title">All horizons — combined forecast fan ({symbol})</div>
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

      {symbol === 'IR_GOLD_18K' && <CustomHorizonCard fmt={fmt} />}

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
                {(() => {
                  const point = pointForecastOf(activePrediction)
                  return point !== null ? fmt(point) : '—'
                })()}
              </div>
              <div className={`delta ${pctClass(activePrediction.expected_change_pct)}`}>
                {formatPct(activePrediction.expected_change_pct)} expected
              </div>
              <div className="stat-sub">
                <div className="kv">
                  <span className="muted">Interval</span>
                  <span className="mono">
                    {fmt(activePrediction.lower_bound)} –{' '}
                    {fmt(activePrediction.upper_bound)}
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
                  {activePrediction.drivers.map((d, i) => {
                    const label = d.factor ?? d.name ?? 'unknown'
                    const weight = d.importance ?? d.impact
                    const detail = d.note ?? d.description
                    return (
                      <li key={`${label}-${i}`} className="driver-row" title={detail}>
                        <span className="driver-name">{label.replace(/_/g, ' ')}</span>
                        <span className={`mono ${weight !== undefined ? pctClass(weight) : 'muted'}`}>
                          {weight !== undefined
                            ? `${weight > 0 ? '+' : ''}${weight.toFixed(2)}`
                            : detail ?? '—'}
                        </span>
                      </li>
                    )
                  })}
                </ul>
              ) : (
                <EmptyState title="No driver breakdown" hint="This model did not report feature attributions." />
              )}
            </div>
          </div>

          <div className="card">
            <div className="card-title">Recent actuals &amp; forecast band ({symbol})</div>
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
                  // normalizePrediction has filled predicted_value/base_value where
                  // derivable; guard anyway — old rows may lack them entirely.
                  const predicted = p.predicted_value
                  const base = p.base_value
                  const errPct =
                    predicted !== undefined && p.actual_value !== null && p.actual_value !== 0
                      ? ((predicted - p.actual_value) / p.actual_value) * 100
                      : null
                  const actualDir =
                    p.actual_value !== null && base !== undefined
                      ? p.actual_value > base
                        ? 'up'
                        : p.actual_value < base
                          ? 'down'
                          : 'flat'
                      : null
                  const hit = actualDir !== null ? actualDir === p.direction : null
                  return (
                    <tr key={p.id}>
                      <td>{formatDateTime(p.created_at, calendar)}</td>
                      <td>{formatDateTime(p.target_time, calendar)}</td>
                      <td className="num mono">{predicted !== undefined ? fmt(predicted) : '—'}</td>
                      <td className="num mono">
                        {p.actual_value !== null ? fmt(p.actual_value) : '—'}
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
