import { useEffect, useRef, type ReactNode } from 'react'
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
  LineStyle,
  createChart,
  type CandlestickData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type LineData,
  type LogicalRange,
  type UTCTimestamp
} from 'lightweight-charts'
import type { CandleOverlays, ChartCandle, PivotLevels } from '../api/types'
import { useSettings } from '../lib/settings'
import { formatDateTime, formatTime, shortDate, type DisplayUnit } from '../lib/format'
import { isIntraday, type IntervalId } from './intervals'
import { indexOfTime, isSingleObservation } from './useCandles'
import { formatChartPrice } from './OhlcHeader'

/**
 * The imperative seam the drawing and indicator layers build on.
 *
 * Coordinates are in CSS pixels relative to `container`, which is also the
 * element `children` are stacked in — so an absolutely-positioned overlay
 * canvas at inset 0 shares the chart's coordinate space with no extra maths.
 */
export interface ChartHandle {
  chart: IChartApi
  candleSeries: ISeriesApi<'Candlestick'>
  container: HTMLDivElement
  timeToX(t: number): number | null
  priceToY(price: number): number | null
  xToTime(x: number): number | null
  yToPrice(y: number): number | null
  /** Fires on pan, zoom, resize and data change. Returns an unsubscribe fn. */
  onViewportChange(cb: () => void): () => void
}

/** Static levels drawn as price lines rather than as series. */
export interface ChartLevels {
  pivots?: PivotLevels | null
  support?: number | null
  resistance?: number | null
}

export interface TradingChartProps {
  candles: ChartCandle[]
  overlays: Partial<CandleOverlays> | null
  /**
   * Bucket times the overlay arrays belong to. Without these, overlays are
   * index-aligned and silently slide once older pages are prepended.
   */
  overlayTimes?: number[]
  interval: IntervalId
  unit: DisplayUnit
  symbol: string
  height: number
  onCrosshair: (candle: ChartCandle | null) => void
  onReady?: (handle: ChartHandle) => void
  children?: ReactNode
  levels?: ChartLevels
  /** Called when the user pans within LOAD_OLDER_BARS of the oldest bucket. */
  onLoadOlder?: () => void
}

/** How close to the left edge a pan has to get before older history is fetched. */
const LOAD_OLDER_BARS = 20

/**
 * lightweight-charts TickMarkType: Year=0, Month=1, DayOfMonth=2, Time=3,
 * TimeWithSeconds=4. Anything at or above Time is a within-day tick; below it
 * is a calendar boundary that must render as a DATE. Kept as a local constant
 * rather than importing the enum so the test file's module mock stays minimal.
 */
const TICK_MARK_TIME = 3

interface Palette {
  text: string
  border: string
  pos: string
  neg: string
  accent: string
  info: string
  warn: string
  purple: string
  muted: string
}

/** Resolve the current theme's CSS variables to concrete colors for the chart. */
function chartColors(): Palette {
  const css = getComputedStyle(document.documentElement)
  const v = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback
  return {
    text: v('--muted', '#8b98a5'),
    border: v('--border', '#22303f'),
    pos: v('--pos', '#2ea36b'),
    neg: v('--neg', '#e5534b'),
    accent: v('--accent', '#d4a017'),
    info: v('--info', '#58a6ff'),
    warn: v('--warn', '#d29922'),
    purple: v('--purple', '#a371f7'),
    muted: v('--muted', '#8b98a5')
  }
}

type OverlayField = Exclude<keyof CandleOverlays, 'supertrend_dir'>

interface OverlayStyle {
  color: keyof Palette
  width: 1 | 2
  style: LineStyle
  /** PSAR is a dot cloud, not a line. */
  markersOnly?: boolean
}

const OVERLAY_STYLES: Record<OverlayField, OverlayStyle> = {
  sma_20: { color: 'info', width: 1, style: LineStyle.Solid },
  sma_50: { color: 'purple', width: 1, style: LineStyle.Solid },
  bollinger_upper: { color: 'info', width: 1, style: LineStyle.Dashed },
  bollinger_mid: { color: 'info', width: 1, style: LineStyle.Dotted },
  bollinger_lower: { color: 'info', width: 1, style: LineStyle.Dashed },
  supertrend: { color: 'warn', width: 2, style: LineStyle.Solid },
  psar: { color: 'accent', width: 1, style: LineStyle.Solid, markersOnly: true },
  ichimoku_tenkan: { color: 'pos', width: 1, style: LineStyle.Solid },
  ichimoku_kijun: { color: 'neg', width: 1, style: LineStyle.Solid },
  ichimoku_senkou_a: { color: 'accent', width: 1, style: LineStyle.Dashed },
  ichimoku_senkou_b: { color: 'purple', width: 1, style: LineStyle.Dashed }
}

/**
 * Single-observation buckets are drawn hollow and muted.
 *
 * 1198 of the 1224 daily buckets in this data set hold exactly one tick, so a
 * green or red body on one of them would claim a day of trading that never
 * happened. A muted outline says "one print, no range" at a glance; the status
 * bar puts a number on it.
 */
function toBar(candle: ChartCandle, colors: Palette): CandlestickData<UTCTimestamp> {
  const bar: CandlestickData<UTCTimestamp> = {
    time: candle.t as UTCTimestamp,
    open: candle.open,
    high: candle.high,
    low: candle.low,
    close: candle.close
  }
  if (isSingleObservation(candle)) {
    bar.color = 'transparent'
    bar.borderColor = colors.muted
    bar.wickColor = 'transparent'
  }
  return bar
}

function sameBar(a: ChartCandle, b: ChartCandle): boolean {
  return (
    a.t === b.t &&
    a.open === b.open &&
    a.high === b.high &&
    a.low === b.low &&
    a.close === b.close &&
    a.ticks === b.ticks
  )
}

/**
 * True when `next` is `prev` with only its tail moved — the live-poll case,
 * where series.update() keeps the user's zoom and pan instead of setData()
 * rebuilding the series.
 */
function isTailPatch(prev: ChartCandle[], next: ChartCandle[]): boolean {
  if (prev.length === 0 || next.length < prev.length) return false
  if (prev[0].t !== next[0].t) return false
  for (let i = 0; i < prev.length - 1; i++) {
    if (!sameBar(prev[i], next[i])) return false
  }
  return true
}

function overlayLineData(times: number[], values: Array<number | null>): Array<LineData<UTCTimestamp>> {
  const out: Array<LineData<UTCTimestamp>> = []
  const n = Math.min(times.length, values.length)
  for (let i = 0; i < n; i++) {
    const v = values[i]
    if (v !== null && v !== undefined && Number.isFinite(v)) {
      out.push({ time: times[i] as UTCTimestamp, value: v })
    }
  }
  return out
}

export function TradingChart({
  candles,
  overlays,
  overlayTimes,
  interval,
  unit,
  symbol,
  height,
  onCrosshair,
  onReady,
  children,
  levels,
  onLoadOlder
}: TradingChartProps) {
  const { calendar } = useSettings()

  const wrapRef = useRef<HTMLDivElement | null>(null)
  const hostRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const handleRef = useRef<ChartHandle | null>(null)
  const listenersRef = useRef<Set<() => void>>(new Set())
  const paintedRef = useRef<ChartCandle[]>([])
  const overlaySeriesRef = useRef<Array<ISeriesApi<'Line'>>>([])
  const priceLinesRef = useRef<IPriceLine[]>([])

  // Fresh props for handlers that are subscribed exactly once, at mount.
  const latest = useRef({ candles, onCrosshair, onLoadOlder, interval, unit, symbol, calendar })
  useEffect(() => {
    latest.current = { candles, onCrosshair, onLoadOlder, interval, unit, symbol, calendar }
  })

  // ---- create once -------------------------------------------------------
  useEffect(() => {
    const wrap = wrapRef.current
    const host = hostRef.current
    if (!wrap || !host) return

    const colors = chartColors()
    const chart = createChart(host, {
      width: wrap.clientWidth || 600,
      height,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: colors.text,
        attributionLogo: false
      },
      grid: {
        vertLines: { color: colors.border, style: LineStyle.Dotted },
        horzLines: { color: colors.border, style: LineStyle.Dotted }
      },
      rightPriceScale: { borderColor: colors.border },
      timeScale: { borderColor: colors.border, secondsVisible: false },
      crosshair: {
        // Snap the crosshair to the candle under the pointer where the library
        // supports it, so readouts match a bar instead of a pixel.
        mode: CrosshairMode.MagnetOHLC,
        horzLine: { labelBackgroundColor: colors.accent },
        vertLine: { labelBackgroundColor: colors.accent }
      },
      handleScroll: {
        mouseWheel: true,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: true
      },
      handleScale: {
        mouseWheel: true,
        pinch: true,
        axisPressedMouseMove: true,
        axisDoubleClickReset: true
      }
    })
    chartRef.current = chart

    const series = chart.addSeries(CandlestickSeries, {
      upColor: colors.pos,
      downColor: colors.neg,
      borderUpColor: colors.pos,
      borderDownColor: colors.neg,
      wickUpColor: colors.pos,
      wickDownColor: colors.neg,
      priceLineVisible: true,
      lastValueVisible: true
    })
    seriesRef.current = series

    const notify = () => {
      for (const cb of Array.from(listenersRef.current)) cb()
    }

    const onCrosshairMove = (params: { time?: unknown; point?: unknown }) => {
      const cb = latest.current.onCrosshair
      if (!params.point || params.time === undefined || params.time === null) {
        cb(null)
        return
      }
      const t = Number(params.time)
      const i = indexOfTime(latest.current.candles, t)
      cb(i >= 0 ? latest.current.candles[i] : null)
    }
    chart.subscribeCrosshairMove(onCrosshairMove)

    const onDblClick = () => chart.timeScale().fitContent()
    chart.subscribeDblClick(onDblClick)

    const onRange = (range: LogicalRange | null) => {
      notify()
      if (!range) return
      if (range.from < LOAD_OLDER_BARS) latest.current.onLoadOlder?.()
    }
    chart.timeScale().subscribeVisibleLogicalRangeChange(onRange)

    const resize = new ResizeObserver(() => {
      const width = wrap.clientWidth
      if (width > 0) chart.applyOptions({ width })
      notify()
    })
    resize.observe(wrap)

    handleRef.current = {
      chart,
      candleSeries: series,
      container: wrap,
      timeToX: (t: number) => chart.timeScale().timeToCoordinate(t as UTCTimestamp),
      priceToY: (price: number) => series.priceToCoordinate(price),
      xToTime: (x: number) => {
        const t = chart.timeScale().coordinateToTime(x)
        return t === null ? null : Number(t)
      },
      yToPrice: (y: number) => {
        const p = series.coordinateToPrice(y)
        return p === null ? null : Number(p)
      },
      onViewportChange: (cb: () => void) => {
        listenersRef.current.add(cb)
        return () => {
          listenersRef.current.delete(cb)
        }
      }
    }
    onReady?.(handleRef.current)

    return () => {
      resize.disconnect()
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(onRange)
      chart.unsubscribeDblClick(onDblClick)
      chart.unsubscribeCrosshairMove(onCrosshairMove)
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
      handleRef.current = null
      overlaySeriesRef.current = []
      priceLinesRef.current = []
      paintedRef.current = []
    }
    // Created exactly once: every later change is an applyOptions/setData call.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ---- height ------------------------------------------------------------
  useEffect(() => {
    chartRef.current?.applyOptions({ height })
  }, [height])

  // ---- theme: repaint, never rebuild -------------------------------------
  useEffect(() => {
    const repaint = () => {
      const chart = chartRef.current
      const series = seriesRef.current
      if (!chart || !series) return
      const colors = chartColors()
      chart.applyOptions({
        layout: { textColor: colors.text },
        grid: {
          vertLines: { color: colors.border, style: LineStyle.Dotted },
          horzLines: { color: colors.border, style: LineStyle.Dotted }
        },
        rightPriceScale: { borderColor: colors.border },
        timeScale: { borderColor: colors.border },
        crosshair: {
          horzLine: { labelBackgroundColor: colors.accent },
          vertLine: { labelBackgroundColor: colors.accent }
        }
      })
      series.applyOptions({
        upColor: colors.pos,
        downColor: colors.neg,
        borderUpColor: colors.pos,
        borderDownColor: colors.neg,
        wickUpColor: colors.pos,
        wickDownColor: colors.neg
      })
      // Per-bar colors are baked into the data, so the muted single-observation
      // treatment has to be re-pushed with the new palette.
      series.setData(paintedRef.current.map((c) => toBar(c, colors)))
    }

    const observer = new MutationObserver(repaint)
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme']
    })
    return () => observer.disconnect()
  }, [])

  // ---- price / time formatting -------------------------------------------
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    chart.applyOptions({
      localization: {
        priceFormatter: (p: number) => formatChartPrice(p, symbol, unit),
        timeFormatter: (t: unknown) => formatDateTime(new Date(Number(t) * 1000), calendar)
      },
      timeScale: {
        timeVisible: isIntraday(interval),
        // lightweight-charts asks for two KINDS of tick on an intraday axis:
        // day/month/year boundaries and times within a day. Formatting both as
        // a clock time made every boundary on a 15m chart read "03:30" — the
        // Tehran rendering of UTC midnight — so five day-separators were
        // indistinguishable and the axis carried no date at all. The tick type
        // is the second argument; honour it.
        tickMarkFormatter: (t: unknown, tickMarkType: unknown) => {
          const d = new Date(Number(t) * 1000)
          const isTimeTick = Number(tickMarkType) >= TICK_MARK_TIME
          return isIntraday(interval) && isTimeTick ? formatTime(d) : shortDate(d, calendar)
        }
      }
    })
  }, [symbol, unit, calendar, interval])

  // ---- data --------------------------------------------------------------
  useEffect(() => {
    const chart = chartRef.current
    const series = seriesRef.current
    if (!chart || !series) return
    const colors = chartColors()
    const prev = paintedRef.current

    if (candles.length === 0) {
      series.setData([])
    } else if (isTailPatch(prev, candles)) {
      for (let i = Math.max(prev.length - 1, 0); i < candles.length; i++) {
        if (i < prev.length && sameBar(prev[i], candles[i])) continue
        series.update(toBar(candles[i], colors))
      }
    } else {
      // Prepending older history shifts every logical index; shift the visible
      // range back by the same amount so the user's viewport does not jump.
      const prepended =
        prev.length > 0 && candles[0].t < prev[0].t ? Math.max(indexOfTime(candles, prev[0].t), 0) : 0
      const before = prepended > 0 ? chart.timeScale().getVisibleLogicalRange() : null
      series.setData(candles.map((c) => toBar(c, colors)))
      if (before && prepended > 0) {
        chart.timeScale().setVisibleLogicalRange({
          from: before.from + prepended,
          to: before.to + prepended
        })
      } else if (prev.length === 0) {
        chart.timeScale().fitContent()
      }
    }

    paintedRef.current = candles
    for (const cb of Array.from(listenersRef.current)) cb()
  }, [candles])

  // ---- overlays ----------------------------------------------------------
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    for (const s of overlaySeriesRef.current) chart.removeSeries(s)
    overlaySeriesRef.current = []
    if (!overlays) return

    const colors = chartColors()
    for (const key of Object.keys(OVERLAY_STYLES) as OverlayField[]) {
      const values = overlays[key]
      if (!Array.isArray(values) || values.length === 0) continue
      const style = OVERLAY_STYLES[key]
      // Fall back to aligning with the newest buckets when the store could not
      // tell us which times the values belong to.
      const fallbackSource = latest.current.candles
      const times =
        overlayTimes && overlayTimes.length > 0
          ? overlayTimes
          : fallbackSource.slice(Math.max(fallbackSource.length - values.length, 0)).map((c) => c.t)
      const data = overlayLineData(times, values)
      if (data.length === 0) continue
      const series = chart.addSeries(LineSeries, {
        color: colors[style.color],
        lineWidth: style.width,
        lineStyle: style.style,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
        ...(style.markersOnly
          ? { lineVisible: false, pointMarkersVisible: true, pointMarkersRadius: 2 }
          : {})
      })
      series.setData(data)
      overlaySeriesRef.current.push(series)
    }
    // Deliberately not keyed on `candles`: the live poll hands back a new array
    // every minute, and rebuilding every overlay series for it would undo the
    // whole point of patching the tail.
  }, [overlays, overlayTimes])

  // ---- static levels -----------------------------------------------------
  useEffect(() => {
    const series = seriesRef.current
    if (!series) return
    for (const line of priceLinesRef.current) series.removePriceLine(line)
    priceLinesRef.current = []
    if (!levels) return

    const colors = chartColors()
    const add = (price: number, title: string, color: string, style: LineStyle) => {
      priceLinesRef.current.push(
        series.createPriceLine({ price, title, color, lineWidth: 1, lineStyle: style, axisLabelVisible: false })
      )
    }
    const p = levels.pivots
    if (p) {
      add(p.r3, 'R3', colors.neg, LineStyle.SparseDotted)
      add(p.r2, 'R2', colors.neg, LineStyle.SparseDotted)
      add(p.r1, 'R1', colors.neg, LineStyle.SparseDotted)
      add(p.p, 'P', colors.text, LineStyle.SparseDotted)
      add(p.s1, 'S1', colors.pos, LineStyle.SparseDotted)
      add(p.s2, 'S2', colors.pos, LineStyle.SparseDotted)
      add(p.s3, 'S3', colors.pos, LineStyle.SparseDotted)
    }
    if (levels.support !== null && levels.support !== undefined) {
      add(levels.support, 'support', colors.pos, LineStyle.LargeDashed)
    }
    if (levels.resistance !== null && levels.resistance !== undefined) {
      add(levels.resistance, 'resistance', colors.neg, LineStyle.LargeDashed)
    }
  }, [levels])

  return (
    <div className="tchart" ref={wrapRef} style={{ height }}>
      <div className="tchart-host" ref={hostRef} />
      {children}
      <div className="tchart-actions">
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          aria-label="Fit all candles"
          onClick={() => chartRef.current?.timeScale().fitContent()}
        >
          Fit
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          aria-label="Jump to latest candle"
          onClick={() => chartRef.current?.timeScale().scrollToRealTime()}
        >
          Latest
        </button>
      </div>
    </div>
  )
}
