import { useEffect, useMemo, useRef, useState } from 'react'
import { useApi } from '../hooks/useApi'
import { useCustomForecast, parseCustomDays } from '../hooks/useCustomForecast'
import {
  HORIZONS,
  HORIZON_LABELS,
  type CustomForecast,
  type PortfolioResponse,
  type PortfolioSummary,
  type Prediction,
  type SignalLevel,
  type SignalSummary
} from '../api/types'
import { useSettings } from '../lib/settings'
import {
  confidencePct,
  formatDateTime,
  formatGregorianDate,
  formatGrouped,
  formatJalaliDate,
  formatPct,
  formatToman,
  pctClass
} from '../lib/format'
import {
  ADVISOR_HORIZON_KEY,
  ADVISOR_HORIZON_LABELS,
  ROUND_TRIP_COST_PCT,
  TILT_LABELS,
  defaultAdvisorHorizon,
  horizonTilt,
  latestByHorizon,
  parseAdvisorSelection,
  serializeAdvisorSelection,
  tiltBadgeClass,
  tiltReason,
  tiltPhrase,
  type AdvisorSelection
} from '../lib/advice'
import { pointForecastOf } from '../lib/forecastChart'
import SignalBadge from './SignalBadge'
import GaugeBar from './GaugeBar'
import Loading from './Loading'
import ErrorMessage from './ErrorMessage'

const DISCLAIMER = 'Decision support only — not financial advice.'

const DIRECTION_ARROWS: Record<string, string> = { up: '▲', down: '▼', flat: '▶' }

const LEAN_LABELS: Record<CustomForecast['decision_lean'], string> = {
  buy: 'Buy lean',
  hold: 'Hold',
  sell: 'Sell lean'
}

/** Detects the engine's "prices from last session (market closed)" note. */
function hasClosedMarketNote(signal: SignalSummary): boolean {
  const texts = [
    ...(signal.notes ?? []),
    ...(signal.risks ?? []),
    ...(signal.conflicting ?? [])
  ]
  return texts.some((t) => /market closed|last session/i.test(t))
}

type Bias = 'buy' | 'sell' | 'hold'

function biasOf(level: SignalLevel): Bias {
  if (level === 'strong_buy' || level === 'buy') return 'buy'
  if (level === 'strong_sell' || level === 'sell') return 'sell'
  return 'hold'
}

/** Highest-confidence prediction whose direction agrees with the signal (any, if none agree). */
function pickHeadlinePrediction(predictions: Prediction[], bias: Bias): Prediction | null {
  if (predictions.length === 0) return null
  const wanted = bias === 'buy' ? 'up' : bias === 'sell' ? 'down' : null
  const agreeing = wanted !== null ? predictions.filter((p) => p.direction === wanted) : []
  const pool = agreeing.length > 0 ? agreeing : predictions
  return pool
    .slice()
    .sort((a, b) => {
      const ca = confidencePct(a.confidence) ?? 0
      const cb = confidencePct(b.confidence) ?? 0
      if (cb !== ca) return cb - ca
      return HORIZONS.indexOf(a.horizon) - HORIZONS.indexOf(b.horizon)
    })[0]
}

function headlineSentence(bias: Bias, p: Prediction | null): string {
  if (bias === 'hold') {
    return 'Conditions do not clearly favor buying or selling — waiting is reasonable.'
  }
  const action = bias === 'buy' ? 'accumulating' : 'reducing exposure'
  if (p === null) {
    return `Conditions currently favor ${action}, based on the latest composite signal — outcomes remain uncertain.`
  }
  const pct = p.expected_change_pct
  const move =
    pct >= 0
      ? `a rise of ${formatPct(pct, { sign: false })}`
      : `a decline of ${formatPct(Math.abs(pct), { sign: false })}`
  const conf = confidencePct(p.confidence)
  const confText = conf !== null ? ` with ${Math.round(conf)}% confidence` : ''
  return (
    `Conditions currently favor ${action} — the models estimate ${move} ` +
    `over ${HORIZON_LABELS[p.horizon] ?? p.horizon}${confText}. Estimates, not guarantees.`
  )
}

function confidenceLabel(pct: number | null): string {
  if (pct === null) return 'unknown'
  if (pct < 45) return 'low — treat as noise'
  if (pct <= 70) return 'moderate'
  return 'high (by historical hit-rate)'
}

function readStoredSelection(): AdvisorSelection | null {
  try {
    return parseAdvisorSelection(window.localStorage.getItem(ADVISOR_HORIZON_KEY))
  } catch {
    return null
  }
}

function persistSelection(sel: AdvisorSelection): void {
  try {
    window.localStorage.setItem(ADVISOR_HORIZON_KEY, serializeAdvisorSelection(sel))
  } catch {
    // localStorage unavailable — selection just won't survive reloads
  }
}

/** Target date in both calendars, e.g. "1405/04/30 · 2026-07-21". */
function bothCalendars(input: string): string {
  const d = new Date(input)
  if (Number.isNaN(d.getTime())) return '—'
  return `${formatJalaliDate(d)} · ${formatGregorianDate(d)}`
}

// ---------- Timeframe selector (chips + detail block) ----------

function AdvisorTimeframe({
  predictions,
  costPct
}: {
  predictions: Prediction[]
  costPct: number
}) {
  const { unit } = useSettings()
  const available = useMemo(() => latestByHorizon(predictions), [predictions])
  const availableHorizons = useMemo(() => available.map((p) => p.horizon), [available])

  const [selection, setSelection] = useState<AdvisorSelection | null>(readStoredSelection)
  const [daysInput, setDaysInput] = useState<string>(() => {
    const stored = readStoredSelection()
    return stored?.kind === 'custom' ? String(stored.days) : '14'
  })

  const effective: AdvisorSelection | null = useMemo(() => {
    if (selection?.kind === 'custom') return selection
    if (selection?.kind === 'std' && availableHorizons.includes(selection.horizon)) return selection
    const def = defaultAdvisorHorizon(availableHorizons)
    return def !== null ? { kind: 'std', horizon: def } : null
  }, [selection, availableHorizons])

  const select = (sel: AdvisorSelection) => {
    setSelection(sel)
    persistSelection(sel)
  }

  const custom = useCustomForecast()
  const { run, runDebounced } = custom
  const lastRunDays = useRef<number | null>(null)
  useEffect(() => {
    if (effective?.kind !== 'custom') return
    const days = effective.days
    if (lastRunDays.current === days) return
    if (lastRunDays.current === null) run(days)
    else runDebounced(days)
    lastRunDays.current = days
  }, [effective, run, runDebounced])

  if (available.length === 0 && effective?.kind !== 'custom') return null

  const fmt = (v: number) => formatToman(v, unit)
  const activePrediction =
    effective?.kind === 'std'
      ? available.find((p) => p.horizon === effective.horizon) ?? null
      : null

  return (
    <div className="advisor-timeframe" data-testid="advisor-timeframe">
      <div className="chip-row" role="group" aria-label="Advisor timeframe">
        {available.map((p) => (
          <button
            key={p.horizon}
            type="button"
            className={`chip ${
              effective?.kind === 'std' && effective.horizon === p.horizon ? 'active' : ''
            }`}
            aria-pressed={effective?.kind === 'std' && effective.horizon === p.horizon}
            onClick={() => select({ kind: 'std', horizon: p.horizon })}
          >
            {ADVISOR_HORIZON_LABELS[p.horizon] ?? p.horizon}
          </button>
        ))}
        <button
          type="button"
          className={`chip ${effective?.kind === 'custom' ? 'active' : ''}`}
          aria-pressed={effective?.kind === 'custom'}
          onClick={() => {
            const days = parseCustomDays(daysInput) ?? 14
            setDaysInput(String(days))
            select({ kind: 'custom', days })
          }}
        >
          Custom…
        </button>
        {effective?.kind === 'custom' && (
          <span className="chip-input">
            <input
              type="number"
              min={1}
              max={90}
              step={1}
              value={daysInput}
              aria-label="Custom horizon in days"
              onChange={(e) => {
                setDaysInput(e.target.value)
                const n = parseCustomDays(e.target.value)
                if (n !== null) select({ kind: 'custom', days: n })
              }}
            />
            <span className="muted small">days (1–90)</span>
          </span>
        )}
      </div>

      {effective?.kind === 'std' && activePrediction && (
        <StandardTimeframeDetail prediction={activePrediction} fmt={fmt} costPct={costPct} />
      )}

      {effective?.kind === 'custom' && (
        <CustomTimeframeDetail
          days={effective.days}
          daysValid={parseCustomDays(daysInput) !== null}
          state={custom}
          fmt={fmt}
        />
      )}
    </div>
  )
}

function StandardTimeframeDetail({
  prediction: p,
  fmt,
  costPct
}: {
  prediction: Prediction
  fmt: (v: number) => string
  costPct: number
}) {
  const point = pointForecastOf(p)
  const tilt = horizonTilt(p, costPct)
  const label = ADVISOR_HORIZON_LABELS[p.horizon] ?? p.horizon
  return (
    <div className="advisor-detail" data-testid="advisor-timeframe-detail">
      <div className="advisor-detail-head">
        <span className="muted small">Selected timeframe · {label}</span>
        <span className={`badge ${tiltBadgeClass(tilt)}`} title={tiltReason(p, costPct)}>
          {TILT_LABELS[tilt]}
        </span>
      </div>
      <div className="advisor-detail-price">
        <span className={`direction-arrow ${pctClass(p.expected_change_pct)}`}>
          {DIRECTION_ARROWS[p.direction] ?? '•'}
        </span>{' '}
        <span className="mono">{point !== null ? fmt(point) : '—'}</span>{' '}
        <span className={`delta ${pctClass(p.expected_change_pct)}`}>
          {formatPct(p.expected_change_pct)}
        </span>
      </div>
      <div className="stat-sub">
        <div className="kv">
          <span className="muted">Target date</span>
          <span className="mono">{bothCalendars(p.target_time)}</span>
        </div>
        <div className="kv">
          <span className="muted">90% interval</span>
          <span className="mono">
            {fmt(p.lower_bound)} – {fmt(p.upper_bound)}
          </span>
        </div>
      </div>
      <GaugeBar value={confidencePct(p.confidence)} label="Confidence" />
      <p className="muted small advisor-tilt-sentence">
        Models project {formatPct(p.expected_change_pct)} by {label.toLowerCase()}; net of ~
        {costPct.toFixed(2)}% round-trip costs
        {costPct !== ROUND_TRIP_COST_PCT ? ' (live dealer spread)' : ''}, {tiltPhrase(tilt)}.
      </p>
    </div>
  )
}

function CustomTimeframeDetail({
  days,
  daysValid,
  state,
  fmt
}: {
  days: number
  daysValid: boolean
  state: ReturnType<typeof useCustomForecast>
  fmt: (v: number) => string
}) {
  const { result, loading, error, run } = state
  if (!daysValid) {
    return (
      <div className="advisor-detail" data-testid="advisor-timeframe-detail">
        <p className="muted small">Enter a whole number of days between 1 and 90.</p>
      </div>
    )
  }
  return (
    <div className="advisor-detail" data-testid="advisor-timeframe-detail">
      <div className="advisor-detail-head">
        <span className="muted small">
          Selected timeframe · custom, {days} day{days > 1 ? 's' : ''} ahead
        </span>
        {result && !loading && (
          <span
            className={`badge ${
              result.decision_lean === 'buy'
                ? 'badge-ok'
                : result.decision_lean === 'sell'
                  ? 'badge-bad'
                  : 'badge-off'
            }`}
          >
            {LEAN_LABELS[result.decision_lean]}
          </span>
        )}
      </div>
      {loading && <Loading label="Computing custom forecast — this can take a few seconds…" />}
      {error && !loading && <ErrorMessage message={error} onRetry={() => run(days)} />}
      {result && !loading && !error && (
        <>
          {result.warnings.map((w, i) => (
            <div key={i} className="callout callout-warn">
              {w}
            </div>
          ))}
          <div className="advisor-detail-price">
            <span className={`direction-arrow ${pctClass(result.expected_change_pct)}`}>
              {DIRECTION_ARROWS[result.direction] ?? '•'}
            </span>{' '}
            <span className="mono">{fmt(result.point_forecast)}</span>{' '}
            <span className={`delta ${pctClass(result.expected_change_pct)}`}>
              {formatPct(result.expected_change_pct)}
            </span>
          </div>
          <div className="stat-sub">
            <div className="kv">
              <span className="muted">90% interval</span>
              <span className="mono">
                {fmt(result.lower_bound)} – {fmt(result.upper_bound)}
              </span>
            </div>
            <div className="kv">
              <span className="muted">Model · regime</span>
              <span className="mono">
                {result.model_name}
                {result.beats_naive ? '' : ' (naive baseline won)'} · {result.regime}
              </span>
            </div>
          </div>
          <GaugeBar value={confidencePct(result.confidence)} label="Confidence" />
          {result.monte_carlo && (
            <div className="stat-sub">
              <div className="kv">
                <span className="muted">
                  Simulated odds ({result.monte_carlo.n_paths} paths)
                </span>
                <span className="mono">
                  {Math.round(result.monte_carlo.p_up * 100)}% up ·{' '}
                  <span className="pos">
                    {Math.round(result.monte_carlo.p_gain_over_cost * 100)}%
                  </span>{' '}
                  beat costs ·{' '}
                  <span className="neg">
                    {Math.round(result.monte_carlo.p_loss_over_cost * 100)}%
                  </span>{' '}
                  lose more than costs
                </span>
              </div>
              <div className="kv">
                <span className="muted">Simulated cone (5% / median / 95%)</span>
                <span className="mono">
                  {formatPct(result.monte_carlo.sim_p05_pct)} /{' '}
                  {formatPct(result.monte_carlo.sim_median_pct)} /{' '}
                  {formatPct(result.monte_carlo.sim_p95_pct)}
                </span>
              </div>
            </div>
          )}
          <div className={`callout ${result.decision_lean === 'hold' ? '' : 'callout-warn'}`}>
            <strong
              className={
                result.decision_lean === 'buy' ? 'pos' : result.decision_lean === 'sell' ? 'neg' : ''
              }
            >
              {LEAN_LABELS[result.decision_lean]}
            </strong>{' '}
            — {result.decision_note} (Model decision engine; assumed costs ≈
            {result.round_trip_cost_pct}% round-trip.)
          </div>
        </>
      )}
    </div>
  )
}

// ---------- Advisor card ----------

export interface AdvisorCardProps {
  signal: SignalSummary | null
  /** Latest prediction per horizon (from /predictions). */
  predictions: Prediction[]
  /** Portfolio summary; null when unavailable (401 / empty / error). */
  portfolio: PortfolioSummary | null
  /** Current 18k price in IRT, for the break-even comparison. */
  currentPrice: number | null
  /** Current premium over theoretical parity, in percent. */
  premiumPct?: number | null
  /** Round-trip cost basis for tilts (live dealer spread or fallback). */
  costPct?: number
}

/** Advisor panel body — props-driven except the on-demand custom forecast. */
export function AdvisorCard({
  signal,
  predictions,
  portfolio,
  currentPrice,
  premiumPct = null,
  costPct = ROUND_TRIP_COST_PCT
}: AdvisorCardProps) {
  const { unit, calendar } = useSettings()

  // Only stay silent when the signal is missing or explicitly marked unfresh;
  // during market closure the engine still emits signals from last-session data.
  if (signal === null || signal.data_fresh === false) {
    return (
      <section className="card advisor-card advisor-stale" data-testid="advisor-card">
        <div className="card-title">Advisor</div>
        <div className="row">
          <SignalBadge signal={null} />
          <span className="muted">Insufficient fresh data — no recommendation.</span>
        </div>
        <p className="muted small">
          The advisor stays silent until price feeds are fresh again; acting on stale inputs adds
          avoidable risk.
          {signal?.explanation ? ` ${signal.explanation}` : ''}
        </p>
        <div className="advisor-footer muted small">{DISCLAIMER}</div>
      </section>
    )
  }

  const bias = biasOf(signal.signal)
  const headlinePrediction = pickHeadlinePrediction(predictions, bias)
  const cPct = confidencePct(signal.confidence)
  const marketClosed = hasClosedMarketNote(signal)

  const supporting = (signal.supporting ?? []).slice(0, 4)
  const conflicting = (signal.conflicting ?? []).concat(signal.risks ?? []).slice(0, 5)

  const invalidation =
    signal.invalidation && signal.invalidation.trim().length > 0
      ? signal.invalidation
      : 'incoming data turns stale or fresh prices decisively contradict the current signal.'
  const reviewText = signal.review_at
    ? `Review by ${formatDateTime(signal.review_at, calendar)} (Tehran).`
    : 'Reviewed at the next signal refresh.'

  const held: PortfolioSummary | null =
    portfolio !== null && portfolio.total_grams_18k_equivalent > 0 ? portfolio : null
  const vsBreakEven =
    held !== null && currentPrice !== null && held.break_even_price > 0
      ? ((currentPrice - held.break_even_price) / held.break_even_price) * 100
      : null

  return (
    <section className="card card-accent advisor-card" data-testid="advisor-card">
      <div className="card-title">Advisor</div>

      <div className="advisor-headline">
        <SignalBadge signal={signal.signal} size="lg" />
        <p className="advisor-sentence">{headlineSentence(bias, headlinePrediction)}</p>
      </div>

      <AdvisorTimeframe predictions={predictions} costPct={costPct} />

      <div className="advisor-meters">
        <div className="advisor-meter">
          <GaugeBar value={cPct} label="Confidence" />
          <span className="muted small">Confidence is {confidenceLabel(cPct)}.</span>
        </div>
        <div className="advisor-meter">
          <GaugeBar value={signal.score} label="Score" />
          <span className="muted small">Composite score, 0 (sell) – 100 (buy).</span>
        </div>
      </div>

      {(supporting.length > 0 || conflicting.length > 0) ? (
        <div className="advisor-lists">
          <div className="advisor-list">
            <h4 className="advisor-list-title pos">Why</h4>
            {supporting.length > 0 ? (
              <ul>
                {supporting.map((f, i) => (
                  <li key={i}>{f}</li>
                ))}
              </ul>
            ) : (
              <p className="muted small">No clearly supporting factors were reported.</p>
            )}
          </div>
          <div className="advisor-list">
            <h4 className="advisor-list-title neg">But</h4>
            {conflicting.length > 0 ? (
              <ul>
                {conflicting.map((f, i) => (
                  <li key={i}>{f}</li>
                ))}
              </ul>
            ) : (
              <p className="muted small">No conflicting factors or notable risks were reported.</p>
            )}
          </div>
        </div>
      ) : (
        signal.explanation && <p className="muted">{signal.explanation}</p>
      )}

      <div className="callout callout-warn advisor-invalid">
        <strong>This view becomes invalid if:</strong> {invalidation} <span>{reviewText}</span>
      </div>

      {held !== null && (
        <div className="advisor-portfolio">
          <p>
            You hold <span className="mono">{formatGrouped(held.total_grams_18k_equivalent, 2)} g</span>{' '}
            (18k-equivalent) at an average of{' '}
            <span className="mono">{formatToman(held.avg_price, unit)}</span>
            {vsBreakEven !== null ? (
              <>
                ; the current price is{' '}
                <span className={`mono ${pctClass(vsBreakEven)}`}>{formatPct(vsBreakEven)}</span> vs
                your break-even.
              </>
            ) : (
              '.'
            )}
          </p>
          {bias === 'sell' && held.unrealized_pnl > 0 && (
            <p className="muted small">
              If you did reduce exposure near current prices, that would realize roughly{' '}
              <span className="mono">{formatToman(held.unrealized_pnl, unit)}</span> of unrealized
              gain — before dealer spread and fees.
            </p>
          )}
          {bias === 'buy' && premiumPct !== null && (
            <p className="muted small">
              Cost note: the local price currently carries a{' '}
              <span className="mono">{formatPct(premiumPct)}</span> premium over global parity — part
              of the effective cost of accumulating now.
            </p>
          )}
        </div>
      )}

      {marketClosed && (
        <p className="warn-text small advisor-closed-note">
          Assessment based on last session&rsquo;s closing prices.
        </p>
      )}
      <div className="advisor-footer muted small">{DISCLAIMER}</div>
    </section>
  )
}

export interface AdvisorPanelProps {
  signal: SignalSummary | null
  predictions: Prediction[]
  currentPrice: number | null
  premiumPct?: number | null
  /** Round-trip cost basis for tilts (live dealer spread or fallback). */
  costPct?: number
  /** True while the parent is still loading the market summary. */
  loading?: boolean
  /**
   * Portfolio lifted from the parent (pass null while unavailable). When the
   * prop is omitted entirely the panel fetches /portfolio itself.
   */
  portfolio?: PortfolioSummary | null
}

/**
 * Thin container: resolves the portfolio (own fetch unless lifted by the
 * parent; 401 / empty both degrade to "no holdings") and delegates rendering
 * to AdvisorCard.
 */
export default function AdvisorPanel({
  signal,
  predictions,
  currentPrice,
  premiumPct = null,
  costPct = ROUND_TRIP_COST_PCT,
  loading = false,
  portfolio
}: AdvisorPanelProps) {
  const fetched = useApi<PortfolioResponse>(portfolio === undefined ? '/portfolio' : null)
  const resolvedPortfolio = portfolio !== undefined ? portfolio : fetched.data

  if (loading && signal === null) {
    return (
      <section className="card advisor-card">
        <div className="card-title">Advisor</div>
        <Loading label="Evaluating market conditions…" />
      </section>
    )
  }

  return (
    <AdvisorCard
      signal={signal}
      predictions={predictions}
      portfolio={resolvedPortfolio}
      currentPrice={currentPrice}
      premiumPct={premiumPct}
      costPct={costPct}
    />
  )
}
