import { useEffect, useMemo, useRef } from 'react'
import type { IChartApi } from 'lightweight-charts'
import type { DisplayUnit } from '../../lib/format'
import { formatChartPrice } from '../OhlcHeader'
import { createPlotLayer, type PlotLayer } from './series'
import { valueAt, type IndicatorPlot } from './registry'

/**
 * RSI and MACD in their own stacked panes.
 *
 * The installed lightweight-charts (5.2.0) has first-class panes:
 * `chart.addPane()`, `chart.panes()` and an optional `paneIndex` on
 * `addSeries`. Using them means the oscillators share the price chart's ONE
 * time scale and ONE crosshair — hovering the RSI at a timestamp is, by
 * construction, the same candle the price pane is showing. Two stacked charts
 * would need the ranges wired together by hand, and any dropped event would
 * put the two readouts on different bars while both looked plausible.
 *
 * There is deliberately no volume pane: this data source carries no volume at
 * all (ChartCandle.volume is always null), and an empty pane would imply one
 * exists.
 */

/** Height each oscillator pane asks for; the price pane keeps the rest. */
export const PANE_HEIGHT = 110

export interface IndicatorPanesProps {
  chart: IChartApi | null
  /** Every sub-pane plot, in the order their instances were added. */
  plots: IndicatorPlot[]
  /** Total chart height, so the price pane keeps its size as panes appear. */
  height: number
  /** Crosshair bucket time; null reads the latest value instead. */
  time: number | null
  symbol: string
  unit: DisplayUnit
}

/** Distinct pane keys, in first-seen order. */
function paneOrder(plots: IndicatorPlot[]): string[] {
  const seen: string[] = []
  for (const plot of plots) {
    if (plot.paneKey === null) continue
    if (!seen.includes(plot.paneKey)) seen.push(plot.paneKey)
  }
  return seen
}

function supportsPanes(chart: IChartApi | null): boolean {
  return (
    chart !== null &&
    typeof (chart as { addPane?: unknown }).addPane === 'function' &&
    typeof (chart as { panes?: unknown }).panes === 'function'
  )
}

export function formatPlotValue(
  plot: IndicatorPlot,
  value: number | null,
  symbol: string,
  unit: DisplayUnit
): string {
  if (value === null) return '—'
  // RSI is a bounded index, not money; MACD is a price difference and follows
  // the IRT/IRR toggle like every other price on the page.
  return plot.format === 'price' ? formatChartPrice(value, symbol, unit) : value.toFixed(1)
}

export function IndicatorPanes({
  chart,
  plots,
  height,
  time,
  symbol,
  unit
}: IndicatorPanesProps) {
  const panePlots = useMemo(() => plots.filter((p) => p.paneKey !== null), [plots])
  const keys = useMemo(() => paneOrder(panePlots), [panePlots])
  const keySignature = keys.join('|')
  const layersRef = useRef<Map<string, PlotLayer>>(new Map())
  // Fresh plots for a layer that is created after they were computed — adding a
  // second oscillator rebuilds every layer, and the first one must not come
  // back empty.
  const plotsRef = useRef(panePlots)
  plotsRef.current = panePlots
  const usable = supportsPanes(chart)

  // ---- panes and their layers: rebuilt only when the pane SET changes ------
  useEffect(() => {
    if (!chart || !usable || keys.length === 0) return
    const layers = new Map<string, PlotLayer>()
    while (chart.panes().length < keys.length + 1) chart.addPane()
    keys.forEach((key, i) => layers.set(key, createPlotLayer(chart, i + 1)))
    layersRef.current = layers
    for (const [key, layer] of layers) {
      layer.sync(plotsRef.current.filter((p) => p.paneKey === key))
    }

    return () => {
      layersRef.current = new Map()
      for (const layer of layers.values()) layer.detach()
      // Panes do not always collapse on their own once emptied, and a leftover
      // empty pane would keep squeezing the candles. Only the ones opened above
      // are closed — pane 0 is the price, and anything else belongs to someone
      // who will clean up after themselves.
      try {
        const open = chart.panes().length
        for (let i = open - 1; i >= Math.max(open - keys.length, 1); i--) chart.removePane(i)
      } catch {
        // The chart was already disposed by TradingChart's own cleanup.
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chart, usable, keySignature])

  // ---- data -------------------------------------------------------------
  useEffect(() => {
    for (const [key, layer] of layersRef.current) {
      layer.sync(panePlots.filter((p) => p.paneKey === key))
    }
  }, [panePlots])

  // ---- heights ----------------------------------------------------------
  useEffect(() => {
    if (!chart || !usable || keys.length === 0) return
    const panes = chart.panes()
    if (panes.length < keys.length + 1) return
    // Stretch factors are relative, so asking for (remaining height : one pane
    // height) keeps the candles the same size whether one oscillator is open
    // or three are.
    const price = Math.max((height - PANE_HEIGHT * keys.length) / PANE_HEIGHT, 1)
    panes[0].setStretchFactor(price)
    for (let i = 1; i <= keys.length; i++) panes[i].setStretchFactor(1)
  }, [chart, usable, keySignature, height, keys.length])

  if (keys.length === 0) return null

  if (!usable) {
    return (
      <p className="muted small tchart-notice" role="status">
        This chart engine has no sub-panes, so RSI and MACD cannot be drawn. Every other indicator
        is unaffected.
      </p>
    )
  }

  return (
    <ul className="tchart-pane-legend" aria-label="Oscillator panes">
      {keys.map((key) => {
        const mine = panePlots.filter((p) => p.paneKey === key)
        const title = mine.find((p) => p.primary)?.label ?? key
        return (
          <li key={key} className="tchart-pane-row">
            <span className="tchart-pane-name">{title}</span>
            {mine.map((plot) => (
              <span key={plot.key} className="tchart-pane-value">
                <span className="tchart-swatch" data-color={plot.color} aria-hidden="true" />
                <span className="muted">{plot.label}</span>{' '}
                <span className="num mono">
                  {formatPlotValue(plot, valueAt(plot, time), symbol, unit)}
                </span>
              </span>
            ))}
          </li>
        )
      })}
    </ul>
  )
}
