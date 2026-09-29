import { useEffect, useState } from 'react'
import type { CandleCoverage, ChartCandle, StockDataAge } from '../api/types'
import { useSettings } from '../lib/settings'
import { formatDate, formatTime, relativeTime } from '../lib/format'
import { intervalLabel, intervalSeconds, type IntervalId } from './intervals'
import { countSingleObservation, isHalted } from './useCandles'

export interface ChartStatusBarProps {
  /**
   * When the newest DATA was observed — for a tick series the symbol's newest
   * stored price (/prices/current `observed_at`). Never the candle response's
   * `as_of`: that is when the response was built, and read as freshness it
   * said "0s ago" beside a daily close three days old. Null is "not known
   * yet", which the bar says rather than guessing.
   */
  asOf: string | null
  interval: IntervalId
  candles: ChartCandle[]
  coverage: CandleCoverage | null
  /** Rendered only when the caller can actually name the provider. */
  source?: string | null
  /**
   * The server's staleness verdict for the same observation. When given it
   * DECIDES: the server knows each symbol's market hours, and the 3×-interval
   * heuristic below would call a TSE-session fund stale every weekend. The
   * heuristic is for a caller with no verdict.
   */
  stale?: boolean
  /**
   * A Tehran series' data_age. When present it decides freshness outright:
   * the server's 10-day bound, measured on the newest stored session. Null is
   * a Tehran series whose age has not arrived (loading, or refused): its age
   * is unknown, which is not the same as fresh. Omit it for a tick series.
   */
  dataAge?: StockDataAge | null
  /**
   * A registry series of one settled close per session (the API's cadence
   * 'daily_close'). Its age is the DATE of its newest close — "13h ago" read
   * as an outage beside a close as fresh as one can be — its staleness is the
   * server's four-day daily-close rule, and its bars are closes, not
   * single-observation buckets.
   */
  dailyClose?: boolean
}

/**
 * A 5m chart whose last bucket is an hour old is stale; a 1d chart is not. Three
 * buckets is the tolerance, floored at the 30 minutes the rest of the site uses
 * so a daily chart does not scream on a quiet afternoon.
 */
function isStale(asOf: string | null, interval: IntervalId, flag?: boolean): boolean {
  if (typeof flag === 'boolean') return flag
  if (!asOf) return false
  const ageSec = (Date.now() - new Date(asOf).getTime()) / 1000
  if (Number.isNaN(ageSec)) return false
  return ageSec > Math.max(intervalSeconds(interval) * 3, 1_800)
}

export function ChartStatusBar({
  asOf,
  interval,
  candles,
  coverage,
  source,
  stale,
  dataAge,
  dailyClose = false
}: ChartStatusBarProps) {
  const [now, setNow] = useState(() => Date.now())

  // The Tehran clock has to keep moving or it reads as another stale field.
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 30_000)
    return () => window.clearInterval(id)
  }, [])

  return (
    <div className="tchart-status">
      <span className="muted small">Tehran {formatTime(new Date(now))}</span>
      {dataAge ? (
        <SessionAge age={dataAge} />
      ) : dataAge === null ? (
        <span className="muted small">data age not known yet</span>
      ) : candles.length === 0 ? (
        <span className="muted small">no data</span>
      ) : asOf === null ? (
        <span className="muted small">age not known yet</span>
      ) : dailyClose ? (
        <DailyCloseAge asOf={asOf} stale={stale === true} />
      ) : (
        <TickAge asOf={asOf} stale={isStale(asOf, interval, stale)} />
      )}
      <span className="muted small">{intervalLabel(interval)}</span>
      {dataAge !== undefined ? (
        <SessionCounts candles={candles} />
      ) : dailyClose ? (
        <span className="muted small">
          {candles.length} daily close{candles.length === 1 ? '' : 's'}
        </span>
      ) : (
        <BucketCounts candles={candles} coverage={coverage} />
      )}
      {source && <span className="muted small mono">{source}</span>}
    </div>
  )
}

function TickAge({ asOf, stale }: { asOf: string | null; stale: boolean }) {
  return (
    <span className="freshness" title={asOf ?? 'no data yet'}>
      <span className={`dot dot-${stale ? 'bad' : 'ok'}`} aria-hidden="true" />
      <span className="muted small">{relativeTime(asOf)}</span>
      {stale && <span className="badge badge-bad">STALE</span>}
    </span>
  )
}

/**
 * A daily settled close is dated, not aged: it is stamped 23:00 UTC on its own
 * trade date, so that UTC date IS the session it closed, and "13h ago" beside
 * it read as an outage. STALE is the server's verdict (more than four days
 * old: a close is missing), never an hours heuristic.
 */
function DailyCloseAge({ asOf, stale }: { asOf: string; stale: boolean }) {
  const { calendar } = useSettings()
  return (
    <span className="freshness" title={`settled close stamped ${asOf}`}>
      <span className={`dot dot-${stale ? 'bad' : 'ok'}`} aria-hidden="true" />
      <span className="muted small">daily close of {formatDate(asOf.slice(0, 10), calendar)}</span>
      {stale && <span className="badge badge-bad">STALE</span>}
    </span>
  )
}

/**
 * The age of the newest stored SESSION, as the server measured it. A Tehran
 * series is refreshed by hand and the exchange shuts Thursday, Friday and for
 * Nowruz, so "3 × the interval" would flag every Saturday morning; the server's
 * bound is the one the Tehran market page and the alerts use.
 */
function SessionAge({ age }: { age: StockDataAge }) {
  const { calendar } = useSettings()
  const newest = age.newest_trade_date
  return (
    <span className="freshness" title={age.warning ?? age.note}>
      <span className={`dot dot-${age.stale ? 'bad' : 'ok'}`} aria-hidden="true" />
      <span className="muted small">
        {newest
          ? `last session ${formatDate(newest, calendar)} · ${age.age_days ?? '—'} day(s) old`
          : 'no stored session'}
      </span>
      {age.stale && <span className="badge badge-bad">STALE</span>}
    </span>
  )
}

function BucketCounts({ candles, coverage }: { candles: ChartCandle[]; coverage: CandleCoverage | null }) {
  const singles = countSingleObservation(candles)
  return (
    <>
      <span className="muted small">{candles.length} candles</span>
      {singles > 0 && (
        <span
          className="muted small tchart-singles"
          title={
            coverage?.note ??
            'These buckets hold one observation, so their high and low are that single price — not a traded range.'
          }
        >
          {singles} of {candles.length} bars are single-observation
        </span>
      )}
    </>
  )
}

/** Sessions, closed-market repeats and halts, counted out loud. */
function SessionCounts({ candles }: { candles: ChartCandle[] }) {
  let unchanged = 0
  let halted = 0
  for (const c of candles) {
    if (c.unchanged === true) unchanged++
    if (isHalted(c)) halted++
  }
  return (
    <>
      <span className="muted small">{candles.length} sessions</span>
      {unchanged > 0 && (
        <span
          className="muted small tchart-singles"
          title="These sessions repeat the previous close exactly — a closed market, or a sector nothing in traded. Drawn flat, left out of every indicator."
        >
          {unchanged} of {candles.length} repeat the previous close
        </span>
      )}
      {halted > 0 && (
        <span
          className="muted small tchart-singles"
          title="Nothing traded in these sessions: they are drawn as gaps and left out of every indicator."
        >
          {halted} of {candles.length} had no trade
        </span>
      )}
    </>
  )
}
