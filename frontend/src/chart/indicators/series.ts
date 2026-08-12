import { useEffect, useRef } from 'react'
import {
  HistogramSeries,
  LineSeries,
  LineStyle,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type UTCTimestamp
} from 'lightweight-charts'
import type { IndicatorPlot, PlotColor } from './registry'

/**
 * The imperative bridge between the pure plot descriptions in registry.ts and
 * lightweight-charts.
 *
 * A layer owns a set of series keyed by plot key and reconciles them: new keys
 * are added, dropped keys are removed, surviving keys are updated in place.
 * Nothing here rebuilds a series that has not changed — a rebuilt series loses
 * its place in the draw order and flickers on every 60-second poll.
 *
 * Colours are resolved from the theme's CSS variables at attach time and
 * re-resolved when the theme flips, so an indicator can never be a hard-coded
 * colour that disappears into a light background.
 */

export type Palette = Record<PlotColor, string>

const FALLBACKS: Palette = {
  info: '#58a6ff',
  purple: '#a371f7',
  warn: '#d29922',
  accent: '#d4a017',
  pos: '#2ea36b',
  neg: '#e5534b',
  muted: '#8b98a5',
  forecast: '#7b61ff',
  'forecast-band': '#5a4bb8'
}

const PLOT_COLORS = Object.keys(FALLBACKS) as PlotColor[]

export function chartPalette(): Palette {
  const css = getComputedStyle(document.documentElement)
  const out = {} as Palette
  for (const name of PLOT_COLORS) {
    out[name] = css.getPropertyValue(`--${name}`).trim() || FALLBACKS[name]
  }
  return out
}

/**
 * Watch the theme attribute the settings provider flips and run `onChange`.
 * Returns an unsubscribe; every caller must use it, or the observer outlives
 * the chart it was repainting.
 */
export function onThemeChange(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange)
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
  return () => observer.disconnect()
}

/** Style names stay strings in the registry so it never imports the chart lib. */
function lineStyleOf(style: IndicatorPlot['style']): LineStyle {
  switch (style) {
    case 'dashed':
      return LineStyle.Dashed
    case 'dotted':
      return LineStyle.Dotted
    case 'sparse':
      return LineStyle.SparseDotted
    case 'largeDashed':
      return LineStyle.LargeDashed
    default:
      return LineStyle.Solid
  }
}

/** Cheap change detector, so an unchanged plot does not get a fresh setData. */
function signature(plot: IndicatorPlot): string {
  const n = plot.data.length
  if (n === 0) return '0'
  const first = plot.data[0]
  const last = plot.data[n - 1]
  return `${n}|${first.time}|${first.value}|${last.time}|${last.value}`
}

type PlotSeries = ISeriesApi<'Line'> | ISeriesApi<'Histogram'>

interface Attached {
  series: PlotSeries
  plot: IndicatorPlot
  signature: string
  priceLines: IPriceLine[]
}

export interface PlotLayer {
  /** Reconcile the chart with `plots`; safe to call on every render. */
  sync(plots: IndicatorPlot[]): void
  /** Remove every series this layer owns. */
  detach(): void
}

/**
 * TradingChart unmounts (and disposes its chart) whenever the candle list
 * empties, which can happen in the same commit that hands this layer a new set
 * of plots. lightweight-charts throws on a disposed object and offers no
 * predicate to ask first, so a disposed chart is caught here rather than
 * surfacing as an uncaught error inside a React effect.
 */
function safely(action: () => void): void {
  try {
    action()
  } catch {
    // The chart went away with its host; there is nothing left to draw on.
  }
}

/**
 * A reconciling set of series on one pane.
 *
 * `paneIndex` is fixed for the life of the layer because pane indices shift
 * when a pane closes; panes.tsx rebuilds its layers when the pane set changes
 * rather than renumbering live series underneath the user.
 */
export function createPlotLayer(chart: IChartApi, paneIndex = 0): PlotLayer {
  const attached = new Map<string, Attached>()
  let palette = chartPalette()
  let disposed = false

  const applyStyle = (entry: Attached) => {
    const color = palette[entry.plot.color]
    if (entry.plot.shape === 'histogram') {
      ;(entry.series as ISeriesApi<'Histogram'>).applyOptions({ color })
      return
    }
    ;(entry.series as ISeriesApi<'Line'>).applyOptions({
      color,
      lineWidth: entry.plot.width,
      lineStyle: lineStyleOf(entry.plot.style)
    })
  }

  const pushData = (entry: Attached) => {
    if (entry.plot.shape === 'histogram') {
      // The sign is carried by the bar's own side of zero as well as by its
      // colour, so the histogram still reads in greyscale.
      ;(entry.series as ISeriesApi<'Histogram'>).setData(
        entry.plot.data.map((p) => ({
          time: p.time as UTCTimestamp,
          value: p.value,
          color: p.value >= 0 ? palette.pos : palette.neg
        }))
      )
      return
    }
    ;(entry.series as ISeriesApi<'Line'>).setData(
      entry.plot.data.map((p) => ({ time: p.time as UTCTimestamp, value: p.value }))
    )
  }

  const syncReferenceLines = (entry: Attached) => {
    for (const line of entry.priceLines) entry.series.removePriceLine(line)
    entry.priceLines = []
    for (const price of entry.plot.reference ?? []) {
      entry.priceLines.push(
        entry.series.createPriceLine({
          price,
          color: palette.muted,
          lineWidth: 1,
          lineStyle: LineStyle.Dotted,
          axisLabelVisible: true,
          title: ''
        })
      )
    }
  }

  const create = (plot: IndicatorPlot): Attached => {
    const color = palette[plot.color]
    const series: PlotSeries =
      plot.shape === 'histogram'
        ? chart.addSeries(
            HistogramSeries,
            { color, priceLineVisible: false, lastValueVisible: false },
            paneIndex
          )
        : chart.addSeries(
            LineSeries,
            {
              color,
              lineWidth: plot.width,
              lineStyle: lineStyleOf(plot.style),
              priceLineVisible: false,
              // An axis tag is opt-in: only the forecast asks for one, so the
              // price scale never fills up with indicator labels.
              lastValueVisible: plot.axisLabel !== undefined,
              title: plot.axisLabel ?? '',
              crosshairMarkerVisible: false,
              ...(plot.shape === 'dots'
                ? { lineVisible: false, pointMarkersVisible: true, pointMarkersRadius: 2 }
                : {})
            },
            paneIndex
          )
    return { series, plot, signature: '', priceLines: [] }
  }

  const createOrNull = (plot: IndicatorPlot): Attached | null => {
    try {
      return create(plot)
    } catch {
      // The chart was disposed with its host before this sync ran.
      return null
    }
  }

  const repaint = () => {
    if (disposed) return
    palette = chartPalette()
    for (const entry of attached.values()) {
      applyStyle(entry)
      // Per-bar colours are baked into histogram data, so they have to be
      // re-pushed with the new palette rather than applied as an option.
      if (entry.plot.shape === 'histogram') pushData(entry)
      syncReferenceLines(entry)
    }
  }

  const stopTheme = onThemeChange(repaint)

  return {
    sync(plots: IndicatorPlot[]) {
      if (disposed) return
      const wanted = new Set(plots.map((p) => p.key))
      for (const [key, entry] of Array.from(attached.entries())) {
        if (wanted.has(key)) continue
        safely(() => chart.removeSeries(entry.series))
        attached.delete(key)
      }
      for (const plot of plots) {
        const existing = attached.get(plot.key)
        const entry = existing ?? createOrNull(plot)
        if (entry === null) continue
        if (existing === undefined) attached.set(plot.key, entry)

        const styleChanged =
          entry.plot.color !== plot.color ||
          entry.plot.style !== plot.style ||
          entry.plot.width !== plot.width
        const referenceChanged =
          (entry.plot.reference ?? []).join(',') !== (plot.reference ?? []).join(',')
        const isNew = entry.signature === ''
        const next = signature(plot)
        const dataChanged = entry.signature !== next
        entry.plot = plot
        safely(() => {
          if (styleChanged) applyStyle(entry)
          if (dataChanged) {
            entry.signature = next
            pushData(entry)
          }
          if (isNew || referenceChanged) syncReferenceLines(entry)
        })
      }
    },
    detach() {
      if (disposed) return
      disposed = true
      stopTheme()
      for (const entry of attached.values()) safely(() => chart.removeSeries(entry.series))
      attached.clear()
    }
  }
}

/**
 * Keep one pane's indicator series in step with `plots` for the life of a
 * component. The layer is created once per (chart, pane) and torn down on
 * unmount, so a symbol change or a theme flip never leaks a series.
 */
export function useIndicatorSeries(
  chart: IChartApi | null,
  plots: IndicatorPlot[],
  paneIndex = 0
): void {
  const layerRef = useRef<PlotLayer | null>(null)
  // The chart arrives asynchronously (onReady), so the layer has to be able to
  // catch up with whatever plots were already computed when it appears.
  const plotsRef = useRef(plots)
  plotsRef.current = plots

  useEffect(() => {
    if (!chart) return
    const layer = createPlotLayer(chart, paneIndex)
    layerRef.current = layer
    layer.sync(plotsRef.current)
    return () => {
      layerRef.current = null
      layer.detach()
    }
  }, [chart, paneIndex])

  useEffect(() => {
    layerRef.current?.sync(plots)
  }, [plots])
}
