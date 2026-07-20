import { useApi } from '../hooks/useApi'
import {
  HORIZONS,
  HORIZON_LABELS,
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
  formatGrouped,
  formatPct,
  formatToman,
  pctClass
} from '../lib/format'
import SignalBadge from './SignalBadge'
import GaugeBar from './GaugeBar'
import Loading from './Loading'

const DISCLAIMER = 'Decision support only — not financial advice.'

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
}

/** Presentational advisor panel — pure props, unit-testable without fetch mocks. */
export function AdvisorCard({
  signal,
  predictions,
  portfolio,
  currentPrice,
  premiumPct = null
}: AdvisorCardProps) {
  const { unit, calendar } = useSettings()

  if (signal === null || signal.data_fresh !== true) {
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

      <div className="advisor-footer muted small">{DISCLAIMER}</div>
    </section>
  )
}

export interface AdvisorPanelProps {
  signal: SignalSummary | null
  predictions: Prediction[]
  currentPrice: number | null
  premiumPct?: number | null
  /** True while the parent is still loading the market summary. */
  loading?: boolean
}

/**
 * Thin container: fetches the portfolio (which may 401 or be empty — both
 * degrade to "no holdings") and delegates rendering to AdvisorCard.
 */
export default function AdvisorPanel({
  signal,
  predictions,
  currentPrice,
  premiumPct = null,
  loading = false
}: AdvisorPanelProps) {
  const portfolio = useApi<PortfolioResponse>('/portfolio')

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
      portfolio={portfolio.data}
      currentPrice={currentPrice}
      premiumPct={premiumPct}
    />
  )
}
