import { useState } from 'react'
import type { PivotLevels, TrendAlignmentState, TrendState, TrendTimeframeKey } from '../api/types'
import { useSettings } from '../lib/settings'
import { formatDateTime, type DisplayUnit } from '../lib/format'
import { formatChartPrice } from './OhlcHeader'
import { IndicatorSettingsForm } from './IndicatorMenu'
import { formatPlotValue } from './indicators/panes'
import {
  instanceLabel,
  levelReadouts,
  removeInstance,
  replaceInstance,
  setVisibility,
  valueAt,
  type ChartIndicatorState,
  type IndicatorInstance,
  type IndicatorPlot
} from './indicators/registry'
import type { EventPlacement, ForecastPoint, TrendOverlayReading } from './overlays'

/**
 * The chart legend: one row per active indicator, plus a block for each
 * overlay that is switched on.
 *
 * Every number here is READ, never derived. Indicator values come from the
 * plot arrays registry.ts built; the trend-alignment verdict comes from
 * GET /market/trend-alignment. In particular this file performs no
 * moving-average arithmetic and no price comparison of its own — the chart is
 * showing one timeframe live, the server evaluates three on closed candles,
 * and a locally-derived verdict would contradict both the Overview card and
 * the alerts for the same market. src/test/ChartLegend.test.tsx pins that.
 */

export interface ForecastReading {
  points: ForecastPoint[]
  loading: boolean
  error: string | null
}

export interface EventReading {
  placement: EventPlacement
  loading: boolean
  error: string | null
  /** Mirrors the API's collection_enabled, so an empty feed can explain itself. */
  collectionEnabled: boolean
}

export interface ChartLegendProps {
  state: ChartIndicatorState
  onChange: (next: ChartIndicatorState) => void
  /** Every plot, including the ones TradingChart draws from its overlays prop. */
  plots: IndicatorPlot[]
  /** Crosshair bucket time; null reads each indicator's latest value instead. */
  time: number | null
  symbol: string
  unit: DisplayUnit
  levels: { pivots: PivotLevels | null; support: number | null; resistance: number | null }
  /** Instances with no value anywhere in this window. */
  cold: IndicatorInstance[]
  forecast: ForecastReading
  events: EventReading
  trend: TrendOverlayReading
}

/** Glyph AND word for every state, so the read survives greyscale. */
const TREND_GLYPH: Record<TrendState, string> = {
  bullish: '▲',
  bearish: '▼',
  neutral: '●',
  unavailable: '—'
}

const TREND_LABEL: Record<TrendState, string> = {
  bullish: 'BULLISH',
  bearish: 'BEARISH',
  neutral: 'NEUTRAL',
  unavailable: 'UNAVAILABLE'
}

const ALIGNMENT_GLYPH: Record<TrendAlignmentState, string> = {
  full_bullish: '▲',
  full_bearish: '▼',
  not_aligned: '◆'
}

const ALIGNMENT_LABEL: Record<TrendAlignmentState, string> = {
  full_bullish: 'FULL BULLISH',
  full_bearish: 'FULL BEARISH',
  not_aligned: 'NOT ALIGNED'
}

const TIMEFRAME_ORDER: TrendTimeframeKey[] = ['1d', '4h', '1h']

const TIMEFRAME_LABEL: Record<TrendTimeframeKey, string> = {
  '1d': '1D',
  '4h': '4H',
  '1h': '1H'
}

export function ChartLegend({
  state,
  onChange,
  plots,
  time,
  symbol,
  unit,
  levels,
  cold,
  forecast,
  events,
  trend
}: ChartLegendProps) {
  const { calendar } = useSettings()
  const [editing, setEditing] = useState<string | null>(null)

  const coldIds = new Set(cold.map((i) => i.id))
  const anything =
    state.instances.length > 0 ||
    state.overlays.forecast ||
    state.overlays.events ||
    state.overlays.trend

  if (!anything) {
    return (
      <div className="tchart-legend" aria-label="Active indicators">
        <span className="muted small">
          No indicators. Candles only — open the Indicators menu to add some.
        </span>
      </div>
    )
  }

  return (
    <div className="tchart-legend" aria-label="Active indicators">
      {state.instances.length > 0 && (
        <ul className="tchart-legend-rows">
          {state.instances.map((instance) => {
            const mine = plots.filter((p) => p.instanceId === instance.id)
            const primary = mine.find((p) => p.primary) ?? mine[0] ?? null
            const readouts = levelReadouts(instance, levels)
            const label = instanceLabel(instance)
            const isCold = coldIds.has(instance.id)
            const value =
              primary === null
                ? null
                : formatPlotValue(primary, valueAt(primary, time), symbol, unit)

            return (
              <li
                key={instance.id}
                className={`tchart-legend-row ${instance.visible ? '' : 'tchart-legend-off'}`}
              >
                <span
                  className="tchart-swatch"
                  data-color={primary?.color ?? 'muted'}
                  data-style={primary?.style ?? 'solid'}
                  aria-hidden="true"
                />
                <span className="tchart-legend-name" title={label}>
                  {label}
                </span>

                {readouts.length > 0 ? (
                  <span className="tchart-legend-value">
                    {readouts.map((readout) => (
                      <span key={readout.label}>
                        <span className="muted">{readout.label}</span>{' '}
                        <span className="num mono">
                          {formatChartPrice(readout.value, symbol, unit)}
                        </span>{' '}
                      </span>
                    ))}
                  </span>
                ) : (
                  <span className="num mono tchart-legend-value">{value ?? '—'}</span>
                )}

                {isCold && (
                  <span
                    className="badge badge-off"
                    title="This indicator needs more history than the window currently holds"
                  >
                    no data
                  </span>
                )}

                <button
                  type="button"
                  className="btn btn-ghost btn-sm tchart-legend-btn"
                  aria-label={`${instance.visible ? 'Hide' : 'Show'} ${label}`}
                  aria-pressed={instance.visible}
                  onClick={() => onChange(setVisibility(state, instance.id, !instance.visible))}
                >
                  <span aria-hidden="true">{instance.visible ? '●' : '○'}</span>
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm tchart-legend-btn"
                  aria-label={`${label} settings`}
                  aria-expanded={editing === instance.id}
                  onClick={() => setEditing(editing === instance.id ? null : instance.id)}
                >
                  <span aria-hidden="true">⚙</span>
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm tchart-legend-btn"
                  aria-label={`Remove ${label}`}
                  onClick={() => {
                    setEditing(null)
                    onChange(removeInstance(state, instance.id))
                  }}
                >
                  <span aria-hidden="true">✕</span>
                </button>

                {editing === instance.id && (
                  <IndicatorSettingsForm
                    instance={instance}
                    onClose={() => setEditing(null)}
                    onApply={(next) => {
                      const result = replaceInstance(state, instance.id, next)
                      if (!result.ok) return result.message
                      onChange(result.value)
                      return null
                    }}
                  />
                )}
              </li>
            )
          })}
        </ul>
      )}

      {state.overlays.forecast && (
        <ForecastBlock reading={forecast} symbol={symbol} unit={unit} calendar={calendar} />
      )}

      {state.overlays.events && <EventBlock reading={events} />}

      {state.overlays.trend && <TrendBlock reading={trend} />}
    </div>
  )
}

// ---------------------------------------------------------------------------

function ForecastBlock({
  reading,
  symbol,
  unit,
  calendar
}: {
  reading: ForecastReading
  symbol: string
  unit: DisplayUnit
  calendar: 'jalali' | 'gregorian'
}) {
  // The bridge point sits on the last candle; the first real target is the one
  // after it, and a lone bridge point is not a forecast worth announcing.
  const targets = reading.points.slice(1)
  const next = targets.length > 0 ? targets[0] : null
  const price = (value: number) => formatChartPrice(value, symbol, unit)

  return (
    <div className="tchart-overlay-block" aria-label="Forecast overlay">
      <span className="tchart-overlay-title">
        <span className="tchart-swatch" data-color="forecast" data-style="largeDashed" aria-hidden="true" />
        FORECAST
      </span>
      <span className="badge badge-warn">ESTIMATE</span>
      {reading.error !== null ? (
        <span className="muted small">{reading.error}</span>
      ) : next === null ? (
        <span className="muted small">
          {reading.loading
            ? 'Loading forecasts…'
            : 'No forecast lands after the last candle, so nothing is drawn.'}
        </span>
      ) : (
        <>
          <span className="num mono">{price(next.point)}</span>
          <span className="muted small">
            {price(next.lower)} – {price(next.upper)} ·{' '}
            {formatDateTime(new Date(next.time * 1000), calendar)}
          </span>
        </>
      )}
      <span className="muted small tchart-overlay-note">
        An estimate with an uncertainty interval — not a path the price will take. The dashed line
        and its band are the model’s own numbers.
      </span>
    </div>
  )
}

// ---------------------------------------------------------------------------

function EventBlock({ reading }: { reading: EventReading }) {
  const { events, undated, outside } = reading.placement
  const placed = events.length

  return (
    <div className="tchart-overlay-block" aria-label="News events overlay">
      <span className="tchart-overlay-title">
        <span aria-hidden="true">◇</span> NEWS EVENTS
      </span>
      {reading.error !== null ? (
        <span className="muted small">{reading.error}</span>
      ) : reading.loading ? (
        <span className="muted small">Loading headlines…</span>
      ) : placed === 0 ? (
        <span className="muted small">
          {reading.collectionEnabled
            ? 'No stored headline carries a publication time inside this window, so no marker is placed.'
            : 'News collection is switched off, so there are no events to place.'}
        </span>
      ) : (
        <span className="num mono">{placed} marked</span>
      )}
      {(undated > 0 || outside > 0) && (
        <span className="muted small">
          {undated > 0 && `${undated} without a publication time (never placed)`}
          {undated > 0 && outside > 0 && ' · '}
          {outside > 0 && `${outside} outside this window`}
        </span>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------

function TrendBlock({ reading }: { reading: TrendOverlayReading }) {
  const data = reading.data

  if (reading.unsupported) {
    return (
      <div className="tchart-overlay-block" aria-label="Trend alignment overlay">
        <span className="tchart-overlay-title">TREND ALIGNMENT</span>
        <span className="muted small">Not served for this symbol.</span>
      </div>
    )
  }

  if (reading.error !== null) {
    return (
      <div className="tchart-overlay-block" aria-label="Trend alignment overlay">
        <span className="tchart-overlay-title">TREND ALIGNMENT</span>
        <span className="muted small" role="alert">
          TREND READ UNAVAILABLE — {reading.error}
        </span>
        <button type="button" className="btn btn-ghost btn-sm" onClick={reading.reload}>
          Retry
        </button>
      </div>
    )
  }

  if (reading.loading || data === null) {
    return (
      <div className="tchart-overlay-block" aria-label="Trend alignment overlay">
        <span className="tchart-overlay-title">TREND ALIGNMENT</span>
        <span className="muted small" role="status">
          Loading trend read…
        </span>
      </div>
    )
  }

  return (
    <div className="tchart-overlay-block" aria-label="Trend alignment overlay">
      <span className="tchart-overlay-title">TREND ALIGNMENT</span>
      <ul className="tchart-trend-rows">
        {TIMEFRAME_ORDER.map((key) => {
          const timeframe = data.timeframes?.[key] ?? null
          const trend: TrendState = timeframe?.trend ?? 'unavailable'
          return (
            <li
              key={key}
              className={`tchart-trend-row trend-${trend}`}
              aria-label={`${TIMEFRAME_LABEL[key]} ${TREND_LABEL[trend]}`}
              title={timeframe?.reason ? timeframe.reason : undefined}
            >
              <span className="mono">{TIMEFRAME_LABEL[key]}</span>{' '}
              <span aria-hidden="true">{TREND_GLYPH[trend]}</span> {TREND_LABEL[trend]}
            </li>
          )
        })}
      </ul>
      <span className={`badge trend-align-${data.alignment}`}>
        <span aria-hidden="true">{ALIGNMENT_GLYPH[data.alignment]}</span>{' '}
        Overall: {ALIGNMENT_LABEL[data.alignment]}
      </span>
      {data.data_fresh === false && (
        <span className="badge badge-warn" title="At least one timeframe is running on old candles">
          STALE
        </span>
      )}
      <span className="muted small tchart-overlay-note">
        The server’s read across three timeframes on closed candles. The chart does not derive it
        from the lines on screen.
      </span>
    </div>
  )
}
