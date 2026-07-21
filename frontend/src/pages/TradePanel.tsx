import { useEffect, useMemo, useRef, useState } from 'react'
import {
  CandlestickSeries,
  ColorType,
  LineSeries,
  LineStyle,
  createChart,
  type IChartApi,
  type UTCTimestamp
} from 'lightweight-charts'
import { useApi } from '../hooks/useApi'
import type {
  Candle,
  CandlesResponse,
  CurrentPricesResponse,
  Prediction,
  ProviderGapResponse,
  SignalSummary
} from '../api/types'
import { HORIZON_LABELS, type Horizon } from '../api/types'
import { unwrapList } from '../lib/unwrap'
import { useSettings } from '../lib/settings'
import { formatPct, formatToman, pctClass } from '../lib/format'
import DataFreshness from '../components/DataFreshness'
import GaugeBar from '../components/GaugeBar'
import Loading from '../components/Loading'
import ErrorMessage from '../components/ErrorMessage'
import EmptyState from '../components/EmptyState'

type OverlayKey = 'sma' | 'bollinger' | 'ichimoku' | 'supertrend' | 'psar' | 'pivots'

const OVERLAY_LABELS: Record<OverlayKey, string> = {
  sma: 'SMA 20/50',
  bollinger: 'Bollinger',
  ichimoku: 'Ichimoku',
  supertrend: 'SuperTrend',
  psar: 'PSAR',
  pivots: 'Pivots'
}

const RANGES: Record<'daily' | 'hourly', Array<{ days: number; label: string }>> = {
  daily: [
    { days: 60, label: '60D' },
    { days: 120, label: '120D' },
    { days: 250, label: '250D' },
    { days: 500, label: '500D' }
  ],
  hourly: [
    { days: 3, label: '3D' },
    { days: 7, label: '7D' },
    { days: 14, label: '14D' }
  ]
}

/** Resolve the current theme's CSS variables to concrete colors for the chart. */
function chartColors() {
  const css = getComputedStyle(document.documentElement)
  const v = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback
  return {
    bg: v('--panel', '#131b24'),
    text: v('--muted', '#8b98a5'),
    border: v('--border', '#22303f'),
    pos: v('--pos', '#2ea36b'),
    neg: v('--neg', '#e5534b'),
    accent: v('--accent', '#d4a017'),
    info: v('--info', '#58a6ff'),
    warn: v('--warn', '#d29922'),
    purple: v('--purple', '#a371f7')
  }
}

function lineData(candles: Candle[], values: Array<number | null>) {
  const out: Array<{ time: UTCTimestamp; value: number }> = []
  for (let i = 0; i < candles.length; i++) {
    const v = values[i]
    if (v !== null && v !== undefined) out.push({ time: candles[i].t as UTCTimestamp, value: v })
  }
  return out
}

function CandleChart({
  data,
  overlays,
  height
}: {
  data: CandlesResponse
  overlays: Set<OverlayKey>
  height: number
}) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const [themeTick, setThemeTick] = useState(0)

  // re-render the chart when the user flips the site theme
  useEffect(() => {
    const observer = new MutationObserver(() => setThemeTick((t) => t + 1))
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    const el = containerRef.current
    if (!el || data.candles.length === 0) return
    const c = chartColors()

    const chart = createChart(el, {
      height,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: c.text,
        attributionLogo: false
      },
      grid: {
        vertLines: { color: c.border, style: LineStyle.Dotted },
        horzLines: { color: c.border, style: LineStyle.Dotted }
      },
      rightPriceScale: { borderColor: c.border },
      timeScale: {
        borderColor: c.border,
        timeVisible: data.interval === 'hourly',
        secondsVisible: false
      },
      crosshair: { horzLine: { labelBackgroundColor: c.accent }, vertLine: { labelBackgroundColor: c.accent } },
      localization: {
        priceFormatter: (p: number) => Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(p)
      }
    })
    chartRef.current = chart

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: c.pos,
      downColor: c.neg,
      borderUpColor: c.pos,
      borderDownColor: c.neg,
      wickUpColor: c.pos,
      wickDownColor: c.neg
    })
    candleSeries.setData(
      data.candles.map((k) => ({
        time: k.t as UTCTimestamp,
        open: k.open,
        high: k.high,
        low: k.low,
        close: k.close
      }))
    )

    const addLine = (
      values: Array<number | null>,
      color: string,
      width: 1 | 2 = 1,
      style: LineStyle = LineStyle.Solid,
      markersOnly = false
    ) => {
      const series = chart.addSeries(LineSeries, {
        color,
        lineWidth: width,
        lineStyle: style,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
        ...(markersOnly
          ? { lineVisible: false, pointMarkersVisible: true, pointMarkersRadius: 2 }
          : {})
      })
      series.setData(lineData(data.candles, values))
      return series
    }

    const o = data.overlays
    if (overlays.has('sma')) {
      addLine(o.sma_20, c.info, 1)
      addLine(o.sma_50, c.purple, 1)
    }
    if (overlays.has('bollinger')) {
      addLine(o.bollinger_upper, c.info, 1, LineStyle.Dashed)
      addLine(o.bollinger_mid, c.info, 1, LineStyle.Dotted)
      addLine(o.bollinger_lower, c.info, 1, LineStyle.Dashed)
    }
    if (overlays.has('ichimoku')) {
      addLine(o.ichimoku_tenkan, c.pos, 1)
      addLine(o.ichimoku_kijun, c.neg, 1)
      addLine(o.ichimoku_senkou_a, c.accent, 1, LineStyle.Dashed)
      addLine(o.ichimoku_senkou_b, c.purple, 1, LineStyle.Dashed)
    }
    if (overlays.has('supertrend')) {
      addLine(o.supertrend, c.warn, 2)
    }
    if (overlays.has('psar')) {
      addLine(o.psar, c.accent, 1, LineStyle.Solid, true)
    }
    if (overlays.has('pivots') && data.pivots) {
      const piv = data.pivots
      const levels: Array<[number, string, string]> = [
        [piv.r3, 'R3', c.neg],
        [piv.r2, 'R2', c.neg],
        [piv.r1, 'R1', c.neg],
        [piv.p, 'P', c.text],
        [piv.s1, 'S1', c.pos],
        [piv.s2, 'S2', c.pos],
        [piv.s3, 'S3', c.pos]
      ]
      for (const [price, title, color] of levels) {
        candleSeries.createPriceLine({
          price,
          title,
          color,
          lineWidth: 1,
          lineStyle: LineStyle.SparseDotted,
          axisLabelVisible: false
        })
      }
    }
    if (data.support !== null) {
      candleSeries.createPriceLine({
        price: data.support,
        title: 'support',
        color: c.pos,
        lineWidth: 1,
        lineStyle: LineStyle.LargeDashed,
        axisLabelVisible: false
      })
    }
    if (data.resistance !== null) {
      candleSeries.createPriceLine({
        price: data.resistance,
        title: 'resistance',
        color: c.neg,
        lineWidth: 1,
        lineStyle: LineStyle.LargeDashed,
        axisLabelVisible: false
      })
    }

    chart.timeScale().fitContent()

    const onResize = () => chart.applyOptions({ width: el.clientWidth })
    onResize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      chart.remove()
      chartRef.current = null
    }
  }, [data, overlays, height, themeTick])

  return <div ref={containerRef} style={{ width: '100%' }} />
}

export default function TradePanel() {
  const { unit } = useSettings()
  const [interval, setInterval_] = useState<'daily' | 'hourly'>('daily')
  const [days, setDays] = useState(120)
  const [active, setActive] = useState<Set<OverlayKey>>(new Set(['sma', 'supertrend', 'pivots']))

  const candles = useApi<CandlesResponse>(`/market/candles?interval=${interval}&days=${days}`)
  const current = useApi<CurrentPricesResponse>('/prices/current')
  const signal = useApi<SignalSummary>('/signals/current')
  const latest = useApi<unknown>('/predictions')
  const gap = useApi<ProviderGapResponse>('/market/provider-gap?symbol=IR_GOLD_18K&history_days=0')

  // keep the desk live: refresh quotes and candles every minute
  useEffect(() => {
    const id = window.setInterval(() => {
      candles.reload()
      current.reload()
      gap.reload()
    }, 60_000)
    return () => window.clearInterval(id)
  }, [candles.reload, current.reload, gap.reload]) // eslint-disable-line react-hooks/exhaustive-deps

  const predictions = useMemo(
    () => unwrapList<Prediction>(latest.data, 'items', 'predictions'),
    [latest.data]
  )

  const toggle = (key: OverlayKey) =>
    setActive((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })

  const switchInterval = (next: 'daily' | 'hourly') => {
    setInterval_(next)
    setDays(RANGES[next][1]?.days ?? RANGES[next][0].days)
  }

  const gold = current.data?.prices?.IR_GOLD_18K
  const st = candles.data?.overlays.supertrend_dir
  const stDir = st && st.length > 0 ? st[st.length - 1] : 0

  return (
    <div className="page-body">
      <h2 className="page-title">Trade panel</h2>

      <div className="trade-layout">
        <div className="trade-main">
          <div className="card">
            <div className="toolbar" style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap', alignItems: 'center' }}>
              <div className="toggle-group" role="group" aria-label="Interval">
                <button type="button" className={interval === 'daily' ? 'active' : ''} onClick={() => switchInterval('daily')}>
                  Daily
                </button>
                <button type="button" className={interval === 'hourly' ? 'active' : ''} onClick={() => switchInterval('hourly')}>
                  Hourly
                </button>
              </div>
              <div className="toggle-group" role="group" aria-label="Range">
                {RANGES[interval].map((r) => (
                  <button
                    key={r.days}
                    type="button"
                    className={days === r.days ? 'active' : ''}
                    onClick={() => setDays(r.days)}
                  >
                    {r.label}
                  </button>
                ))}
              </div>
              <span style={{ flex: 1 }} />
              {(Object.keys(OVERLAY_LABELS) as OverlayKey[]).map((key) => (
                <button
                  key={key}
                  type="button"
                  className={`btn btn-sm ${active.has(key) ? '' : 'btn-ghost'}`}
                  aria-pressed={active.has(key)}
                  onClick={() => toggle(key)}
                >
                  {OVERLAY_LABELS[key]}
                </button>
              ))}
            </div>

            {candles.loading ? (
              <Loading label="Loading candles…" />
            ) : candles.error ? (
              <ErrorMessage message={candles.error} onRetry={candles.reload} />
            ) : candles.data && candles.data.candles.length > 0 ? (
              <CandleChart data={candles.data} overlays={active} height={440} />
            ) : (
              <EmptyState title="No candle data" hint="Candles appear once price history exists." />
            )}
          </div>
        </div>

        <aside className="trade-side">
          <div className="card">
            <div className="card-title">IR_GOLD_18K</div>
            {gold ? (
              <>
                <div className="stat-value big-price">{formatToman(gold.value, unit)}</div>
                <div className={`delta ${pctClass(gold.change_24h_pct)}`}>
                  {formatPct(gold.change_24h_pct)} · 24h
                </div>
                <DataFreshness timestamp={gold.observed_at} stale={gold.stale} marketState={gold.market_state} />
                {stDir !== 0 && (
                  <div className="kv" style={{ marginTop: '0.5rem' }}>
                    <span className="muted">SuperTrend</span>
                    <span className={stDir === 1 ? 'pos' : 'neg'}>
                      {stDir === 1 ? '▲ bullish' : '▼ bearish'}
                    </span>
                  </div>
                )}
              </>
            ) : (
              <span className="muted small">{current.loading ? 'Loading…' : 'No quote'}</span>
            )}
          </div>

          <div className="card">
            <div className="card-title">Signal</div>
            {signal.data ? (
              <>
                <div className={`signal-level sig-${signal.data.signal}`}>
                  {signal.data.signal.replace('_', ' ').toUpperCase()}
                </div>
                <GaugeBar value={signal.data.score} label={`Score ${signal.data.score}/100`} />
                <p className="muted small">{signal.data.explanation}</p>
              </>
            ) : signal.loading ? (
              <Loading label="Loading signal…" />
            ) : (
              <span className="muted small">No signal yet</span>
            )}
          </div>

          <div className="card">
            <div className="card-title">Forecasts</div>
            {predictions.length > 0 ? (
              <ul className="driver-list">
                {predictions.map((p) => {
                  const pct = p.expected_change_pct
                  return (
                    <li key={p.horizon} className="driver-row">
                      <span className="driver-name">
                        {HORIZON_LABELS[p.horizon as Horizon] ?? p.horizon}
                      </span>
                      <span className={`mono ${pctClass(pct)}`}>
                        {p.direction === 'up' ? '▲' : p.direction === 'down' ? '▼' : '▶'}{' '}
                        {formatPct(pct)}
                      </span>
                    </li>
                  )
                })}
              </ul>
            ) : latest.loading ? (
              <Loading label="Loading forecasts…" />
            ) : (
              <span className="muted small">No predictions yet</span>
            )}
          </div>

          <div className="card">
            <div className="card-title">Provider quotes</div>
            {(gap.data?.providers ?? []).length > 0 ? (
              <ul className="driver-list">
                {(gap.data?.providers ?? [])
                  .slice()
                  .sort((a, b) => b.value - a.value)
                  .map((q) => (
                    <li key={q.provider} className="driver-row">
                      <span className="driver-name mono">{q.provider}</span>
                      <span className="mono">{formatToman(q.value, unit, false)}</span>
                    </li>
                  ))}
              </ul>
            ) : (
              <span className="muted small">No fresh quotes in window</span>
            )}
            {gap.data?.gap_pct != null && (
              <div className="kv">
                <span className="muted">Spread</span>
                <span className={`mono ${gap.data.gap_pct >= 1 ? 'neg' : ''}`}>
                  {formatPct(gap.data.gap_pct)}
                </span>
              </div>
            )}
          </div>

          {candles.data?.pivots && (
            <div className="card">
              <div className="card-title">Pivot levels (classic)</div>
              <div className="table-wrap">
                <table className="table">
                  <tbody>
                    {(
                      [
                        ['R3', candles.data.pivots.r3, 'neg'],
                        ['R2', candles.data.pivots.r2, 'neg'],
                        ['R1', candles.data.pivots.r1, 'neg'],
                        ['P', candles.data.pivots.p, ''],
                        ['S1', candles.data.pivots.s1, 'pos'],
                        ['S2', candles.data.pivots.s2, 'pos'],
                        ['S3', candles.data.pivots.s3, 'pos']
                      ] as Array<[string, number, string]>
                    ).map(([label, value, cls]) => (
                      <tr key={label}>
                        <td className={cls}>{label}</td>
                        <td className="num mono">{formatToman(value, unit, false)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </aside>
      </div>
    </div>
  )
}
