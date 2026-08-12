import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useApi } from '../hooks/useApi'
import type {
  CandleOverlays,
  ChartCandle,
  CurrentPricesResponse,
  Prediction,
  ProviderGapResponse,
  SignalSummary
} from '../api/types'
import { GOLD_FUND_SYMBOLS, HORIZON_LABELS, SYMBOL_LABELS, type Horizon } from '../api/types'
import { unwrapList } from '../lib/unwrap'
import { useSettings } from '../lib/settings'
import { formatPct, formatToman, pctClass } from '../lib/format'
import DataFreshness from '../components/DataFreshness'
import GaugeBar from '../components/GaugeBar'
import Loading from '../components/Loading'
import ErrorMessage from '../components/ErrorMessage'
import EmptyState from '../components/EmptyState'
import { TradingChart } from '../chart/TradingChart'
import { ChartToolbar, useChartFullscreen } from '../chart/ChartToolbar'
import { OhlcHeader } from '../chart/OhlcHeader'
import { ChartStatusBar } from '../chart/ChartStatusBar'
import { useCandles } from '../chart/useCandles'
import {
  FALLBACK_INTERVAL,
  intervalLabel,
  isSupported,
  type IntervalId
} from '../chart/intervals'
import {
  DEFAULT_SYMBOL,
  readInterval,
  readSymbol,
  writeInterval,
  writeSymbol,
  type ChartSymbol
} from '../chart/prefs'

type OverlayKey = 'sma' | 'bollinger' | 'ichimoku' | 'supertrend' | 'psar' | 'pivots'

const OVERLAY_LABELS: Record<OverlayKey, string> = {
  sma: 'SMA 20/50',
  bollinger: 'Bollinger',
  ichimoku: 'Ichimoku',
  supertrend: 'SuperTrend',
  psar: 'PSAR',
  pivots: 'Pivots'
}

/** Which response arrays each toggle controls. 'pivots' draws price lines instead. */
const OVERLAY_FIELDS: Record<Exclude<OverlayKey, 'pivots'>, Array<keyof CandleOverlays>> = {
  sma: ['sma_20', 'sma_50'],
  bollinger: ['bollinger_upper', 'bollinger_mid', 'bollinger_lower'],
  ichimoku: ['ichimoku_tenkan', 'ichimoku_kijun', 'ichimoku_senkou_a', 'ichimoku_senkou_b'],
  supertrend: ['supertrend'],
  psar: ['psar']
}

/** The API's 400 for a timeframe this data source cannot bucket. */
const UNSUPPORTED_RE = /not available for the current data source/i

export default function TradePanel() {
  const { unit } = useSettings()
  const shellRef = useRef<HTMLDivElement | null>(null)

  const [symbol, setSymbol] = useState<ChartSymbol>(() => readSymbol())
  const [interval, setIntervalId] = useState<IntervalId>(() => readInterval() ?? FALLBACK_INTERVAL)
  const [notice, setNotice] = useState<string | null>(null)
  const [hovered, setHovered] = useState<ChartCandle | null>(null)
  const [active, setActive] = useState<Set<OverlayKey>>(new Set(['sma', 'supertrend', 'pivots']))

  const candles = useCandles(symbol, interval)
  const { fullscreen, toggle: toggleFullscreen } = useChartFullscreen(shellRef)

  const current = useApi<CurrentPricesResponse>('/prices/current')
  const signal = useApi<SignalSummary>('/signals/current')
  const latest = useApi<unknown>('/predictions')
  const gap = useApi<ProviderGapResponse>('/market/provider-gap?symbol=IR_GOLD_18K&history_days=0')

  // The candle store runs its own live tail poll; these side cards still need a
  // whole-response refresh every minute.
  useEffect(() => {
    const id = window.setInterval(() => {
      current.reload()
      gap.reload()
    }, 60_000)
    return () => window.clearInterval(id)
  }, [current.reload, gap.reload]) // eslint-disable-line react-hooks/exhaustive-deps

  const chooseInterval = useCallback((next: IntervalId) => {
    setNotice(null)
    setHovered(null)
    setIntervalId(next)
    writeInterval(next)
  }, [])

  const chooseSymbol = useCallback((next: ChartSymbol) => {
    setNotice(null)
    setHovered(null)
    setSymbol(next)
    writeSymbol(next)
  }, [])

  // A stored timeframe can stop being servable when coverage changes. Fall back
  // to 1D and keep the reason on screen: silently drawing different bars than
  // the button says would be worse than the empty chart it replaces.
  useEffect(() => {
    if (!candles.coverage) return
    const support = isSupported(interval, candles.coverage)
    if (support.ok) return
    setNotice(
      `${intervalLabel(interval)} is unavailable — showing ${intervalLabel(
        FALLBACK_INTERVAL
      )} instead. ${support.reason}`
    )
    setIntervalId(FALLBACK_INTERVAL)
    writeInterval(FALLBACK_INTERVAL)
  }, [candles.coverage, interval])

  // A rejected timeframe 400s, and a 400 carries no coverage — so the guard
  // above can never fire for it. Read the refusal itself instead.
  useEffect(() => {
    if (!candles.error || interval === FALLBACK_INTERVAL) return
    if (!UNSUPPORTED_RE.test(candles.error)) return
    setNotice(
      `${intervalLabel(interval)} is unavailable — showing ${intervalLabel(
        FALLBACK_INTERVAL
      )} instead. ${candles.error}`
    )
    setIntervalId(FALLBACK_INTERVAL)
    writeInterval(FALLBACK_INTERVAL)
  }, [candles.error, interval])

  const predictions = useMemo(
    () => unwrapList<Prediction>(latest.data, 'items', 'predictions'),
    [latest.data]
  )

  const visibleOverlays = useMemo(() => {
    const all = candles.overlays
    if (!all) return null
    const picked: Partial<CandleOverlays> = {}
    for (const key of Object.keys(OVERLAY_FIELDS) as Array<keyof typeof OVERLAY_FIELDS>) {
      if (!active.has(key)) continue
      for (const field of OVERLAY_FIELDS[key]) {
        const values = all[field]
        if (Array.isArray(values)) {
          // supertrend_dir is a direction array, not a price series.
          ;(picked as Record<string, unknown>)[field] = values
        }
      }
    }
    return picked
  }, [candles.overlays, active])

  // An indicator whose every value is null has not warmed up in this window.
  const warmupWarning = useMemo(() => {
    const all = candles.overlays
    if (!all) return null
    const cold: string[] = []
    for (const key of Object.keys(OVERLAY_FIELDS) as Array<keyof typeof OVERLAY_FIELDS>) {
      if (!active.has(key)) continue
      const fields = OVERLAY_FIELDS[key]
      const anyValue = fields.some((field) => {
        const values = all[field]
        return Array.isArray(values) && values.some((v) => v !== null && v !== undefined)
      })
      if (!anyValue) cold.push(OVERLAY_LABELS[key])
    }
    if (cold.length === 0) return null
    return `${cold.join(', ')} needs more history than this window holds.`
  }, [candles.overlays, active])

  const levels = useMemo(
    () => ({
      pivots: active.has('pivots') ? candles.pivots : null,
      support: candles.support,
      resistance: candles.resistance
    }),
    [active, candles.pivots, candles.support, candles.resistance]
  )

  const toggle = (key: OverlayKey) =>
    setActive((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })

  const quote = current.data?.prices?.[symbol]
  const gold = current.data?.prices?.IR_GOLD_18K
  const st = candles.overlays?.supertrend_dir
  const stDir = st && st.length > 0 ? st[st.length - 1] : 0
  const firstLoad = candles.loading && candles.candles.length === 0

  return (
    <div className="page-body">
      <h2 className="page-title">Trade panel</h2>

      <div className="trade-layout">
        <div className="trade-main">
          <div
            className={`card tchart-shell ${fullscreen ? 'tchart-shell-fs' : ''}`}
            ref={shellRef}
          >
            <ChartToolbar
              symbol={symbol}
              onSymbolChange={chooseSymbol}
              interval={interval}
              onIntervalChange={chooseInterval}
              coverage={candles.coverage}
              fullscreen={fullscreen}
              onToggleFullscreen={toggleFullscreen}
            />

            <div className="tchart-overlays">
              {(Object.keys(OVERLAY_LABELS) as OverlayKey[]).map((key) => (
                <button
                  key={key}
                  type="button"
                  className={`btn btn-sm ${active.has(key) ? '' : 'btn-ghost'}`}
                  aria-label={`${OVERLAY_LABELS[key]} overlay`}
                  aria-pressed={active.has(key)}
                  onClick={() => toggle(key)}
                >
                  {OVERLAY_LABELS[key]}
                </button>
              ))}
            </div>

            {notice && (
              <p className="tchart-notice muted small" role="status">
                {notice}
              </p>
            )}

            <OhlcHeader
              symbol={symbol}
              interval={interval}
              hovered={hovered}
              candles={candles.candles}
              unit={unit}
            />

            {firstLoad ? (
              <Loading label="Loading candles…" />
            ) : candles.error && candles.candles.length === 0 ? (
              <ErrorMessage message={candles.error} onRetry={candles.reload} />
            ) : candles.candles.length === 0 ? (
              <EmptyState
                title="No candle data"
                hint="Candles appear once price history exists for this symbol and timeframe."
              />
            ) : (
              <>
                <TradingChart
                  candles={candles.candles}
                  overlays={visibleOverlays}
                  overlayTimes={candles.overlayTimes}
                  interval={interval}
                  unit={unit}
                  symbol={symbol}
                  height={fullscreen ? Math.max(window.innerHeight - 260, 320) : 440}
                  onCrosshair={setHovered}
                  onLoadOlder={candles.loadOlder}
                  levels={levels}
                />
                {candles.loadingOlder && (
                  <p className="muted small" role="status">
                    Loading older history…
                  </p>
                )}
                {warmupWarning && (
                  <p className="muted small tchart-notice">{warmupWarning}</p>
                )}
              </>
            )}

            <ChartStatusBar
              asOf={candles.asOf}
              interval={interval}
              candles={candles.candles}
              coverage={candles.coverage}
              source={quote?.source ?? null}
              stale={quote?.stale}
            />
          </div>
        </div>

        <aside className="trade-side">
          <div className="card">
            <div className="card-title">IR_GOLD_18K</div>
            {gold ? (
              <>
                <div className="stat-value big-price">{formatToman(gold.value, unit, false)}</div>
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

          {GOLD_FUND_SYMBOLS.some((s) => current.data?.prices?.[s]) && (
            <div className="card">
              <div className="card-title">Gold funds (TSE, 12:00–18:00)</div>
              <ul className="driver-list">
                {GOLD_FUND_SYMBOLS.map((sym) => {
                  const q = current.data?.prices?.[sym]
                  if (!q) return null
                  return (
                    <li key={sym} className="driver-row">
                      <span className="driver-name">{SYMBOL_LABELS[sym]}</span>
                      <span className={`mono ${pctClass(q.change_24h_pct)}`}>
                        {formatToman(q.value, unit, false)}{' '}
                        {q.change_24h_pct !== null ? formatPct(q.change_24h_pct) : ''}
                      </span>
                    </li>
                  )
                })}
              </ul>
              {current.data?.prices?.IR_GOLD_FUND_FLOW && (
                <div className="kv">
                  <span className="muted">Retail net flow</span>
                  <span
                    className={`mono ${
                      current.data.prices.IR_GOLD_FUND_FLOW.value > 0 ? 'pos' : 'neg'
                    }`}
                  >
                    {formatPct(current.data.prices.IR_GOLD_FUND_FLOW.value)} of volume
                  </span>
                </div>
              )}
            </div>
          )}

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

          {candles.pivots && (
            <div className="card">
              <div className="card-title">Pivot levels (classic)</div>
              <div className="table-wrap">
                <table className="table">
                  <tbody>
                    {(
                      [
                        ['R3', candles.pivots.r3, 'neg'],
                        ['R2', candles.pivots.r2, 'neg'],
                        ['R1', candles.pivots.r1, 'neg'],
                        ['P', candles.pivots.p, ''],
                        ['S1', candles.pivots.s1, 'pos'],
                        ['S2', candles.pivots.s2, 'pos'],
                        ['S3', candles.pivots.s3, 'pos']
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
