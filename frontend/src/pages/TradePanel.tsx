import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useApi } from '../hooks/useApi'
import type {
  ChartCandle,
  CurrentPricesResponse,
  NewsFeedResponse,
  NewsItem,
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
import { TradingChart, type ChartHandle } from '../chart/TradingChart'
import { ChartToolbar, useChartFullscreen } from '../chart/ChartToolbar'
import { OhlcHeader } from '../chart/OhlcHeader'
import { ChartStatusBar } from '../chart/ChartStatusBar'
import { useCandles } from '../chart/useCandles'
import { IndicatorMenu } from '../chart/IndicatorMenu'
import { ChartLegend } from '../chart/ChartLegend'
import { IndicatorPanes, PANE_HEIGHT } from '../chart/indicators/panes'
import { useIndicatorSeries } from '../chart/indicators/series'
import {
  buildPlots,
  coldInstances,
  hasInstance,
  loadIndicatorState,
  saveIndicatorState,
  serverOverlays,
  type ChartIndicatorState
} from '../chart/indicators/registry'
import {
  forecastPlots,
  forecastPoints,
  placeEvents,
  useEventMarkers,
  useTrendOverlay
} from '../chart/overlays'
import { DrawingToolbar } from '../chart/DrawingToolbar'
import { DrawingLayer } from '../chart/drawings/DrawingLayer'
import { useDrawings } from '../chart/drawings/useDrawings'
import {
  FALLBACK_INTERVAL,
  intervalLabel,
  intervalSeconds,
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

/** The API's 400 for a timeframe this data source cannot bucket. */
const UNSUPPORTED_RE = /not available for the current data source/i

/** Headlines the event overlay considers; the feed is ordered urgent-first. */
const NEWS_PATH = '/intelligence/news?limit=50'

export default function TradePanel() {
  const { unit } = useSettings()
  const shellRef = useRef<HTMLDivElement | null>(null)

  const [symbol, setSymbol] = useState<ChartSymbol>(() => readSymbol())
  const [interval, setIntervalId] = useState<IntervalId>(() => readInterval() ?? FALLBACK_INTERVAL)
  const [notice, setNotice] = useState<string | null>(null)
  const [hovered, setHovered] = useState<ChartCandle | null>(null)
  const [indicators, setIndicators] = useState<ChartIndicatorState>(() => loadIndicatorState())
  const [chart, setChart] = useState<ChartHandle | null>(null)

  const candles = useCandles(symbol, interval)
  const { fullscreen, toggle: toggleFullscreen } = useChartFullscreen(shellRef)

  // Drawings are scoped to (symbol, interval) by the hook itself; the seconds
  // are only what a duplicate is offset by so it does not land under the original.
  const drawings = useDrawings(symbol, interval, { intervalSeconds: intervalSeconds(interval) })

  useEffect(() => {
    saveIndicatorState(indicators)
  }, [indicators])

  const onChartReady = useCallback((handle: ChartHandle) => setChart(handle), [])

  // TradingChart unmounts — and disposes its chart — the moment the candle list
  // empties, so the handle has to go with it. An indicator layer addressing a
  // disposed chart is the one way this page can throw from inside an effect.
  useEffect(() => {
    if (candles.candles.length === 0) setChart(null)
  }, [candles.candles.length])

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

  // ---- indicators -------------------------------------------------------
  // The server's own overlay arrays still travel through TradingChart's
  // `overlays` prop; only the series the API cannot give us for an arbitrary
  // timeframe (the EMA preset, RSI, MACD) are attached by the indicator layer.
  const plots = useMemo(
    () =>
      buildPlots(indicators.instances, {
        candles: candles.candles,
        overlays: candles.overlays,
        overlayTimes: candles.overlayTimes
      }),
    [indicators.instances, candles.candles, candles.overlays, candles.overlayTimes]
  )

  const shownPlots = useMemo(() => {
    const shown = new Set(indicators.instances.filter((i) => i.visible).map((i) => i.id))
    return plots.filter((p) => shown.has(p.instanceId))
  }, [plots, indicators.instances])

  const mainPlots = useMemo(
    () => shownPlots.filter((p) => p.owner === 'layer' && p.paneKey === null),
    [shownPlots]
  )
  const panePlots = useMemo(() => shownPlots.filter((p) => p.paneKey !== null), [shownPlots])
  const paneCount = useMemo(() => new Set(panePlots.map((p) => p.paneKey)).size, [panePlots])

  const visibleOverlays = useMemo(
    () => serverOverlays(indicators.instances, candles.overlays),
    [indicators.instances, candles.overlays]
  )

  const cold = useMemo(() => coldInstances(indicators.instances, plots), [indicators.instances, plots])

  const levels = useMemo(
    () => ({
      pivots: hasInstance(indicators.instances, 'pivots') ? candles.pivots : null,
      support: hasInstance(indicators.instances, 'sr') ? candles.support : null,
      resistance: hasInstance(indicators.instances, 'sr') ? candles.resistance : null
    }),
    [indicators.instances, candles.pivots, candles.support, candles.resistance]
  )

  // ---- overlays ---------------------------------------------------------
  const lastCandle =
    candles.candles.length > 0 ? candles.candles[candles.candles.length - 1] : null

  const forecast = useMemo(
    () => (indicators.overlays.forecast ? forecastPoints(predictions, lastCandle) : []),
    [indicators.overlays.forecast, predictions, lastCandle]
  )
  const forecastSeries = useMemo(() => forecastPlots(forecast), [forecast])

  const news = useApi<NewsFeedResponse>(indicators.overlays.events ? NEWS_PATH : null, [
    indicators.overlays.events
  ])
  const eventPlacement = useMemo(
    () =>
      placeEvents(
        unwrapList<NewsItem>(news.data, 'items'),
        candles.candles,
        intervalSeconds(interval)
      ),
    [news.data, candles.candles, interval]
  )
  const trend = useTrendOverlay(symbol, indicators.overlays.trend)

  useIndicatorSeries(chart?.chart ?? null, mainPlots)
  useIndicatorSeries(chart?.chart ?? null, forecastSeries)
  useEventMarkers(chart?.candleSeries ?? null, eventPlacement.events, indicators.overlays.events)

  const baseHeight = fullscreen ? Math.max(window.innerHeight - 260, 320) : 440
  const chartHeight = baseHeight + PANE_HEIGHT * paneCount

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
              indicatorsSlot={<IndicatorMenu state={indicators} onChange={setIndicators} />}
              drawSlot={<DrawingToolbar engine={drawings} />}
            />

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
                  height={chartHeight}
                  onCrosshair={setHovered}
                  onReady={onChartReady}
                  onLoadOlder={candles.loadOlder}
                  levels={levels}
                >
                  {/* Inside the chart container so the overlay canvas shares the
                      candles' coordinate space with no extra maths. */}
                  <DrawingLayer
                    handle={chart}
                    engine={drawings}
                    symbol={symbol}
                    interval={interval}
                    unit={unit}
                    candles={candles.candles}
                  />
                </TradingChart>
                <IndicatorPanes
                  chart={chart?.chart ?? null}
                  plots={panePlots}
                  height={chartHeight}
                  time={hovered?.t ?? null}
                  symbol={symbol}
                  unit={unit}
                />
                {candles.loadingOlder && (
                  <p className="muted small" role="status">
                    Loading older history…
                  </p>
                )}
              </>
            )}

            <ChartLegend
              state={indicators}
              onChange={setIndicators}
              plots={plots}
              time={hovered?.t ?? null}
              symbol={symbol}
              unit={unit}
              levels={{
                pivots: candles.pivots,
                support: candles.support,
                resistance: candles.resistance
              }}
              cold={cold}
              forecast={{ points: forecast, loading: latest.loading, error: latest.error }}
              events={{
                placement: eventPlacement,
                loading: news.loading,
                error: news.error,
                collectionEnabled: news.data?.collection_enabled !== false
              }}
              trend={trend}
            />

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
