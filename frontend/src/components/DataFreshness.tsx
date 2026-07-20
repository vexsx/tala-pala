import type { MarketState } from '../api/types'
import { relativeTime } from '../lib/format'

export default function DataFreshness({
  timestamp,
  stale,
  marketState,
  staleMinutes = 30
}: {
  timestamp?: string | null
  /** Backend-computed staleness flag; overrides the age heuristic when true. */
  stale?: boolean
  /**
   * Backend market-hours state (Addendum 1). When 'closed' and the data is not
   * stale, the last observation is a valid last-session price: neutral dot plus
   * an amber "market closed" chip instead of the red stale badge.
   */
  marketState?: MarketState
  staleMinutes?: number
}) {
  const closedFresh = marketState === 'closed' && stale !== true
  const ageMinutes = timestamp
    ? (Date.now() - new Date(timestamp).getTime()) / 60000
    : Number.POSITIVE_INFINITY
  const state = closedFresh
    ? 'off'
    : stale === true || ageMinutes > staleMinutes * 3
      ? 'bad'
      : ageMinutes > staleMinutes
        ? 'warn'
        : 'ok'
  return (
    <span
      className="freshness"
      title={closedFresh ? 'last session price' : (timestamp ?? 'no data yet')}
    >
      <span className={`dot dot-${state}`} aria-hidden="true" />
      <span className="muted small">{relativeTime(timestamp)}</span>
      {stale === true ? (
        <span className="badge badge-bad">stale</span>
      ) : closedFresh ? (
        <span className="badge badge-warn" title="last session price">
          market closed
        </span>
      ) : null}
    </span>
  )
}
