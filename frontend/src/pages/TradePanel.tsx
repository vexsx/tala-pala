import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useApi } from '../hooks/useApi'
import type {
  ChartCandle,
  ChartEquityInstrument,
  ChartIndexInstrument,
  CurrentPrice,
  CurrentPricesResponse,
  NewsFeedResponse,
  NewsItem,
  Prediction,
  ProviderGapResponse,
  SignalSummary,
  Symbol_
} from '../api/types'
import { GOLD_FUND_SYMBOLS, HORIZON_LABELS, SYMBOL_LABELS, type Horizon } from '../api/types'
import { unwrapList } from '../lib/unwrap'
import { useSettings } from '../lib/settings'
import { formatIndexLevel } from '../lib/bourse'
import { formatPct, formatToman, pctClass } from '../lib/format'
import DataFreshness from '../components/DataFreshness'
import GaugeBar from '../components/GaugeBar'
import Loading from '../components/Loading'
import ErrorMessage from '../components/ErrorMessage'
import EmptyState from '../components/EmptyState'
import { BourseAgeNotice } from './Bourse'
import { TradingChart, type ChartHandle } from '../chart/TradingChart'
import { ChartToolbar, useChartFullscreen } from '../chart/ChartToolbar'
import { OhlcHeader, formatChartPrice } from '../chart/OhlcHeader'
import { ChartStatusBar } from '../chart/ChartStatusBar'
import { useCandles } from '../chart/useCandles'
import { useChartCatalog } from '../chart/catalog'
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
  type ChartIndicatorState,
  type IndicatorKind,
  type OverlayToggles
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
  readInterval,
  readLogScale,
  readSymbol,
  writeInterval,
  writeLogScale,
  writeSymbol
} from '../chart/prefs'
import {
  TSE_COVERAGE,
  indicatorSupport,
  isTehranSymbol,
  overlaySupport,
  seriesModeFor,
  symbolIntervals,
  symbolKind,
  type ChartSymbol
} from '../chart/symbols'

/** The API's 400 for a timeframe this data source cannot bucket. */
const UNSUPPORTED_RE = /not available for the current data source/i

/** Headlines the event overlay considers; the feed is ordered urgent-first. */
const NEWS_PATH = '/intelligence/news?limit=50'

/**
 * A Tehran series changes when an operator runs the off-server fetch, a few
 * times a week at most; polling it every minute would ask a question whose
 * answer cannot have changed. Ten minutes still notices a restatement the
 * same sitting it lands.
 */
const TSE_POLL_MS = 600_000

/**
 * The timeframe a symbol forces, with the reason, or null when the reader's
 * choice stands. The choice itself is left alone in storage — see prefs.ts.
 */
function forcedInterval(
  symbol: ChartSymbol,
  wanted: IntervalId
): { interval: IntervalId; notice: string } | null {
  const allowed = symbolIntervals(symbol)
  if (!allowed || allowed.includes(wanted)) return null
  return {
    interval: FALLBACK_INTERVAL,
    notice: `${intervalLabel(wanted)} is unavailable — showing ${intervalLabel(
      FALLBACK_INTERVAL
    )} instead. A Tehran market series is one settled close per session; your ${intervalLabel(
      wanted
    )} choice is kept for the other symbols.`
  }
}

function initialChart(): { symbol: ChartSymbol; interval: IntervalId; notice: string | null } {
  const symbol = readSymbol()
  const wanted = readInterval() ?? FALLBACK_INTERVAL
  const forced = forcedInterval(symbol, wanted)
  return { symbol, interval: forced?.interval ?? wanted, notice: forced?.notice ?? null }
}

function isEquityInstrument(
  inst: ChartIndexInstrument | ChartEquityInstrument
): inst is ChartEquityInstrument {
  return 'adjustment' in inst
}

/** What to call a symbol when the catalog has not named it yet. */
function fallbackLabel(
  symbol: ChartSymbol,
  inst: ChartIndexInstrument | ChartEquityInstrument | null
): string {
  if (inst) return isEquityInstrument(inst) ? inst.symbol : inst.name_en || inst.name_fa
  return SYMBOL_LABELS[symbol as Symbol_] ?? symbol
}

const WEIGHTING: Record<string, string> = {
  cap: 'Capitalisation-weighted',
  equal: 'Equal-weighted',
  free_float: 'Free-float weighted'
}

const BASIS: Record<string, string> = {
  total: 'total return (price and dividends)',
  price: 'price only (dividends excluded)'
}

export default function TradePanel() {
  const { unit, calendar } = useSettings()
  const shellRef = useRef<HTMLDivElement | null>(null)
  const catalog = useChartCatalog()

  const [initial] = useState(initialChart)
  const [symbol, setSymbol] = useState<ChartSymbol>(initial.symbol)
  const [interval, setIntervalId] = useState<IntervalId>(initial.interval)
  const [notice, setNotice] = useState<string | null>(initial.notice)
  const [hovered, setHovered] = useState<ChartCandle | null>(null)
  const [indicators, setIndicators] = useState<ChartIndicatorState>(() => loadIndicatorState())
  const [chart, setChart] = useState<ChartHandle | null>(null)
  const [logScale, setLogScale] = useState(() => readLogScale())

  const tehran = isTehranSymbol(symbol)
  const candles = useCandles(symbol, interval, tehran ? { pollMs: TSE_POLL_MS } : {})
  const { fullscreen, toggle: toggleFullscreen } = useChartFullscreen(shellRef)

  const option = catalog.find(symbol)
  const symbolLabel = option?.short ?? fallbackLabel(symbol, candles.instrument)
  const seriesMode = seriesModeFor(symbol, candles.priceFields)

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

  // ---- what may be drawn over this symbol ---------------------------------
  const forecastSupport = overlaySupport(symbol, 'forecast')
  const eventsSupport = overlaySupport(symbol, 'events')

  const current = useApi<CurrentPricesResponse>('/prices/current')
  // The signal is 18k gold's advisory. Beside a Tehran chart it would read as
  // advice about that index or share, so it is not fetched there at all.
  const signal = useApi<SignalSummary>(tehran ? null : '/signals/current')
  // The forecast of the CHARTED symbol, and only where one exists: asking
  // without ?symbol= got 18k gold's toman forecasts, which were then drawn
  // over XAU/USD.
  const latest = useApi<unknown>(
    forecastSupport.ok ? `/predictions?symbol=${encodeURIComponent(symbol)}` : null
  )
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

  // Only the reader's own choice is stored. A timeframe the data forces is
  // shown, never saved: see prefs.ts.
  const chooseInterval = useCallback(
    (next: IntervalId) => {
      setNotice(null)
      setHovered(null)
      setIntervalId(next)
      if (!isTehranSymbol(symbol)) writeInterval(next)
    },
    [symbol]
  )

  const chooseSymbol = useCallback(
    (next: ChartSymbol) => {
      setNotice(null)
      setHovered(null)
      setSymbol(next)
      writeSymbol(next)
      const forced = forcedInterval(next, interval)
      if (forced) {
        setIntervalId(forced.interval)
        setNotice(forced.notice)
        return
      }
      if (!symbolIntervals(next)) {
        // Back on a tick symbol: the reader's own timeframe returns.
        const wanted = readInterval() ?? FALLBACK_INTERVAL
        if (wanted !== interval) setIntervalId(wanted)
      }
    },
    [interval]
  )

  // A stored timeframe can stop being servable when coverage changes. Fall back
  // to 1D and keep the reason on screen: silently drawing different bars than
  // the button says would be worse than the empty chart it replaces. The
  // stored choice is not overwritten — a daily-only symbol must not reset the
  // timeframe of every other chart.
  //
  // Both fallbacks act only on an answer for the pair on screen: for one render
  // after a switch the store still holds the previous pair's coverage.
  const answered = candles.loadedFor === `${symbol}|${interval}`
  useEffect(() => {
    if (!candles.coverage || !answered) return
    const support = isSupported(interval, candles.coverage)
    if (support.ok) return
    setNotice(
      `${intervalLabel(interval)} is unavailable — showing ${intervalLabel(
        FALLBACK_INTERVAL
      )} instead. ${support.reason}`
    )
    setIntervalId(FALLBACK_INTERVAL)
  }, [candles.coverage, interval, answered])

  // A rejected timeframe 400s, and a 400 carries no coverage — so the guard
  // above can never fire for it. Read the refusal itself instead.
  useEffect(() => {
    if (!candles.error || !answered || interval === FALLBACK_INTERVAL) return
    if (!UNSUPPORTED_RE.test(candles.error)) return
    setNotice(
      `${intervalLabel(interval)} is unavailable — showing ${intervalLabel(
        FALLBACK_INTERVAL
      )} instead. ${candles.error}`
    )
    setIntervalId(FALLBACK_INTERVAL)
  }, [candles.error, interval, answered])

  const toggleLogScale = useCallback(() => {
    setLogScale((on) => {
      writeLogScale(!on)
      return !on
    })
  }, [])

  const predictions = useMemo(
    () => unwrapList<Prediction>(latest.data, 'items', 'predictions'),
    [latest.data]
  )

  // ---- indicators -------------------------------------------------------
  // Until the first response names its price fields, an index is known to be
  // close-only from its symbol alone.
  const priceFields = useMemo(
    () => candles.priceFields ?? (symbolKind(symbol) === 'tse_index' ? ['close'] : null),
    [candles.priceFields, symbol]
  )
  const indicatorRefusal = useCallback(
    (kind: IndicatorKind): string | null => {
      const support = indicatorSupport(kind, priceFields)
      return support.ok ? null : support.reason
    },
    [priceFields]
  )
  const overlayRefusal = useCallback(
    (key: keyof OverlayToggles): string | null => {
      const support = overlaySupport(symbol, key)
      return support.ok ? null : support.reason
    },
    [symbol]
  )
  const overlayRefusals = useMemo(() => {
    const out: Partial<Record<keyof OverlayToggles, string>> = {}
    for (const key of ['forecast', 'events', 'trend'] as const) {
      const reason = overlayRefusal(key)
      if (reason !== null) out[key] = reason
    }
    return out
  }, [overlayRefusal])

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

  const cold = useMemo(
    () => coldInstances(indicators.instances, plots, (i) => indicatorRefusal(i.kind) !== null),
    [indicators.instances, plots, indicatorRefusal]
  )

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

  const forecastOn = indicators.overlays.forecast && forecastSupport.ok
  const forecast = useMemo(
    () => (forecastOn ? forecastPoints(predictions, lastCandle) : []),
    [forecastOn, predictions, lastCandle]
  )
  const forecastSeries = useMemo(() => forecastPlots(forecast), [forecast])

  const eventsOn = indicators.overlays.events && eventsSupport.ok
  const news = useApi<NewsFeedResponse>(eventsOn ? NEWS_PATH : null)
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
  useEventMarkers(chart?.mainSeries ?? null, eventPlacement.events, eventsOn)

  const baseHeight = fullscreen ? Math.max(window.innerHeight - 260, 320) : 440
  const chartHeight = baseHeight + PANE_HEIGHT * paneCount

  const prices = current.data?.prices as Partial<Record<string, CurrentPrice>> | undefined
  const quote = tehran ? undefined : prices?.[symbol]
  const gold = current.data?.prices?.IR_GOLD_18K
  // The row sits under the 18k gold card, so it is 18k gold's direction or
  // nothing — never the charted symbol's.
  const st = symbol === 'IR_GOLD_18K' ? candles.overlays?.supertrend_dir : null
  const stDir = st && st.length > 0 ? st[st.length - 1] : 0
  const firstLoad = candles.loading && candles.candles.length === 0
  const refusedEmpty = !candles.loading && candles.error !== null && candles.candles.length === 0
  const onGold = symbol === 'IR_GOLD_18K'

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
              groups={catalog.groups}
              symbolLabel={symbolLabel}
              interval={interval}
              onIntervalChange={chooseInterval}
              coverage={candles.coverage ?? (tehran ? TSE_COVERAGE : null)}
              fullscreen={fullscreen}
              onToggleFullscreen={toggleFullscreen}
              logScale={logScale}
              onToggleLogScale={toggleLogScale}
              indicatorsSlot={
                <IndicatorMenu
                  state={indicators}
                  onChange={setIndicators}
                  indicatorRefusal={indicatorRefusal}
                  overlayRefusal={overlayRefusal}
                />
              }
              drawSlot={<DrawingToolbar engine={drawings} />}
            />

            {notice && (
              <p className="tchart-notice muted small" role="status">
                {notice}
              </p>
            )}

            <OhlcHeader
              symbol={symbol}
              label={symbolLabel}
              interval={interval}
              hovered={hovered}
              candles={candles.candles}
              unit={unit}
              priceFields={candles.priceFields}
            />

            {firstLoad ? (
              <Loading label="Loading candles…" />
            ) : candles.error && candles.candles.length === 0 ? (
              <ErrorMessage message={candles.error} onRetry={candles.reload} />
            ) : candles.candles.length === 0 ? (
              <EmptyState
                title="No candle data"
                hint={
                  tehran
                    ? `No settled session of ${symbolLabel} is stored. Tehran market data arrives when an operator runs the off-server fetch.`
                    : `Nothing is stored for ${symbolLabel} yet. Its chart appears once price history has been collected for it — nothing is drawn in the meantime.`
                }
              />
            ) : (
              <>
                <TradingChart
                  // A series cannot change type in place: a close-only index
                  // is a line, everything else is candles.
                  key={seriesMode}
                  seriesMode={seriesMode}
                  logScale={logScale}
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
              refusal={(instance) => {
                const support = indicatorSupport(instance.kind, priceFields)
                return support.ok ? null : { short: support.short, reason: support.reason }
              }}
              overlayRefusals={overlayRefusals}
            />

            <ChartStatusBar
              // A tick series is as fresh as its newest stored price; the
              // candle response's as_of is only when it was built. A Tehran
              // series is aged by data_age instead, below.
              asOf={tehran ? candles.asOf : (quote?.observed_at ?? null)}
              interval={interval}
              candles={candles.candles}
              coverage={candles.coverage}
              source={tehran ? (candles.source ?? 'TSETMC') : (quote?.source ?? null)}
              stale={tehran ? undefined : quote?.stale}
              // A refused series has no age to wait for: "not known yet"
              // would promise one. It is simply a chart with no data.
              dataAge={tehran && !refusedEmpty ? candles.dataAge : undefined}
            />
          </div>
        </div>

        <aside className="trade-side">
          {tehran && (
            <TehranInstrumentCard
              symbol={symbol}
              label={symbolLabel}
              instrument={candles.instrument}
              loading={candles.loading}
              refused={candles.error !== null && candles.candles.length === 0 ? candles.error : null}
              notes={candles.notes}
              last={lastCandle}
              ageCard={<BourseAgeNotice age={candles.dataAge ?? undefined} calendar={calendar} />}
            />
          )}

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

          {!tehran && (
            <div className="card">
              <div className="card-title">{onGold ? 'Signal' : 'Signal · 18k gold'}</div>
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
          )}

          <div className="card">
            <div className="card-title">
              {onGold || !forecastSupport.ok ? 'Forecasts' : `Forecasts · ${symbolLabel}`}
            </div>
            {!forecastSupport.ok ? (
              <span className="muted small">{forecastSupport.reason}</span>
            ) : predictions.length > 0 ? (
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
            <div className="card-title">{onGold ? 'Provider quotes' : 'Provider quotes · 18k gold'}</div>
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
                        <td className="num mono">{formatChartPrice(value, symbol, unit)}</td>
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

/**
 * What the charted Tehran series IS: its registry label, its verdict, the
 * API's own notes and how old the stored data is. Every sentence here comes
 * from the response; this card derives nothing.
 */
function TehranInstrumentCard({
  symbol,
  label,
  instrument,
  loading,
  refused,
  notes,
  last,
  ageCard
}: {
  symbol: ChartSymbol
  label: string
  instrument: ChartIndexInstrument | ChartEquityInstrument | null
  /** The candle request is in flight. */
  loading: boolean
  /** The API's refusal, when the request answered with one and no series. */
  refused: string | null
  notes: string[]
  last: ChartCandle | null
  ageCard: ReactNode
}) {
  const equity = symbolKind(symbol) === 'tse_equity'
  return (
    <div className="card" data-testid="trade-instrument">
      <div className="card-title">{label}</div>
      {instrument && isEquityInstrument(instrument) ? (
        <>
          <div className="bidi-fa" lang="fa" dir="rtl">
            {instrument.name_fa}
          </div>
          <div className="kv">
            <span className="muted">Sector</span>
            <span className="bidi-fa" lang="fa" dir="rtl">
              {instrument.sector_fa || '—'}
            </span>
          </div>
          <div className="kv">
            <span className="muted">Adjustment</span>
            <span className="mono small">
              {instrument.adjustment.status} · {instrument.adjustment.actions_applied} action(s)
            </span>
          </div>
        </>
      ) : instrument ? (
        <>
          <div className="bidi-fa" lang="fa" dir="rtl">
            {instrument.name_fa}
          </div>
          <div className="kv">
            <span className="muted">Basis</span>
            <span className="small">
              {WEIGHTING[instrument.weighting] ?? 'Weighting not stated'},{' '}
              {BASIS[instrument.return_basis] ?? 'return basis not stated'}
            </span>
          </div>
          <div className="kv">
            <span className="muted">Check</span>
            <span className="mono small">
              {instrument.check_status} · {instrument.rows_rescaled} value(s) corrected
            </span>
          </div>
          {last && (
            <div className="kv">
              <span className="muted">Last close</span>
              <span className="num mono">{formatIndexLevel(last.close)}</span>
            </div>
          )}
        </>
      ) : loading ? (
        <span className="muted small">Loading…</span>
      ) : (
        // Answered, and not with a series: a refused index or share says why
        // beside the chart, so the card must not claim an answer is coming.
        <span className="muted small">
          No series is served for this symbol{refused ? ` — ${refused}` : '.'}
        </span>
      )}
      {ageCard}
      {notes.length > 0 && (
        <ul className="muted small tchart-notes">
          {notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
      {equity && instrument && isEquityInstrument(instrument) ? (
        <Link className="small" to={`/stocks/${encodeURIComponent(instrument.symbol)}`}>
          {instrument.symbol} against its market →
        </Link>
      ) : (
        <Link className="small" to="/bourse">
          The Tehran market page →
        </Link>
      )}
    </div>
  )
}
