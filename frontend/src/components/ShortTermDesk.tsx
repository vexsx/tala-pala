import { useMemo } from 'react'
import { useApi } from '../hooks/useApi'
import type { MarketSummary, Prediction } from '../api/types'
import { unwrapList } from '../lib/unwrap'
import { useSettings } from '../lib/settings'
import { confidencePct, formatPct, formatToman, relativeTime } from '../lib/format'
import {
  ADVISOR_HORIZON_LABELS,
  TILT_LABELS,
  planRows,
  resolvePolicy,
  tiltBadgeClass,
  tiltReason,
  type PlanRow,
  type Tilt
} from '../lib/advice'
import { normalizePrediction } from '../lib/forecastChart'
import Loading from './Loading'
import ErrorMessage from './ErrorMessage'
import EmptyState from './EmptyState'

/**
 * Short-term desk: the near horizons (<= 3 days) collapsed into one answer.
 *
 * The Action planner already lists every horizon, but reading a verdict off it
 * means comparing seven rows against a cost bar in your head. This card does
 * that arithmetic once, for the short end only, and says what it implies.
 *
 * It invents no rules. Thresholds, the cost basis and the confidence bar all
 * come from the server's decision policy (app/core/costs.py), the same values
 * the Advisor and Action planner use, so the three surfaces cannot disagree.
 * Where the horizons themselves disagree, that disagreement is the headline
 * rather than something averaged away.
 */

/** Horizons a short-term decision actually depends on. */
const SHORT_HORIZONS = new Set(['1h', '4h', 'eod', '1d', '3d'])

type Verdict = 'buy' | 'sell' | 'wait' | 'mixed' | 'no-call'

const VERDICT_LABEL: Record<Verdict, string> = {
  buy: 'LEANS BUY',
  sell: 'LEANS SELL',
  wait: 'WAIT',
  mixed: 'MIXED',
  'no-call': 'NO CALL'
}

const VERDICT_CLASS: Record<Verdict, string> = {
  buy: 'badge-ok',
  sell: 'badge-bad',
  wait: 'badge-warn',
  mixed: 'badge-off',
  'no-call': 'badge-off'
}

interface Consensus {
  verdict: Verdict
  buy: PlanRow[]
  sell: PlanRow[]
  wait: PlanRow[]
  unclear: PlanRow[]
  stale: PlanRow[]
}

/**
 * Collapse the short horizons into one verdict.
 *
 * Deliberately conservative: a directional call needs the short end to agree
 * with itself. One horizon leaning buy while another leans sell is reported as
 * MIXED, not resolved by majority — a split short end is information, and
 * hiding it behind a single arrow would overstate what the models know.
 */
export function shortTermConsensus(rows: PlanRow[]): Consensus {
  const by = (t: Tilt) => rows.filter((r) => r.tilt === t)
  const buy = by('favors-buying')
  const sell = by('favors-selling')
  const wait = by('favors-waiting')
  const unclear = by('unclear')
  const stale = rows.filter((r) => !r.dataFresh)

  let verdict: Verdict
  if (rows.length === 0 || stale.length === rows.length) {
    verdict = 'no-call'
  } else if (buy.length > 0 && sell.length > 0) {
    // Opposing directional calls at the short end: report the split.
    verdict = 'mixed'
  } else if (buy.length > 0) {
    verdict = 'buy'
  } else if (sell.length > 0) {
    verdict = 'sell'
  } else if (wait.length > 0) {
    // Nothing clears the bar and at least one horizon says so explicitly.
    verdict = 'wait'
  } else {
    // Only 'unclear' rows: moves clear the cost but confidence does not.
    verdict = 'mixed'
  }
  return { verdict, buy, sell, wait, unclear, stale }
}

/** One-sentence justification, always naming the cost bar it was judged against. */
export function verdictSentence(c: Consensus, costPct: number): string {
  const cost = `${costPct.toFixed(2)}% round-trip cost`
  const list = (rows: PlanRow[]) =>
    rows.map((r) => (ADVISOR_HORIZON_LABELS[r.horizon] ?? r.horizon).toLowerCase()).join(', ')
  switch (c.verdict) {
    case 'buy':
      return `Over ${list(c.buy)} the projected gain clears the ${cost} with enough confidence to matter.`
    case 'sell':
      return `Over ${list(c.sell)} the projected drop clears the sell bar (the exit leg of the ${cost}).`
    case 'mixed':
      return c.buy.length > 0 && c.sell.length > 0
        ? `The short end disagrees with itself — ${list(c.buy)} lean buy while ${list(c.sell)} lean sell. A split short end is a reason to wait, not to pick a side.`
        : `No horizon clears the ${cost} decisively; the short end is inconclusive rather than pointing anywhere.`
    case 'no-call':
      return 'Short-horizon inputs are stale, so no short-term call is made.'
    default:
      return `Every short-horizon move is smaller than the ${cost}, so trading it would cost more than it is projected to make.`
  }
}

export default function ShortTermDesk() {
  const { unit } = useSettings()
  const summary = useApi<MarketSummary>('/market/summary')
  const latest = useApi<unknown>('/predictions')

  const predictions = useMemo(
    () => unwrapList<Prediction>(latest.data, 'items', 'predictions').map(normalizePrediction),
    [latest.data]
  )

  const policy = resolvePolicy(
    (summary.data as { decision_policy?: Parameters<typeof resolvePolicy>[0] } | null)
      ?.decision_policy,
    summary.data?.trading_cost_pct
  )
  const currentPrice = summary.data?.current_18k?.value ?? null

  const rows = useMemo(
    () =>
      planRows(predictions, currentPrice, policy.costPct).filter((r) =>
        SHORT_HORIZONS.has(r.horizon)
      ),
    [predictions, currentPrice, policy.costPct]
  )
  const consensus = useMemo(() => shortTermConsensus(rows), [rows])
  const fmt = (v: number) => formatToman(v, unit)

  const loading = (summary.loading || latest.loading) && !summary.data && !latest.data
  const error = latest.error ?? summary.error

  return (
    <div className="card">
      <div className="row space-between">
        <div className="card-title">Short-term desk — next hours to 3 days</div>
        <span className={`badge ${VERDICT_CLASS[consensus.verdict]}`}>
          {VERDICT_LABEL[consensus.verdict]}
        </span>
      </div>

      {loading ? (
        <Loading label="Reading the short horizons…" />
      ) : error ? (
        <ErrorMessage message={error} onRetry={latest.error ? latest.reload : summary.reload} />
      ) : rows.length === 0 ? (
        <EmptyState
          title="NO SHORT-HORIZON FORECASTS"
          hint="Short-term rows appear once the 1h–3d horizons have produced predictions."
        />
      ) : (
        <>
          <p className="std-sentence">{verdictSentence(consensus, policy.costPct)}</p>

          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Horizon</th>
                  <th className="num">Projected</th>
                  <th className="num">Move</th>
                  <th className="num">Net of cost</th>
                  <th className="num">Confidence</th>
                  <th>Read</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const conf = confidencePct(
                    predictions.find((p) => p.horizon === r.horizon)?.confidence
                  )
                  const pred = predictions.find((p) => p.horizon === r.horizon)
                  return (
                    <tr key={r.horizon}>
                      <td>{ADVISOR_HORIZON_LABELS[r.horizon] ?? r.horizon}</td>
                      <td className="num mono">{fmt(r.forecast)}</td>
                      <td className={`num mono ${r.expectedChangePct >= 0 ? 'pos' : 'neg'}`}>
                        {formatPct(r.expectedChangePct)}
                      </td>
                      <td className={`num mono ${r.netPct >= 0 ? 'pos' : 'neg'}`}>
                        {formatPct(r.netPct)}
                      </td>
                      <td className="num mono">
                        {conf !== null ? formatPct(conf, { sign: false, digits: 0 }) : '—'}
                      </td>
                      <td>
                        <span
                          className={`badge ${tiltBadgeClass(r.tilt)}`}
                          title={pred ? tiltReason(pred, policy.costPct) : undefined}
                        >
                          {TILT_LABELS[r.tilt]}
                        </span>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          <p className="muted small std-basis">
            Judged against a {policy.costPct.toFixed(2)}% round-trip cost
            {policy.basis === 'observed_spread'
              ? ' (live dealer spread)'
              : ' (assumed — no recent spread observation)'}
            , buying above {policy.buyPct.toFixed(2)}% and selling below −
            {policy.sellPct.toFixed(2)}%, both needing {policy.minConf.toFixed(0)}% confidence.
            {consensus.stale.length > 0 &&
              ` ${consensus.stale.length} horizon(s) ran on stale inputs.`}
            {summary.data?.last_update
              ? ` Prices ${relativeTime(summary.data.last_update)}.`
              : ''}
          </p>

          <p className="muted small">
            Estimates for decision support, not financial advice — and short horizons are the
            noisiest part of the forecast.
          </p>
        </>
      )}
    </div>
  )
}
