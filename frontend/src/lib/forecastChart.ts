import type { Prediction, PriceHistoryItem } from '../api/types'

/**
 * One recharts-ready point on the combined history + forecast chart.
 * `t` is the epoch-millisecond timestamp used for ordering; callers map it to
 * a display label before handing the array to PriceChart.
 */
export interface ForecastChartPoint {
  t: number
  actual?: number
  forecast?: number
  /** [lower, upper] prediction interval for the shaded fan band. */
  band?: [number, number]
}

/** The live API calls the point estimate point_forecast; older payloads used predicted_value. */
function pointForecastOf(p: Prediction): number | null {
  const v = typeof p.point_forecast === 'number' ? p.point_forecast : p.predicted_value
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/**
 * Merge actual price history with the latest prediction rows into a single
 * time-ordered series:
 *
 * - history items become `{t, actual}` points sorted ascending (later
 *   observations win on duplicate timestamps);
 * - each prediction becomes a `{t, forecast, band: [lower, upper]}` point at
 *   its target_time; rows sharing a target_time are merged (mean forecast,
 *   min(lower)..max(upper) band) so the overlay fans out;
 * - predictions whose target_time is not after the last actual are dropped
 *   (the overlay is a continuation, not a hindcast);
 * - when both series exist, the last actual point is bridged into the
 *   forecast series (forecast = actual, zero-width band) so the dashed
 *   forecast line and the band connect to the solid history line.
 */
export function buildForecastChartData(
  history: PriceHistoryItem[],
  predictions: Prediction[]
): ForecastChartPoint[] {
  // --- actuals: sort, dedupe by timestamp (last write wins) ---
  const actualByT = new Map<number, number>()
  const sortedHistory = history
    .slice()
    .sort((a, b) => Date.parse(a.observed_at) - Date.parse(b.observed_at))
  for (const item of sortedHistory) {
    const t = Date.parse(item.observed_at)
    if (Number.isFinite(t) && Number.isFinite(item.value)) actualByT.set(t, item.value)
  }
  const actuals: ForecastChartPoint[] = Array.from(actualByT.entries())
    .map(([t, v]) => ({ t, actual: v }))
    .sort((a, b) => a.t - b.t)
  const lastActual = actuals.length > 0 ? actuals[actuals.length - 1] : null

  // --- forecasts: one point per target_time, merged across horizons ---
  const merged = new Map<number, { sum: number; n: number; lo: number; hi: number }>()
  for (const p of predictions) {
    const t = Date.parse(p.target_time)
    const v = pointForecastOf(p)
    if (!Number.isFinite(t) || v === null) continue
    if (lastActual !== null && t <= lastActual.t) continue
    const lo = Math.min(Number.isFinite(p.lower_bound) ? p.lower_bound : v, v)
    const hi = Math.max(Number.isFinite(p.upper_bound) ? p.upper_bound : v, v)
    const cur = merged.get(t)
    if (cur) {
      cur.sum += v
      cur.n += 1
      cur.lo = Math.min(cur.lo, lo)
      cur.hi = Math.max(cur.hi, hi)
    } else {
      merged.set(t, { sum: v, n: 1, lo, hi })
    }
  }
  const forecasts: ForecastChartPoint[] = Array.from(merged.entries())
    .map(([t, f]) => ({
      t,
      forecast: f.sum / f.n,
      band: [f.lo, f.hi] as [number, number]
    }))
    .sort((a, b) => a.t - b.t)

  if (forecasts.length === 0) return actuals

  const out = actuals.slice()
  if (lastActual !== null && lastActual.actual !== undefined) {
    // Bridge so the dashed forecast line starts where the solid line ends.
    out[out.length - 1] = {
      ...lastActual,
      forecast: lastActual.actual,
      band: [lastActual.actual, lastActual.actual]
    }
  }
  return out.concat(forecasts)
}
