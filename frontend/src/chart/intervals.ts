import type { CandleCoverage } from '../api/types'

/**
 * Every timeframe the candles API accepts. The toolbar, the prefs store and the
 * support check all read this one table so a timeframe can never exist in the
 * UI without the server agreeing it exists.
 */
export type IntervalId =
  | '5m'
  | '10m'
  | '15m'
  | '20m'
  | '30m'
  | '45m'
  | '1h'
  | '2h'
  | '3h'
  | '4h'
  | '6h'
  | '8h'
  | '12h'
  | '1d'
  | '2d'
  | '3d'
  | '1w'

export type IntervalGroup = 'minutes' | 'hours' | 'days'

export interface IntervalDef {
  id: IntervalId
  /** Toolbar text — hours and up are upper-cased the way a trader reads them. */
  label: string
  seconds: number
  group: IntervalGroup
  /** Buckets the first page asks for; also the default history span. */
  defaultBars: number
}

const DAY = 86_400

/**
 * defaultBars is deliberately modest for the fine timeframes: the real intraday
 * history is only ~23 days deep, so asking for thousands of 5m buckets would
 * just return everything there is and make the first paint slower.
 */
export const INTERVALS: IntervalDef[] = [
  { id: '5m', label: '5m', seconds: 300, group: 'minutes', defaultBars: 480 },
  { id: '10m', label: '10m', seconds: 600, group: 'minutes', defaultBars: 480 },
  { id: '15m', label: '15m', seconds: 900, group: 'minutes', defaultBars: 480 },
  { id: '20m', label: '20m', seconds: 1_200, group: 'minutes', defaultBars: 400 },
  { id: '30m', label: '30m', seconds: 1_800, group: 'minutes', defaultBars: 400 },
  { id: '45m', label: '45m', seconds: 2_700, group: 'minutes', defaultBars: 400 },
  { id: '1h', label: '1H', seconds: 3_600, group: 'hours', defaultBars: 500 },
  { id: '2h', label: '2H', seconds: 7_200, group: 'hours', defaultBars: 400 },
  { id: '3h', label: '3H', seconds: 10_800, group: 'hours', defaultBars: 400 },
  { id: '4h', label: '4H', seconds: 14_400, group: 'hours', defaultBars: 400 },
  { id: '6h', label: '6H', seconds: 21_600, group: 'hours', defaultBars: 400 },
  { id: '8h', label: '8H', seconds: 28_800, group: 'hours', defaultBars: 400 },
  { id: '12h', label: '12H', seconds: 43_200, group: 'hours', defaultBars: 400 },
  { id: '1d', label: '1D', seconds: DAY, group: 'days', defaultBars: 500 },
  { id: '2d', label: '2D', seconds: 2 * DAY, group: 'days', defaultBars: 400 },
  { id: '3d', label: '3D', seconds: 3 * DAY, group: 'days', defaultBars: 400 },
  { id: '1w', label: '1W', seconds: 7 * DAY, group: 'days', defaultBars: 300 }
]

const BY_ID = new Map<IntervalId, IntervalDef>(INTERVALS.map((d) => [d.id, d]))

/** The strip that is always visible. Everything else lives behind "Custom". */
export const PRESET_INTERVALS: IntervalId[] = ['15m', '30m', '1h', '4h', '1d', '1w']

export const CUSTOM_INTERVALS: IntervalId[] = INTERVALS.map((d) => d.id).filter(
  (id) => !PRESET_INTERVALS.includes(id)
)

/**
 * Where the chart lands when nothing is stored and where it retreats to when a
 * stored timeframe stops being servable. 1d is the only interval this data
 * source has always been able to answer.
 */
export const DEFAULT_INTERVAL: IntervalId = '1d'
export const FALLBACK_INTERVAL: IntervalId = '1d'

/** The API still answers to the pre-rewrite names; accept them on the way in. */
const LEGACY_ALIASES: Record<string, IntervalId> = { hourly: '1h', daily: '1d' }

/** Parse a stored/queried timeframe. Returns null for anything unrecognised. */
export function parseInterval(raw: string | null | undefined): IntervalId | null {
  if (!raw) return null
  const key = raw.trim().toLowerCase()
  if (BY_ID.has(key as IntervalId)) return key as IntervalId
  return LEGACY_ALIASES[key] ?? null
}

export function intervalDef(id: IntervalId): IntervalDef {
  const def = BY_ID.get(id)
  if (!def) throw new Error(`unknown interval: ${id}`)
  return def
}

export function intervalSeconds(id: IntervalId): number {
  return intervalDef(id).seconds
}

export function intervalLabel(id: IntervalId): string {
  return intervalDef(id).label
}

/** Sub-daily timeframes need sub-daily observations to mean anything. */
export function isIntraday(id: IntervalId): boolean {
  return intervalSeconds(id) < DAY
}

export function defaultBars(id: IntervalId): number {
  return intervalDef(id).defaultBars
}

/** Default history span, in seconds, for a first page of this timeframe. */
export function defaultHistorySeconds(id: IntervalId): number {
  const def = intervalDef(id)
  return def.seconds * def.defaultBars
}

/** Plain-English duration used inside the refusal reasons. */
export function humanSeconds(seconds: number): string {
  if (seconds % (7 * DAY) === 0) {
    const n = seconds / (7 * DAY)
    return n === 1 ? 'weekly' : `${n}-week`
  }
  if (seconds % DAY === 0) {
    const n = seconds / DAY
    return n === 1 ? 'daily' : `${n}-day`
  }
  if (seconds % 3_600 === 0) {
    const n = seconds / 3_600
    return `${n}-hour`
  }
  const minutes = Math.round(seconds / 60)
  return `${minutes}-minute`
}

export type IntervalSupport = { ok: true } | { ok: false; reason: string }

const OK: IntervalSupport = { ok: true }

/**
 * Can the server actually answer this timeframe for this symbol?
 *
 * Coverage may be missing while the API rolls out; an unknown coverage is
 * treated as permissive so the chart degrades to "try it and show the error"
 * rather than refusing every timeframe.
 */
export function isSupported(
  id: IntervalId,
  coverage: CandleCoverage | null | undefined
): IntervalSupport {
  if (!coverage) return OK
  const def = intervalDef(id)

  // Physical reasons first: they explain *why*, where the server's list can only
  // say "no".
  const base = coverage.base_granularity_seconds
  if (base !== null && base !== undefined && def.seconds < base) {
    return {
      ok: false,
      reason: `${def.label} is finer than the ${humanSeconds(base)} source data.`
    }
  }
  if (isIntraday(id) && !coverage.intraday_from) {
    return {
      ok: false,
      reason: `${def.label} needs sub-daily observations; this symbol has only one per day.`
    }
  }

  const listed = coverage.supported_intervals
  if (Array.isArray(listed) && listed.length > 0 && !listed.includes(id)) {
    return {
      ok: false,
      reason: `${def.label} is not available for the current data source.`
    }
  }
  return OK
}

/** Every timeframe the given coverage allows, in table order. */
export function supportedIntervals(coverage: CandleCoverage | null | undefined): IntervalId[] {
  return INTERVALS.filter((d) => isSupported(d.id, coverage).ok).map((d) => d.id)
}

export interface ResolvedInterval {
  interval: IntervalId
  /** Why the request was not honoured; null when it was. Never substitute silently. */
  notice: string | null
}

/**
 * Pick the timeframe to actually render. A stored timeframe that the data no
 * longer supports falls back to 1d and carries the reason with it so the UI can
 * say what happened instead of quietly showing different bars.
 */
export function resolveInterval(
  wanted: IntervalId | null | undefined,
  coverage: CandleCoverage | null | undefined
): ResolvedInterval {
  const id = wanted ?? DEFAULT_INTERVAL
  const support = isSupported(id, coverage)
  if (support.ok) return { interval: id, notice: null }
  return {
    interval: FALLBACK_INTERVAL,
    notice: `${intervalLabel(id)} is unavailable — showing ${intervalLabel(
      FALLBACK_INTERVAL
    )} instead. ${support.reason}`
  }
}
