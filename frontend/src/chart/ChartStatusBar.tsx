import { useEffect, useState } from 'react'
import type { CandleCoverage, ChartCandle } from '../api/types'
import { formatTime, relativeTime } from '../lib/format'
import { intervalLabel, intervalSeconds, type IntervalId } from './intervals'
import { countSingleObservation } from './useCandles'

export interface ChartStatusBarProps {
  asOf: string | null
  interval: IntervalId
  candles: ChartCandle[]
  coverage: CandleCoverage | null
  /** Rendered only when the caller can actually name the provider. */
  source?: string | null
  /** Server-side staleness flag; overrides the age heuristic when true. */
  stale?: boolean
}

/**
 * A 5m chart whose last bucket is an hour old is stale; a 1d chart is not. Three
 * buckets is the tolerance, floored at the 30 minutes the rest of the site uses
 * so a daily chart does not scream on a quiet afternoon.
 */
function isStale(asOf: string | null, interval: IntervalId, flag?: boolean): boolean {
  if (flag === true) return true
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
  stale
}: ChartStatusBarProps) {
  const [now, setNow] = useState(() => Date.now())

  // The Tehran clock has to keep moving or it reads as another stale field.
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 30_000)
    return () => window.clearInterval(id)
  }, [])

  const stale_ = isStale(asOf, interval, stale)
  const singles = countSingleObservation(candles)

  return (
    <div className="tchart-status">
      <span className="muted small">Tehran {formatTime(new Date(now))}</span>
      <span className="freshness" title={asOf ?? 'no data yet'}>
        <span className={`dot dot-${stale_ ? 'bad' : 'ok'}`} aria-hidden="true" />
        <span className="muted small">{relativeTime(asOf)}</span>
        {stale_ && <span className="badge badge-bad">STALE</span>}
      </span>
      <span className="muted small">{intervalLabel(interval)}</span>
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
      {source && <span className="muted small mono">{source}</span>}
    </div>
  )
}
