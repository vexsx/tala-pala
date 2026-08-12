import { Fragment } from 'react'
import { useApi } from '../hooks/useApi'
import type {
  TrendPerformanceBasis,
  TrendPerformanceItem,
  TrendPerformanceResponse,
  TrendSymbol
} from '../api/types'
import { useSettings } from '../lib/settings'
import { formatDateTime, formatPct, pctClass, type CalendarMode } from '../lib/format'
import ErrorMessage from './ErrorMessage'
import EmptyState from './EmptyState'

/**
 * Trend alignment — the measured track record.
 *
 * A REPLAY of the indicator over past candles: what it would have said at each
 * close, and what price did over the next day. Nobody traded any of it, so the
 * section says so above the table rather than letting a reader take these rows
 * for realised P&L.
 *
 * Like TrendAlignment.tsx this file only displays what
 * GET /market/trend-alignment/performance returned. There is no return, mean or
 * hit-rate arithmetic below — src/test/TrendPerformance.test.tsx pins that
 * against the source, so a "small helper" can never turn into a second,
 * disagreeing backtest that quietly contradicts the job.
 *
 * The one thing this section must get right is that its rows are not all the
 * same measurement. A 'daily_only' row is the 1D leg alone; a 'full_mtf' row is
 * the real three-timeframe alignment. Averaging them in your head is the
 * mistake the badge, the row tint and the per-row note exist to prevent.
 */

const PATH = '/market/trend-alignment/performance?symbol='

/**
 * First instant with sub-daily observations. Before it `prices` holds one tick
 * per day, so the 4H and 1H legs of the alignment simply do not exist that far
 * back and the longer windows can only be measured on the daily leg. Stated in
 * the preamble because it is the reason the table has two kinds of row at all.
 */
const INTRADAY_FROM = '2026-07-20'

/**
 * Fewest bars this section will quote a rate or a conditional mean over.
 *
 * Twenty is a floor, not a significance test: at twenty bars one bar moves a hit
 * rate by five points, and the interval around a 60% rate still comfortably
 * contains a coin flip. Printing "60.0%" there would read as a measurement of
 * the indicator instead of as noise, so the cell says how thin it is and shows
 * no percentage at all. The conditional forward returns are held to the same
 * floor: they are means over the SAME handful of bars, and a mean of four
 * observations is no more readable than a rate over four.
 */
const MIN_BARS_FOR_RATE = 20

/** Columns in the table — the per-row note spans all of them. */
const TABLE_COLUMNS = 8

/**
 * Hit rates arrive as a fraction in [0,1] (migration 0021). Intl's percent style
 * performs the unit change, so the component never multiplies a statistic.
 * No decimals on purpose: at the 20-bar floor above, one bar is worth five
 * points, so a tenth of a percent would be invented precision.
 */
const HIT_RATE_FORMAT = new Intl.NumberFormat('en-US', {
  style: 'percent',
  maximumFractionDigits: 0
})

/**
 * Every basis carries a WORD, not just a tint: a reader skimming the table has
 * to see that a daily-only row answers a different question before they compare
 * its numbers with the row above.
 */
const BASIS_LABEL: Record<TrendPerformanceBasis, string> = {
  full_mtf: 'FULL 1D+4H+1H',
  daily_only: 'DAILY LEG ONLY'
}

const BASIS_BADGE: Record<TrendPerformanceBasis, string> = {
  full_mtf: 'badge-info',
  daily_only: 'badge-warn'
}

const BASIS_TITLE: Record<TrendPerformanceBasis, string> = {
  full_mtf:
    'The real three-timeframe alignment — 1D, 4H and 1H agreeing — replayed at every 1H close.',
  daily_only:
    'The daily leg alone: price against its three moving averages on daily candles. This is NOT the multi-timeframe alignment and must not be read as one — the 4H and 1H legs have no history this far back.'
}

/**
 * Duplicated from TrendAlignment.tsx rather than imported: that file belongs to
 * the indicator itself and this section must not reach into it.
 */
const SYMBOL_TITLE: Record<string, string> = {
  IR_GOLD_18K: '18k gold',
  XAUUSD: 'XAU/USD'
}

/** One skeleton bar per window the endpoint serves (90, 60, 30, 14). */
const SKELETON_ROWS = ['a', 'b', 'c', 'd']

export interface TrendPerformanceProps {
  /** Only IR_GOLD_18K and XAUUSD are served; anything else is a 400. */
  symbol?: TrendSymbol
}

function symbolTitle(symbol: string): string {
  return SYMBOL_TITLE[symbol] ?? symbol
}

/**
 * Basis lookups go through helpers with a fallback: if the server ever adds a
 * third basis, an unknown row must still announce its own name rather than
 * render blank and read as if it were the row above it.
 */
function basisLabel(basis: TrendPerformanceBasis): string {
  return BASIS_LABEL[basis] ?? String(basis).toUpperCase()
}

function basisBadge(basis: TrendPerformanceBasis): string {
  return BASIS_BADGE[basis] ?? 'badge-off'
}

function basisTitle(basis: TrendPerformanceBasis): string {
  return BASIS_TITLE[basis] ?? 'Basis reported by the server; this build does not know it.'
}

/** One accessible sentence describing the whole section, for screen readers. */
function ariaSummary(
  symbol: TrendSymbol,
  served: string,
  loading: boolean,
  error: string | null,
  count: number
): string {
  const head = `Trend alignment track record, ${symbolTitle(served || symbol)}`
  if (loading) return `${head}: loading`
  if (error !== null) return `${head}: unavailable`
  if (count === 0) return `${head}: not computed yet`
  return `${head}: ${count} replayed windows`
}

function PerformanceSkeleton() {
  return (
    <div
      className="tp-skeleton"
      role="status"
      aria-live="polite"
      aria-label="Loading trend alignment track record"
    >
      {SKELETON_ROWS.map((row) => (
        <span key={row} className="tp-skeleton-bar" />
      ))}
    </div>
  )
}

/**
 * One statistic cell. Three outcomes, deliberately different to look at:
 *
 *   null            — an em dash and, in the tooltip, why. Never 0.0%: "the
 *                     alignment was never bearish here" and "it was bearish and
 *                     paid nothing" are different facts.
 *   too few bars    — the marker, never a percentage. See MIN_BARS_FOR_RATE.
 *   otherwise       — exactly the number the API measured.
 */
function Stat({
  value,
  bars,
  kind,
  absentReason
}: {
  value: number | null
  /** Bars this statistic was measured over — its denominator, from the API. */
  bars: number
  kind: 'return' | 'rate'
  /** Why there is no number, shown when value is null. */
  absentReason: string
}) {
  if (value === null) {
    return (
      <span className="tp-absent" title={absentReason}>
        —
      </span>
    )
  }
  if (bars < MIN_BARS_FOR_RATE) {
    return (
      <span
        className="tp-thin"
        title={`Measured over ${bars} bars, under the ${MIN_BARS_FOR_RATE}-bar floor this section will quote a percentage over. The number exists but reading it as a rate would overstate it.`}
      >
        too few ({bars} bars)
      </span>
    )
  }
  // Only a return carries a sign worth colouring, and it carries its own +/−
  // besides. A hit rate is a magnitude: 40% is not "negative", and tinting
  // every non-zero rate green would read as approval of a coin flip.
  return kind === 'rate' ? (
    <span className="tp-rate" title={`Measured over ${bars} bars.`}>
      {HIT_RATE_FORMAT.format(value)}
    </span>
  ) : (
    <span className={pctClass(value)} title={`Measured over ${bars} bars.`}>
      {formatPct(value, { digits: 2 })}
    </span>
  )
}

/**
 * The row's own limitation, spelled out under it. Per row because the answer
 * differs per window: the same table holds a 14-day full-alignment row and a
 * 90-day daily-only row, and only the note explains why.
 */
function NoteRow({ item, calendar }: { item: TrendPerformanceItem; calendar: CalendarMode }) {
  return (
    // Same basis class as its statistics row: the tint runs down the pair, so
    // the note is unmistakably a qualification of the row above it.
    <tr
      className={`tp-note-row tp-basis-${item.basis}`}
      data-testid={`tp-note-${item.window_days}`}
    >
      <td colSpan={TABLE_COLUMNS} className="muted small">
        <span className={`tp-note-basis badge ${basisBadge(item.basis)}`}>
          {basisLabel(item.basis)}
        </span>{' '}
        {item.note ? item.note : 'The job recorded no note for this window.'}{' '}
        <span className="tp-note-dates">
          Replayed {formatDateTime(item.evaluated_from, calendar)} to{' '}
          {formatDateTime(item.evaluated_to, calendar)} (Tehran); computed{' '}
          {formatDateTime(item.computed_at, calendar)}.
        </span>
      </td>
    </tr>
  )
}

export default function TrendPerformance({ symbol = 'IR_GOLD_18K' }: TrendPerformanceProps) {
  const { calendar } = useSettings()
  const res = useApi<TrendPerformanceResponse>(`${PATH}${encodeURIComponent(symbol)}`, [symbol])
  // The skeleton is first-load only; a later refresh must not blank the rows.
  const loading = res.loading && res.data === null
  const items = res.data?.items ?? []
  const served = res.data?.symbol ?? symbol

  return (
    <section
      className="card tp-card"
      aria-label={ariaSummary(symbol, served, loading, res.error, items.length)}
      data-testid="trend-performance"
    >
      <div className="row space-between trend-head">
        <div className="card-title">TREND ALIGNMENT — TRACK RECORD, {symbolTitle(served)}</div>
      </div>

      <p className="muted small tp-preamble">
        This is a replay of the indicator over past candles — what it would have said at each close
        and what price did next — not a realised trading record, and windows longer than the
        intraday history fall back to the daily leg alone because 4H and 1H candles only begin on{' '}
        {INTRADAY_FROM}.
      </p>

      {loading ? (
        <PerformanceSkeleton />
      ) : res.error !== null ? (
        // The section keeps its frame on failure: a section that vanishes reads
        // as "this indicator has no track record", which is a different claim.
        <div className="trend-error">
          <div className="trend-state-title">TRACK RECORD UNAVAILABLE</div>
          <ErrorMessage message={res.error} onRetry={res.reload} />
        </div>
      ) : items.length === 0 ? (
        <EmptyState
          title="NOT COMPUTED YET"
          hint="The track record is a scheduled replay of past candles; it appears here once that job has run for this symbol."
        />
      ) : (
        <>
          <div className="table-wrap">
            <table className="table tp-table">
              <thead>
                <tr>
                  <th>Window</th>
                  <th>Basis</th>
                  <th className="num" title="Bars whose forward day was fully covered by data — the denominator of every rate in this row.">
                    Samples
                  </th>
                  <th className="num">Fwd return · bullish</th>
                  <th className="num">Fwd return · bearish</th>
                  <th className="num" title="Mean forward return over every bar in the window, whatever the indicator said — what simply holding would have paid.">
                    Baseline (all bars)
                  </th>
                  <th className="num">Hit rate · bullish</th>
                  <th className="num">Hit rate · bearish</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  // Fragment keyed per window: the statistics row and the note
                  // that qualifies it are one unit and must never be separated.
                  <Fragment key={item.window_days}>
                    <tr
                      className={`tp-row tp-basis-${item.basis}`}
                      data-testid={`tp-row-${item.window_days}`}
                    >
                      <td className="mono tp-window">{item.window_days}d</td>
                      <td>
                        <span
                          className={`badge tp-basis-badge ${basisBadge(item.basis)}`}
                          title={basisTitle(item.basis)}
                        >
                          {basisLabel(item.basis)}
                        </span>
                      </td>
                      <td
                        className="num mono"
                        title={`${item.bullish_bars} bullish, ${item.bearish_bars} bearish and ${item.unaligned_bars} unaligned bars, over ${item.bullish_episodes} bullish and ${item.bearish_episodes} bearish episodes. Bars are not independent trades.`}
                      >
                        {item.samples}
                      </td>
                      <td className="num mono">
                        <Stat
                          value={item.fwd_return_bullish_pct}
                          bars={item.bullish_bars}
                          kind="return"
                          absentReason={
                            item.bullish_bars === 0
                              ? 'The alignment was never bullish inside this window, so there was nothing to measure.'
                              : 'Not measured for this window.'
                          }
                        />
                      </td>
                      <td className="num mono">
                        <Stat
                          value={item.fwd_return_bearish_pct}
                          bars={item.bearish_bars}
                          kind="return"
                          absentReason={
                            item.bearish_bars === 0
                              ? 'The alignment was never bearish inside this window, so there was nothing to measure.'
                              : 'Not measured for this window.'
                          }
                        />
                      </td>
                      <td className="num mono tp-baseline">
                        <Stat
                          value={item.fwd_return_baseline_pct}
                          bars={item.samples}
                          kind="return"
                          absentReason="No bar in this window had a complete forward day, so there is nothing to compare the conditional returns against."
                        />
                      </td>
                      <td className="num mono">
                        <Stat
                          value={item.hit_rate_bullish}
                          bars={item.bullish_bars}
                          kind="rate"
                          absentReason={
                            item.bullish_bars === 0
                              ? 'The alignment was never bullish inside this window — a hit rate over no bars is not a zero.'
                              : 'Not measured for this window.'
                          }
                        />
                      </td>
                      <td className="num mono">
                        <Stat
                          value={item.hit_rate_bearish}
                          bars={item.bearish_bars}
                          kind="rate"
                          absentReason={
                            item.bearish_bars === 0
                              ? 'The alignment was never bearish inside this window — a hit rate over no bars is not a zero.'
                              : 'Not measured for this window.'
                          }
                        />
                      </td>
                    </tr>
                    <NoteRow item={item} calendar={calendar} />
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>

          <p className="muted small trend-note">
            Forward returns are the mean move over the next day from each bar's close, in percent;
            hit rates count a bullish bar as right when price rose and a bearish bar as right when
            it fell. Both are served by the API — this page performs no return or hit-rate
            arithmetic of its own. Rows on different bases are different measurements and must not
            be averaged together.
          </p>
        </>
      )}
    </section>
  )
}
