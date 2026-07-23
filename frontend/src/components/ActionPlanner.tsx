import { useMemo } from 'react'
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceDot,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from 'recharts'
import { useApi } from '../hooks/useApi'
import type {
  IndicatorsResponse,
  PortfolioSummary,
  Prediction,
  PriceHistoryItem,
  PriceHistoryResponse
} from '../api/types'
import { unwrapList } from '../lib/unwrap'
import { useSettings } from '../lib/settings'
import {
  formatCompact,
  formatCompactToman,
  formatGregorianDate,
  formatGrouped,
  formatJalaliDate,
  formatPct,
  formatTime,
  formatToman,
  pctClass,
  shortDate,
  type CalendarMode
} from '../lib/format'
import {
  ADVISOR_HORIZON_LABELS,
  ROUND_TRIP_COST_PCT,
  TILT_LABELS,
  bestWindows,
  changeOverDays,
  latestByHorizon,
  planRows,
  tiltBadgeClass,
  type BestWindow,
  type PlanRow
} from '../lib/advice'
import {
  buildForecastChartData,
  normalizePrediction,
  pointForecastOf
} from '../lib/forecastChart'
import { ChartTip } from './PriceChart'
import Loading from './Loading'
import EmptyState from './EmptyState'

const DAY_MS = 24 * 60 * 60 * 1000

/** Target date in both calendars, e.g. "1405/04/30 · 2026-07-21". */
function bothCalendars(input: string): string {
  const d = new Date(input)
  if (Number.isNaN(d.getTime())) return '—'
  return `${formatJalaliDate(d)} · ${formatGregorianDate(d)}`
}

// ---------- Chart data (18k history+forecast, XAU overlay indexed to 18k) ----------

interface PlannerPoint {
  t: number
  label: string
  actual?: number
  forecast?: number
  band?: [number, number]
  xau?: number
  xauForecast?: number
}

const dayOf = (t: number): number => Math.floor(t / DAY_MS)

function validSorted(items: PriceHistoryItem[]): Array<{ t: number; value: number }> {
  return items
    .map((it) => ({ t: Date.parse(it.observed_at), value: it.value }))
    .filter((it) => Number.isFinite(it.t) && Number.isFinite(it.value))
    .sort((a, b) => a.t - b.t)
}

/**
 * XAU→18k index factor from the first day both series cover:
 * 18k_value / xau_value on that day, so the overlay starts on the 18k line
 * and diverges only where relative performance differs. Null without overlap.
 */
function indexFactor(hist18k: PriceHistoryItem[], histXau: PriceHistoryItem[]): number | null {
  const a = validSorted(hist18k)
  const b = validSorted(histXau)
  if (a.length === 0 || b.length === 0) return null
  const xauByDay = new Map<number, number>()
  for (const it of b) xauByDay.set(dayOf(it.t), it.value)
  for (const it of a) {
    const x = xauByDay.get(dayOf(it.t))
    if (x !== undefined && x !== 0) return it.value / x
  }
  return null
}

function buildPlannerChartData(
  predictions: Prediction[],
  history: PriceHistoryItem[],
  xauPredictions: Prediction[],
  xauHistory: PriceHistoryItem[],
  calendar: CalendarMode,
  now: number
): { points: PlannerPoint[]; hasXauOverlay: boolean } {
  const base = buildForecastChartData(history, latestByHorizon(predictions))
  const rows = new Map<number, PlannerPoint>()
  for (const p of base) {
    rows.set(p.t, { t: p.t, label: '', actual: p.actual, forecast: p.forecast, band: p.band })
  }

  // Rows carrying an actual, keyed by day, so the XAU overlay lands on the
  // same tick as the 18k observation of that day.
  const rowTByDay = new Map<number, number>()
  for (const p of base) {
    if (p.actual !== undefined) rowTByDay.set(dayOf(p.t), p.t)
  }

  const factor = indexFactor(history, xauHistory)
  let hasXauOverlay = false
  if (factor !== null) {
    const xauSorted = validSorted(xauHistory)
    let lastXauRowT: number | null = null
    let lastXauIndexed: number | null = null
    for (const it of xauSorted) {
      const rt = rowTByDay.get(dayOf(it.t)) ?? it.t
      const row = rows.get(rt) ?? { t: rt, label: '' }
      row.xau = it.value * factor
      rows.set(rt, row)
      lastXauRowT = rt
      lastXauIndexed = it.value * factor
      hasXauOverlay = true
    }
    // Bridge the dashed XAU forecast continuation to the last XAU actual.
    if (lastXauRowT !== null && lastXauIndexed !== null) {
      const bridge = rows.get(lastXauRowT)
      if (bridge) bridge.xauForecast = lastXauIndexed
    }
    for (const p of latestByHorizon(xauPredictions)) {
      const t = Date.parse(p.target_time)
      const point = pointForecastOf(p)
      if (!Number.isFinite(t) || t <= now || point === null) continue
      const row = rows.get(t) ?? { t, label: '' }
      row.xauForecast = point * factor
      rows.set(t, row)
    }
  }

  const points = Array.from(rows.values()).sort((a, b) => a.t - b.t)
  const lastActualT = base.reduce((m, p) => (p.actual !== undefined ? Math.max(m, p.t) : m), 0)
  for (const p of points) {
    const d = new Date(p.t)
    p.label = p.t > lastActualT ? `${shortDate(d, calendar)} ${formatTime(d)}` : shortDate(d, calendar)
  }
  return { points, hasXauOverlay }
}

// ---------- Pure presentational planner ----------

export interface ActionPlannerProps {
  /** Latest 18k predictions (normalized or raw — both handled). */
  predictions: Prediction[]
  /** 18k daily history (~30d). */
  history: PriceHistoryItem[]
  xauPredictions?: Prediction[]
  xauHistory?: PriceHistoryItem[]
  usdHistory?: PriceHistoryItem[]
  currentPrice: number | null
  premiumPct?: number | null
  premiumAvg30d?: number | null
  corrXau20?: number | null
  fundsFlowPct?: number | null
  portfolio?: PortfolioSummary | null
  /** Round-trip cost basis for tilts (live dealer spread or fallback). */
  costPct?: number
  /** Fixed clock for tests. */
  now?: number
}

function windowText(
  w: BestWindow,
  kind: 'buy' | 'sell',
  fmt: (v: number) => string
): string {
  const label = kind === 'buy' ? 'Likely best buy window' : 'Likely best sell window'
  if (w.when === 'today') {
    const why =
      kind === 'buy'
        ? 'every forecast sits above today’s price'
        : 'every forecast sits below today’s price'
    return `${label}: today — ${why}.`
  }
  const r = w.row
  return (
    `${label}: ${bothCalendars(r.targetTime)} ` +
    `(${ADVISOR_HORIZON_LABELS[r.horizon] ?? r.horizon}), projected ${fmt(r.forecast)}.`
  )
}

export function ActionPlanner({
  predictions,
  history,
  xauPredictions = [],
  xauHistory = [],
  usdHistory = [],
  currentPrice,
  premiumPct = null,
  premiumAvg30d = null,
  corrXau20 = null,
  fundsFlowPct = null,
  portfolio = null,
  costPct = ROUND_TRIP_COST_PCT,
  now
}: ActionPlannerProps) {
  const { unit, calendar } = useSettings()
  const clock = now ?? Date.now()
  const fmt = (v: number) => formatToman(v, unit)

  const rows: PlanRow[] = useMemo(
    () => planRows(predictions, currentPrice, costPct, clock),
    [predictions, currentPrice, costPct, clock]
  )
  const windows = useMemo(() => bestWindows(rows, currentPrice), [rows, currentPrice])

  const chart = useMemo(
    () => buildPlannerChartData(predictions, history, xauPredictions, xauHistory, calendar, clock),
    [predictions, history, xauPredictions, xauHistory, calendar, clock]
  )

  const buyDot =
    windows && windows.buy.when === 'horizon'
      ? chart.points.find((p) => windows.buy.when === 'horizon' && p.t === windows.buy.row.t)
      : undefined
  const sellDot =
    windows && windows.sell.when === 'horizon'
      ? chart.points.find((p) => windows.sell.when === 'horizon' && p.t === windows.sell.row.t)
      : undefined

  const xau7d = changeOverDays(xauHistory, 7)
  const usd7d = changeOverDays(usdHistory, 7)
  const premiumDelta =
    premiumPct !== null && premiumAvg30d !== null ? premiumPct - premiumAvg30d : null

  const held = portfolio !== null && portfolio.total_grams_18k_equivalent > 0 ? portfolio : null
  const sellRow: PlanRow | null =
    windows && windows.sell.when === 'horizon' ? windows.sell.row : null

  const contextItems: Array<{ key: string; arrow: string; cls: string; label: string; value: string; meaning: string }> = []
  const arrowFor = (v: number): string => (v > 0.005 ? '▲' : v < -0.005 ? '▼' : '▶')
  if (xau7d !== null) {
    contextItems.push({
      key: 'xau',
      arrow: arrowFor(xau7d),
      cls: pctClass(xau7d),
      label: 'Global gold · 7d',
      value: formatPct(xau7d),
      meaning: 'the global tailwind (or headwind) behind the local price'
    })
  }
  if (usd7d !== null) {
    contextItems.push({
      key: 'usd',
      arrow: arrowFor(usd7d),
      cls: pctClass(usd7d),
      label: 'USD/IRT · 7d',
      value: formatPct(usd7d),
      meaning: 'a weaker toman mechanically lifts the local gold price'
    })
  }
  if (premiumPct !== null) {
    contextItems.push({
      key: 'premium',
      arrow: premiumDelta !== null ? arrowFor(premiumDelta) : '▶',
      cls: premiumDelta !== null ? pctClass(premiumDelta) : 'flat',
      label: 'Premium now',
      value:
        premiumAvg30d !== null
          ? `${formatPct(premiumPct)} (30d avg ${formatPct(premiumAvg30d)})`
          : formatPct(premiumPct),
      meaning:
        premiumDelta !== null && premiumDelta > 0
          ? 'the local market is pricier than its recent norm vs global parity'
          : 'the local market is near or below its recent norm vs global parity'
    })
  }
  if (corrXau20 !== null) {
    contextItems.push({
      key: 'corr',
      arrow: arrowFor(corrXau20),
      cls: Math.abs(corrXau20) >= 0.5 ? 'pos' : 'flat',
      label: 'Corr. with XAU · 20d',
      value: corrXau20.toFixed(2),
      meaning:
        Math.abs(corrXau20) >= 0.5
          ? 'local gold is tracking global gold closely right now'
          : 'local gold is currently decoupled from global gold'
    })
  }
  if (fundsFlowPct !== null) {
    contextItems.push({
      key: 'flow',
      arrow: arrowFor(fundsFlowPct),
      cls: pctClass(fundsFlowPct),
      label: 'Gold-fund retail flow',
      value: formatPct(fundsFlowPct),
      meaning:
        fundsFlowPct > 0
          ? 'retail money is flowing into gold funds'
          : fundsFlowPct < 0
            ? 'retail money is flowing out of gold funds'
            : 'retail flows into gold funds are balanced'
    })
  }

  return (
    <section className="card action-planner" data-testid="action-planner">
      <div className="card-title">Action planner</div>

      {rows.length === 0 ? (
        <EmptyState
          title="No future forecasts yet"
          hint="The planner appears once the hourly prediction job has produced forward-looking horizons."
        />
      ) : (
        <>
          {chart.points.length >= 2 && (
            <>
              <div className="chart-box" style={{ height: 280 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={chart.points} margin={{ top: 16, right: 16, bottom: 4, left: 8 }}>
                    <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
                    <XAxis
                      dataKey="label"
                      tick={{ fill: 'var(--muted)', fontSize: 11 }}
                      minTickGap={28}
                      tickLine={false}
                      axisLine={{ stroke: 'var(--border)' }}
                    />
                    <YAxis
                      tick={{ fill: 'var(--muted)', fontSize: 11 }}
                      tickFormatter={(v: number) => formatCompactToman(v, unit)}
                      width={64}
                      domain={['auto', 'auto']}
                      tickLine={false}
                      axisLine={{ stroke: 'var(--border)' }}
                    />
                    <Tooltip content={<ChartTip format={fmt} />} />
                    <Area
                      type="monotone"
                      dataKey="band"
                      name="Interval"
                      fill="var(--band-fill)"
                      stroke="none"
                      isAnimationActive={false}
                      connectNulls
                      legendType="none"
                    />
                    <Line
                      type="monotone"
                      dataKey="actual"
                      name="18k actual"
                      stroke="var(--accent)"
                      strokeWidth={2}
                      dot={false}
                      isAnimationActive={false}
                      connectNulls
                    />
                    <Line
                      type="monotone"
                      dataKey="forecast"
                      name="18k forecast"
                      stroke="var(--info)"
                      strokeWidth={2}
                      strokeDasharray="6 4"
                      dot={{ r: 3 }}
                      isAnimationActive={false}
                      connectNulls
                    />
                    {chart.hasXauOverlay && (
                      <Line
                        type="monotone"
                        dataKey="xau"
                        name="Global gold (indexed)"
                        stroke="var(--muted)"
                        strokeWidth={1}
                        dot={false}
                        isAnimationActive={false}
                        connectNulls
                      />
                    )}
                    {chart.hasXauOverlay && (
                      <Line
                        type="monotone"
                        dataKey="xauForecast"
                        name="Global gold forecast (indexed)"
                        stroke="var(--muted)"
                        strokeWidth={1}
                        strokeDasharray="4 4"
                        dot={{ r: 2 }}
                        isAnimationActive={false}
                        connectNulls
                      />
                    )}
                    {buyDot && buyDot.forecast !== undefined && (
                      <ReferenceDot
                        x={buyDot.label}
                        y={buyDot.forecast}
                        r={5}
                        fill="var(--pos)"
                        stroke="var(--bg)"
                        label={{
                          value: 'likely best buy window',
                          position: 'bottom',
                          fill: 'var(--pos)',
                          fontSize: 11
                        }}
                      />
                    )}
                    {sellDot && sellDot.forecast !== undefined && (
                      <ReferenceDot
                        x={sellDot.label}
                        y={sellDot.forecast}
                        r={5}
                        fill="var(--neg)"
                        stroke="var(--bg)"
                        label={{
                          value: 'likely best sell window',
                          position: 'top',
                          fill: 'var(--neg)',
                          fontSize: 11
                        }}
                      />
                    )}
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
              <div className="chart-legend">
                <span className="chart-legend-item">
                  <span className="legend-swatch legend-actual" aria-hidden="true" /> 18k history
                </span>
                <span className="chart-legend-item">
                  <span className="legend-swatch legend-forecast" aria-hidden="true" /> 18k forecast
                  (band = 90% interval)
                </span>
                {chart.hasXauOverlay && (
                  <span className="chart-legend-item">
                    <span className="legend-swatch legend-xau" aria-hidden="true" /> global gold,
                    indexed for comparison
                  </span>
                )}
              </div>
            </>
          )}

          {windows && (
            <div className="planner-windows">
              <p className="planner-window pos">{windowText(windows.buy, 'buy', fmt)}</p>
              <p className="planner-window neg">{windowText(windows.sell, 'sell', fmt)}</p>
            </div>
          )}

          <div className="table-wrap">
            <table className="table planner-table">
              <thead>
                <tr>
                  <th>Timeframe</th>
                  <th>Target date</th>
                  <th className="num">Projected (90% band)</th>
                  <th className="num">Δ vs today</th>
                  <th className="num" title={`Model expected change minus the ${costPct.toFixed(2)}% round-trip cost${costPct !== ROUND_TRIP_COST_PCT ? ' (live dealer spread)' : ' (assumed)'}`}>
                    Net if buy today → sell then
                  </th>
                  <th>Tilt</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.horizon}>
                    <td>{ADVISOR_HORIZON_LABELS[r.horizon] ?? r.horizon}</td>
                    <td className="mono small">{bothCalendars(r.targetTime)}</td>
                    <td className="num mono">
                      {fmt(r.forecast)}
                      <div className="muted small">
                        {fmt(r.lower)} – {fmt(r.upper)}
                      </div>
                    </td>
                    <td className={`num mono ${pctClass(r.changeVsTodayPct)}`}>
                      {formatPct(r.changeVsTodayPct)}
                    </td>
                    <td className={`num mono ${pctClass(r.netPct)}`}>{formatPct(r.netPct)}</td>
                    <td>
                      <span className={`badge ${tiltBadgeClass(r.tilt)}`} title={r.tiltReason}>
                        {TILT_LABELS[r.tilt]}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <p className="muted small planner-tilt-note">
            How to read the tilt: <em>favors waiting</em> = the projected move is smaller than the{' '}
            {costPct.toFixed(2)}% round-trip cost (a 0.00% projection means the horizon&apos;s active
            model is &quot;naive&quot; — nothing beat it in validation, i.e. no short-term edge);{' '}
            <em>unclear</em> = the move clears the cost but confidence is below 55%. Hover a badge
            for that row&apos;s exact reason.
          </p>

          {held !== null && sellRow !== null && (
            <p className="planner-portfolio">
              You hold{' '}
              <span className="mono">{formatGrouped(held.total_grams_18k_equivalent, 2)} g</span>{' '}
              (avg <span className="mono">{formatToman(held.avg_price, unit)}</span>). At the likely
              best sell window ({bothCalendars(sellRow.targetTime)}) projected value ≈{' '}
              <span className="mono">
                {formatToman(held.total_grams_18k_equivalent * sellRow.lower, unit)} –{' '}
                {formatToman(held.total_grams_18k_equivalent * sellRow.upper, unit)}
              </span>{' '}
              → est. P/L{' '}
              <span className="mono">
                {formatToman(held.total_grams_18k_equivalent * sellRow.lower - held.invested, unit)}{' '}
                – {formatToman(held.total_grams_18k_equivalent * sellRow.upper - held.invested, unit)}
              </span>
              .
            </p>
          )}
          {held !== null && sellRow === null && windows?.sell.when === 'today' && (
            <p className="planner-portfolio">
              You hold{' '}
              <span className="mono">{formatGrouped(held.total_grams_18k_equivalent, 2)} g</span>{' '}
              (avg <span className="mono">{formatToman(held.avg_price, unit)}</span>). Every forecast
              sits below today&rsquo;s price, so the likely best sell window is today — current value
              ≈ <span className="mono">{formatToman(held.current_value, unit)}</span>, unrealized P/L{' '}
              <span className={`mono ${pctClass(held.unrealized_pnl)}`}>
                {formatToman(held.unrealized_pnl, unit)}
              </span>
              .
            </p>
          )}

          {contextItems.length > 0 && (
            <div className="context-strip">
              {contextItems.map((it) => (
                <div key={it.key} className="context-item">
                  <span className={`context-arrow ${it.cls}`} aria-hidden="true">
                    {it.arrow}
                  </span>
                  <span className="context-label muted">{it.label}</span>
                  <span className={`context-value mono ${it.cls}`}>{it.value}</span>
                  <span className="context-meaning muted small">{it.meaning}</span>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      <div className="advisor-footer muted small">
        Projections are model estimates with wide uncertainty — the &ldquo;best day&rdquo; shifts as
        new data arrives. Not financial advice.
      </div>
    </section>
  )
}

// ---------- Fetching container ----------

const HISTORY_30D = (symbol: string): string => {
  const from = new Date(Date.now() - 30 * DAY_MS).toISOString()
  return `/prices/history?symbol=${symbol}&interval=daily&page_size=500&from=${encodeURIComponent(from)}`
}

export interface ActionPlannerPanelProps {
  /** 18k predictions and daily history, lifted from Overview's existing fetches. */
  predictions: Prediction[]
  history: PriceHistoryItem[]
  /** True while the parent is still loading predictions/history. */
  loading?: boolean
  /** Round-trip cost basis for tilts (live dealer spread or fallback). */
  costPct?: number
  currentPrice: number | null
  premiumPct?: number | null
  premiumAvg30d?: number | null
  /** Retail net flow, lifted from Overview's /market/funds fetch. */
  fundsFlowPct?: number | null
  /** Portfolio lifted from the parent; null when unavailable (401 / empty). */
  portfolio?: PortfolioSummary | null
}

/**
 * Container: adds the XAUUSD forecast/history, USD/IRT history, and the
 * corr_xau_20 indicator on top of the props lifted from Overview. Every
 * secondary fetch degrades silently — the planner renders without overlays
 * and context facts it cannot source.
 */
export default function ActionPlannerPanel({
  predictions,
  history,
  loading = false,
  currentPrice,
  premiumPct = null,
  premiumAvg30d = null,
  fundsFlowPct = null,
  portfolio = null,
  costPct = ROUND_TRIP_COST_PCT
}: ActionPlannerPanelProps) {
  const xauLatest = useApi<unknown>('/predictions?symbol=XAUUSD')
  const xauPredictions = useMemo(
    () => unwrapList<Prediction>(xauLatest.data, 'items', 'predictions').map(normalizePrediction),
    [xauLatest.data]
  )
  const xauHistoryPath = useMemo(() => HISTORY_30D('XAUUSD'), [])
  const usdHistoryPath = useMemo(() => HISTORY_30D('USD_IRT'), [])
  const xauHistory = useApi<PriceHistoryResponse>(xauHistoryPath)
  const usdHistory = useApi<PriceHistoryResponse>(usdHistoryPath)
  const indicators = useApi<IndicatorsResponse>('/market/indicators?days=30')

  if (loading) {
    return (
      <section className="card action-planner" data-testid="action-planner">
        <div className="card-title">Action planner</div>
        <Loading label="Loading forecasts…" />
      </section>
    )
  }

  return (
    <ActionPlanner
      predictions={predictions}
      history={history}
      xauPredictions={xauPredictions}
      xauHistory={xauHistory.data?.items ?? []}
      usdHistory={usdHistory.data?.items ?? []}
      currentPrice={currentPrice}
      premiumPct={premiumPct}
      premiumAvg30d={premiumAvg30d}
      corrXau20={indicators.data?.corr_xau_20 ?? null}
      fundsFlowPct={fundsFlowPct}
      portfolio={portfolio}
      costPct={costPct}
    />
  )
}
