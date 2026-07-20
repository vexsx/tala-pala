import { relativeTime } from '../lib/format'

export default function DataFreshness({
  timestamp,
  stale,
  staleMinutes = 30
}: {
  timestamp?: string | null
  /** Backend-computed staleness flag; overrides the age heuristic when true. */
  stale?: boolean
  staleMinutes?: number
}) {
  const ageMinutes = timestamp
    ? (Date.now() - new Date(timestamp).getTime()) / 60000
    : Number.POSITIVE_INFINITY
  const state =
    stale === true || ageMinutes > staleMinutes * 3
      ? 'bad'
      : ageMinutes > staleMinutes
        ? 'warn'
        : 'ok'
  return (
    <span className="freshness" title={timestamp ?? 'no data yet'}>
      <span className={`dot dot-${state}`} aria-hidden="true" />
      <span className="muted small">{relativeTime(timestamp)}</span>
      {stale === true && <span className="badge badge-warn">stale</span>}
    </span>
  )
}
