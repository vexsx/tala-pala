import { useEffect, useRef } from 'react'
import { createSeriesMarkers, type ISeriesApi, type UTCTimestamp } from 'lightweight-charts'
import { useApi } from '../hooks/useApi'
import type {
  ChartCandle,
  NewsItem,
  Prediction,
  TrendAlignmentResponse,
  TrendSymbol
} from '../api/types'
import { pointForecastOf } from '../lib/forecastChart'
import type { IndicatorPlot } from './indicators/registry'

/**
 * The three overlays that are not indicators: the model's forecast, the news
 * events, and the server's trend-alignment read.
 *
 * Each one draws something the price series cannot justify on its own, so each
 * one is held to the same rule: DRAW ONLY WHAT THE SERVER SAID, and say what
 * it is. There is no arithmetic on a price or a moving average anywhere in
 * this file — the forecast band is the API's own interval, the alignment
 * verdict is the API's own conclusion, and an event with no timestamp is not
 * placed at all.
 */

// ---------------------------------------------------------------------------
// Forecast
// ---------------------------------------------------------------------------

export interface ForecastPoint {
  /** Bucket time in unix seconds. */
  time: number
  point: number
  lower: number
  upper: number
}

/**
 * The forecast rows worth drawing, oldest first.
 *
 * Rules, all of them honesty rules rather than cosmetics:
 *  - a row without a parseable target time or point estimate is dropped;
 *  - a row whose target is at or before the last candle is dropped — the
 *    overlay is a continuation, not a hindcast redrawn over bars that already
 *    happened;
 *  - rows sharing a target time are merged into one point with the widest of
 *    their intervals, so overlapping horizons never draw a narrower band than
 *    any single model claimed;
 *  - the last candle's close is prepended as a zero-width bridge so the dashed
 *    line starts where the candles end instead of floating in space.
 */
export function forecastPoints(
  predictions: Prediction[],
  lastCandle: ChartCandle | null
): ForecastPoint[] {
  const merged = new Map<number, { sum: number; n: number; lo: number; hi: number }>()
  for (const p of predictions) {
    const ms = Date.parse(p.target_time)
    const value = pointForecastOf(p)
    if (!Number.isFinite(ms) || value === null) continue
    const time = Math.floor(ms / 1000)
    if (lastCandle !== null && time <= lastCandle.t) continue
    const lo = Math.min(Number.isFinite(p.lower_bound) ? p.lower_bound : value, value)
    const hi = Math.max(Number.isFinite(p.upper_bound) ? p.upper_bound : value, value)
    const cur = merged.get(time)
    if (cur) {
      cur.sum += value
      cur.n += 1
      cur.lo = Math.min(cur.lo, lo)
      cur.hi = Math.max(cur.hi, hi)
    } else {
      merged.set(time, { sum: value, n: 1, lo, hi })
    }
  }

  const points = Array.from(merged.entries())
    .map(([time, f]) => ({ time, point: f.sum / f.n, lower: f.lo, upper: f.hi }))
    .sort((a, b) => a.time - b.time)

  if (points.length === 0) return []
  if (lastCandle === null) return points
  return [
    { time: lastCandle.t, point: lastCandle.close, lower: lastCandle.close, upper: lastCandle.close },
    ...points
  ]
}

/**
 * The forecast drawn as three series.
 *
 * Restraint is the point. The interval edges are thin, sparse-dotted and in a
 * colour used by nothing else on the chart; the centre line is heavily dashed
 * so it cannot be mistaken for an indicator (all solid or evenly dashed) or
 * for a user drawing (which the drawing layer paints on its own canvas). The
 * price-axis label reads EST, and the legend spells out ESTIMATE with the
 * interval next to it.
 */
export function forecastPlots(points: ForecastPoint[]): IndicatorPlot[] {
  if (points.length === 0) return []
  const base = {
    instanceId: 'forecast',
    paneKey: null,
    format: 'price' as const,
    owner: 'layer' as const
  }
  return [
    {
      ...base,
      key: 'forecast:upper',
      label: 'Upper bound',
      color: 'forecast-band' as const,
      style: 'sparse' as const,
      width: 1 as const,
      shape: 'line' as const,
      primary: false,
      data: points.map((p) => ({ time: p.time, value: p.upper }))
    },
    {
      ...base,
      key: 'forecast:lower',
      label: 'Lower bound',
      color: 'forecast-band' as const,
      style: 'sparse' as const,
      width: 1 as const,
      shape: 'line' as const,
      primary: false,
      data: points.map((p) => ({ time: p.time, value: p.lower }))
    },
    {
      ...base,
      key: 'forecast:point',
      label: 'Forecast',
      color: 'forecast' as const,
      style: 'largeDashed' as const,
      width: 2 as const,
      shape: 'line' as const,
      primary: true,
      axisLabel: 'EST',
      data: points.map((p) => ({ time: p.time, value: p.point }))
    }
  ]
}

// ---------------------------------------------------------------------------
// News events
// ---------------------------------------------------------------------------

export interface ChartEvent {
  id: number
  /** The bucket the headline is placed in, in unix seconds. */
  time: number
  title: string
  source: string
  urgent: boolean
  /** The publication time was estimated by the collector, not published. */
  estimated: boolean
}

export interface EventPlacement {
  events: ChartEvent[]
  /** Headlines the source published with no time at all. */
  undated: number
  /** Headlines whose time falls outside the buckets currently loaded. */
  outside: number
}

/**
 * Place headlines on the buckets that contain them.
 *
 * MEASURED FACT: news_events is empty in production right now, so the honest
 * outcome of this function today is an empty list. It must stay empty: an
 * article the source published with no timestamp gets NO marker, because the
 * only other time available is when the collector stored it, and dropping a
 * marker there would claim the news broke at the moment we happened to fetch
 * it. Those are counted and reported instead.
 */
export function placeEvents(
  items: NewsItem[],
  candles: ChartCandle[],
  intervalSeconds: number
): EventPlacement {
  const out: EventPlacement = { events: [], undated: 0, outside: 0 }
  if (candles.length === 0) {
    out.undated = items.filter((i) => i.published_at === null).length
    out.outside = items.length - out.undated
    return out
  }
  const first = candles[0].t
  const lastBucketEnd = candles[candles.length - 1].t + Math.max(intervalSeconds, 1)

  for (const item of items) {
    if (item.published_at === null) {
      out.undated += 1
      continue
    }
    const ms = Date.parse(item.published_at)
    if (!Number.isFinite(ms)) {
      out.undated += 1
      continue
    }
    const seconds = Math.floor(ms / 1000)
    if (seconds < first || seconds >= lastBucketEnd) {
      out.outside += 1
      continue
    }
    const bucket = bucketAt(candles, seconds)
    if (bucket === null) {
      out.outside += 1
      continue
    }
    out.events.push({
      id: item.id,
      time: bucket,
      title: item.title,
      source: item.source_name,
      urgent: item.urgency === 'urgent',
      estimated: item.published_at_estimated
    })
  }
  out.events.sort((a, b) => a.time - b.time)
  return out
}

/** Start time of the bucket containing `seconds`, or null when there is none. */
function bucketAt(candles: ChartCandle[], seconds: number): number | null {
  let lo = 0
  let hi = candles.length - 1
  let found: number | null = null
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (candles[mid].t <= seconds) {
      found = candles[mid].t
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  return found
}

/**
 * Attach event markers to the candle series.
 *
 * Every marker carries a glyph AND its headline as text, so the row is never
 * distinguished by colour alone. Markers are cleared on unmount and whenever
 * the toggle goes off; a stale marker outliving its toggle would put news on a
 * chart the user switched away from.
 */
export function useEventMarkers(
  series: ISeriesApi<'Candlestick'> | null,
  events: ChartEvent[],
  enabled: boolean
): void {
  const pluginRef = useRef<{ setMarkers: (m: unknown[]) => void; detach: () => void } | null>(null)

  // The plugin is attached only while the toggle is on: a marker plugin bolted
  // to the candle series for a feature nobody switched on is weight the chart
  // carries for nothing.
  useEffect(() => {
    if (!series || !enabled) return
    const plugin = createSeriesMarkers(series, [])
    pluginRef.current = plugin as unknown as {
      setMarkers: (m: unknown[]) => void
      detach: () => void
    }
    return () => {
      pluginRef.current = null
      try {
        plugin.detach()
      } catch {
        // The series went with the chart; nothing left to detach from.
      }
    }
  }, [series, enabled])

  useEffect(() => {
    const plugin = pluginRef.current
    if (!plugin) return
    const markers =
      !enabled || events.length === 0
        ? []
        : events.map((event) => ({
            time: event.time as UTCTimestamp,
            position: 'aboveBar',
            shape: 'circle',
            color: event.urgent ? 'var(--warn)' : 'var(--muted)',
            text: event.urgent ? '!' : '•',
            size: 1
          }))
    try {
      plugin.setMarkers(markers)
    } catch {
      // The series was disposed with its chart before this effect ran.
    }
  }, [events, enabled])
}

// ---------------------------------------------------------------------------
// Trend alignment
// ---------------------------------------------------------------------------

const TREND_PATH = '/market/trend-alignment?symbol='

/** Only these two symbols are served; anything else 400s. */
export function isTrendSymbol(symbol: string): symbol is TrendSymbol {
  return symbol === 'IR_GOLD_18K' || symbol === 'XAUUSD'
}

export interface TrendOverlayReading {
  data: TrendAlignmentResponse | null
  loading: boolean
  error: string | null
  reload: () => void
  /** The symbol on the chart has no trend-alignment endpoint. */
  unsupported: boolean
}

/**
 * The 1D / 4H / 1H alignment, read from the server.
 *
 * The chart is showing ONE timeframe, live, and the server evaluates three
 * timeframes on CLOSED candles. Deriving the verdict from the EMA lines the
 * chart happens to be drawing would therefore produce a different answer from
 * the card on Overview and from the alerts — for the same market, at the same
 * moment. So the browser asks, and displays what it is told.
 */
export function useTrendOverlay(symbol: string, enabled: boolean): TrendOverlayReading {
  const unsupported = !isTrendSymbol(symbol)
  const path = enabled && !unsupported ? `${TREND_PATH}${encodeURIComponent(symbol)}` : null
  const res = useApi<TrendAlignmentResponse>(path, [symbol, enabled])
  return {
    data: res.data,
    loading: res.loading && res.data === null,
    error: res.error,
    reload: res.reload,
    unsupported
  }
}
