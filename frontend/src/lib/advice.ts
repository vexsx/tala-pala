import {
  HORIZONS,
  type Horizon,
  type Prediction,
  type PriceHistoryItem
} from '../api/types'
import { confidencePct } from './format'
import { pointForecastOf } from './forecastChart'

/**
 * Assumed round-trip trading cost (dealer fee + bid/ask spread estimate), in
 * percent. Display-only heuristic used to express forecasts net of costs —
 * it never executes or prices anything.
 */
export const ROUND_TRIP_COST_PCT = 1.5

const DAY_MS = 24 * 60 * 60 * 1000

// ---------- Cost-aware tilt ----------

export type Tilt =
  | 'no-call'
  | 'favors-buying'
  | 'favors-selling'
  | 'favors-waiting'
  | 'unclear'

export const TILT_LABELS: Record<Tilt, string> = {
  'no-call': 'no call',
  'favors-buying': 'favors buying',
  'favors-selling': 'favors selling',
  'favors-waiting': 'favors waiting',
  unclear: 'unclear'
}

/** Badge class for a tilt, reusing the existing badge palette. */
export function tiltBadgeClass(tilt: Tilt): string {
  switch (tilt) {
    case 'favors-buying':
      return 'badge-ok'
    case 'favors-selling':
      return 'badge-bad'
    case 'favors-waiting':
      return 'badge-warn'
    default:
      return 'badge-off'
  }
}

/** Conditional phrasing for the tilt sentence — deliberately never a promise. */
export function tiltPhrase(tilt: Tilt): string {
  switch (tilt) {
    case 'favors-buying':
      return 'conditions modestly favor buying'
    case 'favors-selling':
      return 'conditions modestly favor selling'
    case 'favors-waiting':
      return 'the projected move is smaller than the cost, so waiting looks reasonable'
    case 'no-call':
      return 'input data is stale, so no call is made'
    default:
      return 'there is no clear edge either way'
  }
}

/**
 * Cost-aware tilt for a single horizon prediction:
 * - stale inputs (`data_fresh === false`) → 'no-call';
 * - expected move above the full round-trip cost, confidence ≥ 55% → 'favors-buying';
 * - expected drop beyond half the cost, confidence ≥ 55% → 'favors-selling';
 * - |expected move| within the cost → 'favors-waiting';
 * - anything else → 'unclear'.
 */
export function horizonTilt(p: Prediction, costPct: number = ROUND_TRIP_COST_PCT): Tilt {
  if (p.data_fresh === false) return 'no-call'
  const pct = p.expected_change_pct
  if (!Number.isFinite(pct)) return 'unclear'
  const conf = confidencePct(p.confidence) ?? 0
  if (pct > costPct && conf >= 55) return 'favors-buying'
  if (pct < -costPct / 2 && conf >= 55) return 'favors-selling'
  if (Math.abs(pct) <= costPct) return 'favors-waiting'
  return 'unclear'
}

// ---------- Advisor timeframe selection ----------

/** Chip labels for the advisor timeframe row (friendlier than HORIZON_LABELS). */
export const ADVISOR_HORIZON_LABELS: Record<Horizon, string> = {
  '1h': '1 hour',
  '4h': '4 hours',
  eod: 'End of today',
  '1d': 'Tomorrow',
  '3d': '3 days',
  '7d': '7 days',
  '30d': '30 days'
}

export const ADVISOR_HORIZON_KEY = 'igp_advisor_horizon'

export type AdvisorSelection =
  | { kind: 'std'; horizon: Horizon }
  | { kind: 'custom'; days: number }

/** Parse a persisted selection ('7d' or 'custom:14'); null when invalid. */
export function parseAdvisorSelection(raw: string | null): AdvisorSelection | null {
  if (!raw) return null
  if (raw.startsWith('custom:')) {
    const days = Number.parseInt(raw.slice('custom:'.length), 10)
    return Number.isInteger(days) && days >= 1 && days <= 90 ? { kind: 'custom', days } : null
  }
  return (HORIZONS as string[]).includes(raw)
    ? { kind: 'std', horizon: raw as Horizon }
    : null
}

export function serializeAdvisorSelection(sel: AdvisorSelection): string {
  return sel.kind === 'custom' ? `custom:${sel.days}` : sel.horizon
}

// ---------- Horizon selection helpers ----------

/**
 * Latest prediction per horizon (by predicted_at/created_at), returned in
 * canonical horizon order. `/predictions` already returns one row per horizon;
 * this dedupes defensively.
 */
export function latestByHorizon(predictions: Prediction[]): Prediction[] {
  const by = new Map<Horizon, Prediction>()
  for (const p of predictions) {
    const prev = by.get(p.horizon)
    if (prev === undefined) {
      by.set(p.horizon, p)
      continue
    }
    const tNew = Date.parse(p.predicted_at ?? p.created_at ?? '')
    const tOld = Date.parse(prev.predicted_at ?? prev.created_at ?? '')
    if (Number.isFinite(tNew) && (!Number.isFinite(tOld) || tNew >= tOld)) by.set(p.horizon, p)
  }
  const out: Prediction[] = []
  for (const h of HORIZONS) {
    const p = by.get(h)
    if (p !== undefined) out.push(p)
  }
  return out
}

/** Default advisor timeframe: 7d when available, otherwise the longest horizon present. */
export function defaultAdvisorHorizon(available: Horizon[]): Horizon | null {
  if (available.length === 0) return null
  if (available.includes('7d')) return '7d'
  const ordered = HORIZONS.filter((h) => available.includes(h))
  return ordered.length > 0 ? ordered[ordered.length - 1] : null
}

// ---------- Action-planner rows & best windows ----------

export interface PlanRow {
  horizon: Horizon
  targetTime: string
  /** target_time as epoch milliseconds. */
  t: number
  forecast: number
  lower: number
  upper: number
  expectedChangePct: number
  /** Percent change of the point forecast vs today's price (null without a price). */
  changeVsTodayPct: number | null
  /** Model expected change minus the assumed round-trip cost. */
  netPct: number
  tilt: Tilt
  dataFresh: boolean
  warnings: string[]
}

/**
 * One row per FUTURE horizon (target after `now`), latest prediction per
 * horizon, sorted by target time ascending. Pure — pass `now` for testing.
 */
export function planRows(
  predictions: Prediction[],
  currentPrice: number | null,
  costPct: number = ROUND_TRIP_COST_PCT,
  now: number = Date.now()
): PlanRow[] {
  const rows: PlanRow[] = []
  for (const p of latestByHorizon(predictions)) {
    const t = Date.parse(p.target_time)
    const point = pointForecastOf(p)
    if (!Number.isFinite(t) || t <= now || point === null) continue
    const lower = Number.isFinite(p.lower_bound) ? Math.min(p.lower_bound, point) : point
    const upper = Number.isFinite(p.upper_bound) ? Math.max(p.upper_bound, point) : point
    rows.push({
      horizon: p.horizon,
      targetTime: p.target_time,
      t,
      forecast: point,
      lower,
      upper,
      expectedChangePct: p.expected_change_pct,
      changeVsTodayPct:
        currentPrice !== null && currentPrice > 0
          ? ((point - currentPrice) / currentPrice) * 100
          : null,
      netPct: p.expected_change_pct - costPct,
      tilt: horizonTilt(p, costPct),
      dataFresh: p.data_fresh !== false,
      warnings: p.warnings ?? []
    })
  }
  return rows.sort((a, b) => a.t - b.t)
}

export type BestWindow = { when: 'today' } | { when: 'horizon'; row: PlanRow }

export interface BestWindows {
  buy: BestWindow
  sell: BestWindow
}

/**
 * Among fresh future rows: highest point forecast → likely best sell window,
 * lowest → likely best buy window. When every forecast is above today's price
 * the buy window is 'today'; mirrored for the sell window. Null when no fresh
 * rows exist.
 */
export function bestWindows(rows: PlanRow[], currentPrice: number | null): BestWindows | null {
  const fresh = rows.filter((r) => r.dataFresh)
  if (fresh.length === 0) return null
  let lo = fresh[0]
  let hi = fresh[0]
  for (const r of fresh) {
    if (r.forecast < lo.forecast) lo = r
    if (r.forecast > hi.forecast) hi = r
  }
  const buy: BestWindow =
    currentPrice !== null && lo.forecast > currentPrice
      ? { when: 'today' }
      : { when: 'horizon', row: lo }
  const sell: BestWindow =
    currentPrice !== null && hi.forecast < currentPrice
      ? { when: 'today' }
      : { when: 'horizon', row: hi }
  return { buy, sell }
}

// ---------- Small history helpers ----------

/**
 * Percent change between the latest observation and the latest observation at
 * least `days` before it. Falls back to the earliest available observation
 * when the series is shorter than the window; null with fewer than 2 points.
 */
export function changeOverDays(items: PriceHistoryItem[], days: number): number | null {
  const sorted = items
    .filter((it) => Number.isFinite(Date.parse(it.observed_at)) && Number.isFinite(it.value))
    .sort((a, b) => Date.parse(a.observed_at) - Date.parse(b.observed_at))
  if (sorted.length < 2) return null
  const last = sorted[sorted.length - 1]
  const cutoff = Date.parse(last.observed_at) - days * DAY_MS
  let ref = sorted[0]
  for (const it of sorted) {
    if (it === last) break
    if (Date.parse(it.observed_at) <= cutoff) ref = it
    else break
  }
  if (ref.value === 0) return null
  return ((last.value - ref.value) / ref.value) * 100
}
