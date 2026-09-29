import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type {
  CandleCoverage,
  ChartCandle,
  ChartCandlesResponse,
  CurrentPricesResponse,
  StockDataAge
} from '../api/types'

/**
 * lightweight-charts drives a real canvas, which jsdom has none of. Mocking the
 * module lets the tests assert the thing that actually broke the old chart:
 * how many times it gets *built*.
 */
const lw = vi.hoisted(() => ({
  createChartCalls: 0,
  removeCalls: 0,
  setDataCalls: [] as unknown[][],
  updateCalls: [] as unknown[],
  rangeHandlers: [] as Array<(r: { from: number; to: number } | null) => void>,
  crosshairHandlers: [] as Array<(p: { time?: number; point?: { x: number; y: number } }) => void>,
  visibleRangeCalls: [] as Array<{ from: number; to: number }>,
  chartOptions: [] as Array<Record<string, any>>,
  /** Every series definition passed to addSeries, in order: [0] is the price series. */
  seriesDefs: [] as unknown[],
  reset() {
    lw.createChartCalls = 0
    lw.removeCalls = 0
    lw.setDataCalls = []
    lw.updateCalls = []
    lw.rangeHandlers = []
    lw.crosshairHandlers = []
    lw.visibleRangeCalls = []
    lw.chartOptions = []
    lw.seriesDefs = []
  }
}))

vi.mock('lightweight-charts', () => {
  const timeScale = {
    fitContent: vi.fn(),
    scrollToRealTime: vi.fn(),
    subscribeVisibleLogicalRangeChange: (h: (r: { from: number; to: number } | null) => void) => {
      lw.rangeHandlers.push(h)
    },
    unsubscribeVisibleLogicalRangeChange: (h: unknown) => {
      lw.rangeHandlers = lw.rangeHandlers.filter((x) => x !== h)
    },
    getVisibleLogicalRange: () => ({ from: 0, to: 50 }),
    setVisibleLogicalRange: (r: { from: number; to: number }) => {
      lw.visibleRangeCalls.push(r)
    },
    timeToCoordinate: () => 42,
    coordinateToTime: () => 1_700_000_000,
    applyOptions: vi.fn()
  }
  const series = {
    setData: (d: unknown[]) => lw.setDataCalls.push(d),
    update: (d: unknown) => lw.updateCalls.push(d),
    applyOptions: vi.fn(),
    priceToCoordinate: () => 7,
    coordinateToPrice: () => 8_120_000,
    createPriceLine: (o: unknown) => o,
    removePriceLine: vi.fn()
  }
  const chart = {
    addSeries: (def: unknown) => {
      lw.seriesDefs.push(def)
      return series
    },
    removeSeries: vi.fn(),
    applyOptions: (o: Record<string, any>) => lw.chartOptions.push(o),
    remove: () => {
      lw.removeCalls++
    },
    timeScale: () => timeScale,
    subscribeCrosshairMove: (h: (p: { time?: number }) => void) => {
      lw.crosshairHandlers.push(h)
    },
    unsubscribeCrosshairMove: (h: unknown) => {
      lw.crosshairHandlers = lw.crosshairHandlers.filter((x) => x !== h)
    },
    subscribeDblClick: vi.fn(),
    unsubscribeDblClick: vi.fn()
  }
  return {
    createChart: () => {
      lw.createChartCalls++
      return chart
    },
    CandlestickSeries: 'Candlestick',
    LineSeries: 'Line',
    AreaSeries: 'Area',
    ColorType: { Solid: 'solid', VerticalGradient: 'gradient' },
    CrosshairMode: { Normal: 0, Magnet: 1, Hidden: 2, MagnetOHLC: 3 },
    LineStyle: { Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3, SparseDotted: 4 }
  }
})

// TradePanel reaches the network through the shared client; mock the transport
// so the page test stays hermetic (same pattern as OsintStream/AdvisorCard).
vi.mock('../api/client', () => ({
  api: vi.fn(() => new Promise(() => undefined)),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : 'Unexpected error')
}))
import { api } from '../api/client'

import { TradingChart, type ChartHandle } from '../chart/TradingChart'
import TradePanel from '../pages/TradePanel'
import { ChartToolbar } from '../chart/ChartToolbar'
import { ChartStatusBar } from '../chart/ChartStatusBar'
import { OhlcHeader } from '../chart/OhlcHeader'
import { useCandles } from '../chart/useCandles'
import { SettingsProvider } from '../lib/settings'
import { INTERVALS } from '../chart/intervals'
import { TSE_COVERAGE, TSE_INDEX } from '../chart/symbols'

const DAY = 86_400
const BASE_T = Date.parse('2026-08-10T00:00:00Z') / 1000

/** A genuine multi-tick bucket. */
function bar(index: number, close: number, ticks = 12): ChartCandle {
  return {
    t: BASE_T + index * DAY,
    open_time: new Date((BASE_T + index * DAY) * 1000).toISOString(),
    open: close - 20_000,
    high: close + 30_000,
    low: close - 40_000,
    close,
    volume: null,
    ticks,
    confirmed: true,
    synthetic: false
  }
}

/** A bucket with one observation: no traded range at all. */
function singleBar(index: number, price: number): ChartCandle {
  return {
    t: BASE_T + index * DAY,
    open_time: new Date((BASE_T + index * DAY) * 1000).toISOString(),
    open: price,
    high: price,
    low: price,
    close: price,
    volume: null,
    ticks: 1,
    confirmed: true,
    synthetic: true
  }
}

const CANDLES = [bar(0, 8_000_000), bar(1, 8_060_000), bar(2, 8_120_000)]

function chartProps(overrides: Partial<Parameters<typeof TradingChart>[0]> = {}) {
  return {
    candles: CANDLES,
    overlays: null,
    interval: '1d' as const,
    unit: 'IRT' as const,
    symbol: 'IR_GOLD_18K',
    height: 400,
    onCrosshair: vi.fn(),
    ...overrides
  }
}

function coverage(overrides: Partial<CandleCoverage> = {}): CandleCoverage {
  return {
    base_granularity_seconds: 300,
    intraday_from: '2026-07-20T00:00:00Z',
    history_from: '2022-04-20T00:00:00Z',
    supported_intervals: INTERVALS.map((d) => d.id),
    note: '',
    ...overrides
  }
}

const apiMock = api as unknown as Mock

/** Only the candles endpoint answers; the side cards stay pending on purpose. */
function serveCandles(body: Partial<ChartCandlesResponse>) {
  apiMock.mockImplementation((path: string) => {
    if (path.startsWith('/market/candles')) {
      return Promise.resolve({
        symbol: 'IR_GOLD_18K',
        interval: '1d',
        interval_seconds: DAY,
        timezone: 'UTC',
        candles: [],
        has_more: false,
        next_before: null,
        support: null,
        resistance: null,
        as_of: new Date().toISOString(),
        ...body
      })
    }
    return new Promise(() => undefined)
  })
}

beforeEach(() => {
  lw.reset()
  apiMock.mockReset()
  apiMock.mockImplementation(() => new Promise(() => undefined))
  document.documentElement.dataset.theme = 'dark'
})

describe('TradingChart lifecycle', () => {
  it('creates the chart exactly once across a data update and a theme flip', async () => {
    const props = chartProps()
    const { rerender } = render(<TradingChart {...props} />)
    expect(lw.createChartCalls).toBe(1)

    // A poll that rewrites the forming bar and appends the next one.
    const updated = [CANDLES[0], CANDLES[1], bar(2, 8_140_000), bar(3, 8_150_000)]
    rerender(<TradingChart {...props} candles={updated} />)
    expect(lw.createChartCalls).toBe(1)

    document.documentElement.dataset.theme = 'light'
    await waitFor(() => expect(lw.setDataCalls.length).toBeGreaterThan(1))

    expect(lw.createChartCalls).toBe(1)
    expect(lw.removeCalls).toBe(0)
  })

  it('labels an intraday day-boundary with a DATE, not a clock time', () => {
    // Found in the real browser: every tick on a 15m chart read "03:30",
    // because the formatter ignored the tick TYPE and Tehran renders UTC
    // midnight as 03:30. Five day separators were indistinguishable and the
    // axis carried no date at all.
    render(<TradingChart {...chartProps()} interval="15m" />)
    const opts = lw.chartOptions.filter((o) => o?.timeScale?.tickMarkFormatter).pop()
    const fmt = opts!.timeScale.tickMarkFormatter as (t: number, k: number) => string
    const midnightUtc = Date.UTC(2026, 7, 12) / 1000
    // TickMarkType: 2 = DayOfMonth (a calendar boundary), 3 = Time.
    expect(fmt(midnightUtc, 2)).not.toMatch(/^\d{1,2}:\d{2}$/)
    expect(fmt(midnightUtc, 3)).toMatch(/^\d{1,2}:\d{2}$/)
  })

  it('labels the crosshair on a daily chart with the DATE alone', () => {
    // A daily bar is a trade date (a Tehran session) or a whole UTC day; a
    // clock time on it would be Tehran's rendering of UTC midnight, "03:30",
    // which no one traded at — the header already drops it, and so must the
    // crosshair label under the axis.
    const lastTimeFormatter = () =>
      lw.chartOptions.filter((o) => o?.localization?.timeFormatter).pop()!.localization
        .timeFormatter as (t: number) => string
    const midnightUtc = Date.UTC(2026, 8, 28) / 1000
    const { rerender } = render(<TradingChart {...chartProps({ candles: INDEX_BARS, symbol: TEDPIX })} seriesMode="line" />)
    expect(lastTimeFormatter()(midnightUtc)).not.toMatch(/\d{1,2}:\d{2}/)
    // An intraday bucket keeps its clock time.
    rerender(<TradingChart {...chartProps()} interval="15m" />)
    expect(lastTimeFormatter()(midnightUtc)).toMatch(/03:30/)
  })

  it('patches the tail with update() instead of replacing the series', () => {
    const props = chartProps()
    const { rerender } = render(<TradingChart {...props} />)
    const setDataBefore = lw.setDataCalls.length

    rerender(
      <TradingChart {...props} candles={[CANDLES[0], CANDLES[1], bar(2, 8_200_000)]} />
    )

    expect(lw.updateCalls).toHaveLength(1)
    expect(lw.setDataCalls.length).toBe(setDataBefore)
  })

  it('rebuilds the data and keeps the viewport when older history is prepended', () => {
    const props = chartProps()
    const { rerender } = render(<TradingChart {...props} />)
    const setDataBefore = lw.setDataCalls.length

    rerender(<TradingChart {...props} candles={[bar(-2, 7_900_000), bar(-1, 7_950_000), ...CANDLES]} />)

    expect(lw.setDataCalls.length).toBe(setDataBefore + 1)
    // Two bars arrived before the old first bar, so every logical index moved by
    // two — the visible range has to move with them or the view jumps back in
    // time the moment older history lands.
    expect(lw.visibleRangeCalls).toEqual([{ from: 2, to: 52 }])
  })

  it('draws single-observation buckets hollow and muted, never as a traded range', () => {
    render(<TradingChart {...chartProps({ candles: [singleBar(0, 8_000_000), bar(1, 8_060_000)] })} />)

    const pushed = lw.setDataCalls[0] as Array<Record<string, unknown>>
    expect(pushed[0].color).toBe('transparent')
    expect(pushed[0].wickColor).toBe('transparent')
    expect(pushed[0].borderColor).toBeTruthy()
    // The genuine bucket keeps the series' own up/down colors.
    expect(pushed[1].color).toBeUndefined()
    expect(pushed[1].borderColor).toBeUndefined()
  })

  it('loads older history when the user pans within 20 bars of the left edge', () => {
    const onLoadOlder = vi.fn()
    render(<TradingChart {...chartProps({ onLoadOlder })} />)
    expect(lw.rangeHandlers.length).toBeGreaterThan(0)

    lw.rangeHandlers[0]({ from: 120, to: 200 })
    expect(onLoadOlder).not.toHaveBeenCalled()

    lw.rangeHandlers[0]({ from: 5, to: 60 })
    expect(onLoadOlder).toHaveBeenCalledTimes(1)
  })

  it('reports the crosshair candle and clears it when the pointer leaves', () => {
    const onCrosshair = vi.fn()
    render(<TradingChart {...chartProps({ onCrosshair })} />)

    lw.crosshairHandlers[0]({ time: CANDLES[1].t, point: { x: 10, y: 10 } })
    expect(onCrosshair).toHaveBeenLastCalledWith(CANDLES[1])

    lw.crosshairHandlers[0]({})
    expect(onCrosshair).toHaveBeenLastCalledWith(null)
  })

  it('hands the parallel layers a stable handle and stacks children over the chart', () => {
    const onReady = vi.fn()
    const { container } = render(
      <TradingChart {...chartProps({ onReady })}>
        <canvas data-testid="overlay" />
      </TradingChart>
    )

    expect(onReady).toHaveBeenCalledTimes(1)
    const handle = onReady.mock.calls[0][0] as ChartHandle
    expect(handle.chart).toBeTruthy()
    expect(handle.mainSeries).toBeTruthy()
    expect(handle.container).toBe(container.querySelector('.tchart'))
    expect(handle.timeToX(CANDLES[0].t)).toBe(42)
    expect(handle.priceToY(8_000_000)).toBe(7)
    expect(handle.xToTime(10)).toBe(1_700_000_000)
    expect(handle.yToPrice(10)).toBe(8_120_000)

    // The overlay canvas must live inside the positioned container.
    expect(handle.container.querySelector('[data-testid="overlay"]')).not.toBeNull()

    const seen = vi.fn()
    const off = handle.onViewportChange(seen)
    lw.rangeHandlers[0]({ from: 40, to: 60 })
    expect(seen).toHaveBeenCalled()
    off()
    lw.rangeHandlers[0]({ from: 41, to: 61 })
    expect(seen).toHaveBeenCalledTimes(1)
  })
})

describe('OhlcHeader', () => {
  it('shows the crosshair candle, then falls back to the latest one', () => {
    const props = {
      symbol: 'IR_GOLD_18K',
      interval: '1d' as const,
      candles: CANDLES,
      unit: 'IRT' as const
    }
    const { rerender } = render(<OhlcHeader {...props} hovered={CANDLES[0]} />)
    expect(screen.getByText('8,000,000')).toBeInTheDocument()
    expect(screen.queryByText('latest')).not.toBeInTheDocument()

    rerender(<OhlcHeader {...props} hovered={null} />)
    expect(screen.getByText('8,120,000')).toBeInTheDocument()
    expect(screen.getByText('latest')).toBeInTheDocument()
  })

  it('honours the IRT/IRR display toggle', () => {
    const props = {
      symbol: 'IR_GOLD_18K',
      interval: '1d' as const,
      candles: CANDLES,
      hovered: null
    }
    const { rerender } = render(<OhlcHeader {...props} unit="IRT" />)
    expect(screen.getByText('8,120,000')).toBeInTheDocument()

    rerender(<OhlcHeader {...props} unit="IRR" />)
    expect(screen.getByText('81,200,000')).toBeInTheDocument()
  })

  it('formats XAUUSD in dollars regardless of the toman toggle', () => {
    render(
      <OhlcHeader
        symbol="XAUUSD"
        interval="1d"
        candles={[bar(0, 2_410.5)]}
        hovered={null}
        unit="IRR"
      />
    )
    expect(screen.getByText('$2,410.50')).toBeInTheDocument()
  })

  it('marks a single-observation bucket in the readout', () => {
    render(
      <OhlcHeader
        symbol="IR_GOLD_18K"
        interval="1d"
        candles={[singleBar(0, 8_000_000)]}
        hovered={null}
        unit="IRT"
      />
    )
    expect(screen.getByText('1 obs')).toBeInTheDocument()
  })

  // 2026-08-12T09:00:00Z is 12:30 Tehran, i.e. 1405/05/21 in the Jalali calendar.
  const stamped: ChartCandle = {
    ...bar(0, 8_120_000),
    open_time: '2026-08-12T09:00:00Z'
  }

  it('renders the bucket time in the Jalali calendar by default', () => {
    render(
      <SettingsProvider>
        <OhlcHeader symbol="IR_GOLD_18K" interval="1h" candles={[stamped]} hovered={null} unit="IRT" />
      </SettingsProvider>
    )
    expect(screen.getByText('1405/05/21 12:30')).toBeInTheDocument()
  })

  it('renders the bucket time in the Gregorian calendar when the user picked it', () => {
    window.localStorage.setItem('igp_calendar', 'gregorian')
    render(
      <SettingsProvider>
        <OhlcHeader symbol="IR_GOLD_18K" interval="1h" candles={[stamped]} hovered={null} unit="IRT" />
      </SettingsProvider>
    )
    expect(screen.getByText('2026-08-12 12:30')).toBeInTheDocument()
  })

  it('dates a bucket of a day or more without a clock time', () => {
    // A daily bucket starts at UTC midnight, 03:30 in Tehran: a time nobody
    // traded at, which the header used to print beside every daily close.
    render(
      <SettingsProvider>
        <OhlcHeader symbol="IR_GOLD_18K" interval="1d" candles={[stamped]} hovered={null} unit="IRT" />
      </SettingsProvider>
    )
    expect(screen.getByText('1405/05/21')).toBeInTheDocument()
    expect(screen.queryByText(/12:30/)).toBeNull()
  })

  it('shows a daily-close series as closes, with no "1 obs" badge', () => {
    // Silver 999: one settled close a day. O=H=L=C and a single-observation
    // badge on every bar read as a broken feed; the API says close-only.
    const close = { ...stamped, open: 506570, high: 506570, low: 506570, close: 506570, ticks: 1, synthetic: true }
    render(
      <SettingsProvider>
        <OhlcHeader
          symbol="IR_SILVER_999"
          interval="1d"
          candles={[close]}
          hovered={null}
          unit="IRT"
          priceFields={['close']}
        />
      </SettingsProvider>
    )
    expect(screen.queryByText('O')).toBeNull()
    expect(screen.queryByText('H')).toBeNull()
    expect(screen.getByText('C')).toBeInTheDocument()
    expect(screen.queryByText('1 obs')).toBeNull()
  })
})

describe('ChartToolbar', () => {
  const base = {
    symbol: 'IR_GOLD_18K' as const,
    onSymbolChange: vi.fn(),
    interval: '1d' as const,
    onIntervalChange: vi.fn(),
    fullscreen: false,
    onToggleFullscreen: vi.fn()
  }

  it('offers every preset when the data supports it', () => {
    render(<ChartToolbar {...base} coverage={coverage()} />)
    expect(screen.getByLabelText('15m candles')).toBeEnabled()
    expect(screen.getByLabelText('1D candles')).toHaveAttribute('aria-pressed', 'true')
  })

  it('disables an unsupported timeframe and explains why rather than hiding it', () => {
    render(
      <ChartToolbar
        {...base}
        coverage={coverage({
          base_granularity_seconds: 86_400,
          intraday_from: null,
          supported_intervals: ['1d', '2d', '3d', '1w']
        })}
      />
    )
    const fifteen = screen.getByLabelText('15m candles')
    expect(fifteen).toBeDisabled()
    expect(fifteen).toHaveAttribute('title', expect.stringContaining('daily source data'))
    expect(screen.getByLabelText('1D candles')).toBeEnabled()
  })

  it('reserves slots for the indicator and drawing layers', () => {
    render(
      <ChartToolbar
        {...base}
        coverage={coverage()}
        indicatorsSlot={<button type="button">Indicators</button>}
        drawSlot={<button type="button">Draw</button>}
      />
    )
    expect(screen.getByText('Indicators')).toBeInTheDocument()
    expect(screen.getByText('Draw')).toBeInTheDocument()
  })
})

describe('TradePanel wiring', () => {
  const MIXED = [singleBar(0, 8_000_000), singleBar(1, 8_010_000), bar(2, 8_120_000)]

  it('renders the chart once and keeps every side card', async () => {
    serveCandles({ candles: MIXED, coverage: coverage() })
    render(<TradePanel />)

    await waitFor(() => expect(lw.createChartCalls).toBe(1))

    // The desk cards are the reason this page exists; none of them may vanish.
    const cardTitles = Array.from(document.querySelectorAll('.card-title')).map(
      (el) => el.textContent
    )
    expect(cardTitles).toEqual(
      expect.arrayContaining(['IR_GOLD_18K', 'Signal', 'Forecasts', 'Provider quotes'])
    )
    expect(screen.getByLabelText('Symbol')).toBeInTheDocument()
    expect(screen.getByText('2 of 3 bars are single-observation')).toBeInTheDocument()
  })

  it('falls back to 1D and says why when the stored timeframe stops being servable', async () => {
    window.localStorage.setItem('igp_chart_interval', '15m')
    serveCandles({
      candles: MIXED,
      coverage: coverage({
        base_granularity_seconds: 86_400,
        intraday_from: null,
        supported_intervals: ['1d', '2d', '3d', '1w']
      })
    })
    render(<TradePanel />)

    await waitFor(() =>
      expect(screen.getByText(/15m is unavailable — showing 1D instead/)).toBeInTheDocument()
    )
    expect(screen.getByLabelText('1D candles')).toHaveAttribute('aria-pressed', 'true')
  })

  it('never blanks the chart while a refresh is in flight', async () => {
    serveCandles({ candles: MIXED, coverage: coverage() })
    render(<TradePanel />)
    await waitFor(() => expect(lw.createChartCalls).toBe(1))

    expect(screen.queryByText('Loading candles…')).not.toBeInTheDocument()
    expect(screen.queryByText('No candle data')).not.toBeInTheDocument()
  })

  it('shows the empty state rather than a black rectangle when there are no candles', async () => {
    serveCandles({ candles: [], coverage: coverage() })
    render(<TradePanel />)

    await waitFor(() => expect(screen.getByText('No candle data')).toBeInTheDocument())
    expect(lw.createChartCalls).toBe(0)
  })
})

describe('ChartStatusBar', () => {
  it('marks a daily close STALE only on the server\'s daily-close verdict', () => {
    // More than four days old: a close is missing. The date stays on screen.
    render(
      <SettingsProvider>
        <ChartStatusBar
          asOf="2026-09-22T23:00:00Z"
          interval="1d"
          candles={[singleBar(0, 505_000)]}
          coverage={null}
          stale
          dailyClose
        />
      </SettingsProvider>
    )
    expect(screen.getByText('daily close of 1405/06/31')).toBeInTheDocument()
    expect(screen.getByText('STALE')).toBeInTheDocument()
    expect(screen.getByText('1 daily close')).toBeInTheDocument()
  })

  it('counts the single-observation buckets out loud', () => {
    render(
      <ChartStatusBar
        asOf={new Date().toISOString()}
        interval="1d"
        candles={[singleBar(0, 8_000_000), singleBar(1, 8_010_000), bar(2, 8_120_000)]}
        coverage={coverage()}
      />
    )
    expect(screen.getByText('2 of 3 bars are single-observation')).toBeInTheDocument()
    expect(screen.getByText('3 candles')).toBeInTheDocument()
  })

  it('says nothing about a source it cannot name', () => {
    const { container } = render(
      <ChartStatusBar asOf={new Date().toISOString()} interval="1d" candles={CANDLES} coverage={null} />
    )
    expect(container.querySelector('.mono')).toBeNull()
  })

  it('names the source when the caller knows it', () => {
    render(
      <ChartStatusBar
        asOf={new Date().toISOString()}
        interval="1d"
        candles={CANDLES}
        coverage={null}
        source="hamrah_gold"
      />
    )
    expect(screen.getByText('hamrah_gold')).toBeInTheDocument()
  })

  it('flags stale data with the repo freshness vocabulary', () => {
    render(
      <ChartStatusBar
        asOf={new Date(Date.now() - 6 * 3_600_000).toISOString()}
        interval="5m"
        candles={CANDLES}
        coverage={null}
      />
    )
    expect(screen.getByText('STALE')).toBeInTheDocument()
  })

  it('lets the server’s market-hours verdict decide over the clock heuristic', () => {
    // A TSE-session fund on a Saturday morning: its newest price is from
    // Wednesday's session, which the server calls current for a shut market.
    const wednesday = new Date(Date.now() - 2.6 * DAY * 1000).toISOString()
    const { rerender } = render(
      <ChartStatusBar asOf={wednesday} interval="1h" candles={CANDLES} coverage={null} stale={false} />
    )
    expect(screen.queryByText('STALE')).not.toBeInTheDocument()
    expect(screen.getByText('2d ago')).toBeInTheDocument()
    rerender(<ChartStatusBar asOf={wednesday} interval="1h" candles={CANDLES} coverage={null} stale />)
    expect(screen.getByText('STALE')).toBeInTheDocument()
  })

  it('stays quiet when the data is fresh for the timeframe', () => {
    render(
      <ChartStatusBar
        asOf={new Date(Date.now() - 4 * 60_000).toISOString()}
        interval="1d"
        candles={CANDLES}
        coverage={null}
      />
    )
    expect(screen.queryByText('STALE')).not.toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// The Tehran market on the chart
// ---------------------------------------------------------------------------

const TEDPIX = TSE_INDEX.TEDPIX
const FOOLAD = 'EQ:46348559193224090'

/** One settled index session: close-only, as /market/candles serves it. */
function indexBar(index: number, close: number, extra: Partial<ChartCandle> = {}): ChartCandle {
  return {
    t: BASE_T + index * DAY,
    open_time: new Date((BASE_T + index * DAY) * 1000).toISOString(),
    open: null,
    high: null,
    low: null,
    close,
    volume: null,
    confirmed: true,
    ...extra
  }
}

/** One adjusted share session; a halt has no range and traded:false. */
function shareBar(index: number, close: number, traded = true): ChartCandle {
  return {
    t: BASE_T + index * DAY,
    open_time: new Date((BASE_T + index * DAY) * 1000).toISOString(),
    open: traded ? close - 10 : null,
    high: traded ? close + 20 : null,
    low: traded ? close - 20 : null,
    close,
    volume: traded ? 1000 : 0,
    confirmed: true,
    traded,
    ...(traded ? { last_trade: close + 1 } : {})
  }
}

function dataAge(overrides: Partial<StockDataAge> = {}): StockDataAge {
  return {
    newest_trade_date: '2026-09-28',
    age_days: 3,
    as_of: '2026-10-01',
    stale: false,
    stale_after_days: 10,
    refresh_command: 'make refresh-equities',
    note: 'Tehran market data does not refresh itself.',
    ...overrides
  }
}

const INDEX_BARS = [
  indexBar(0, 3_650_000),
  indexBar(1, 3_713_955.9),
  indexBar(2, 3_713_955.9, { unchanged: true })
]

function indexResponse(): Partial<ChartCandlesResponse> {
  const nulls = [null, null, null]
  return {
    symbol: TEDPIX,
    candles: INDEX_BARS,
    coverage: { ...TSE_COVERAGE, history_from: '2008-12-06T00:00:00Z' },
    overlays: {
      sma_20: nulls,
      sma_50: nulls,
      bollinger_upper: nulls,
      bollinger_mid: nulls,
      bollinger_lower: nulls,
      supertrend: null,
      supertrend_dir: null,
      psar: null,
      ichimoku_tenkan: null,
      ichimoku_kijun: null,
      ichimoku_senkou_a: null,
      ichimoku_senkou_b: null
    },
    pivots: null,
    price_fields: ['close'],
    unit: 'index_points',
    source: 'TSETMC',
    instrument: {
      ins_code: '32097828799138957',
      name_fa: 'شاخص کل',
      name_en: 'TEDPIX, all-share (price and dividends)',
      market: 'bourse',
      kind: 'headline',
      sector_code: '',
      weighting: 'cap',
      return_basis: 'total',
      check_status: 'validated',
      rows_rescaled: 0,
      refusal_reason: ''
    },
    data_age: dataAge(),
    notes: ['Index points, not a price.'],
    revision: 'r1'
  }
}

describe('TradingChart for a Tehran series', () => {
  it('draws a close-only index as a line of closes, closed sessions muted', () => {
    render(
      <TradingChart
        {...chartProps({ candles: INDEX_BARS, symbol: TEDPIX })}
        seriesMode="line"
      />
    )
    expect(lw.seriesDefs[0]).toBe('Line')
    const pushed = lw.setDataCalls[0] as Array<Record<string, unknown>>
    expect(pushed[0]).toEqual({ time: INDEX_BARS[0].t, value: 3_650_000 })
    // No invented open/high/low anywhere on the series.
    for (const point of pushed) {
      expect(point).not.toHaveProperty('open')
      expect(point).not.toHaveProperty('high')
    }
    // The closure repeat is drawn — flat, as published — but muted.
    expect(pushed[2].value).toBe(3_713_955.9)
    expect(pushed[2].color).toBeTruthy()
    expect(pushed[1].color).toBeUndefined()
  })

  it('draws a halted share session as a gap, never as a candle', () => {
    const bars = [shareBar(0, 2_800), shareBar(1, 2_800, false), shareBar(2, 2_881)]
    render(<TradingChart {...chartProps({ candles: bars, symbol: FOOLAD })} />)
    expect(lw.seriesDefs[0]).toBe('Candlestick')
    const pushed = lw.setDataCalls[0] as Array<Record<string, unknown>>
    expect(pushed[1]).toEqual({ time: bars[1].t })
    expect(pushed[2]).toMatchObject({ open: 2_871, high: 2_901, low: 2_861, close: 2_881 })
  })

  it('puts the price axis on a log scale when asked', () => {
    const { rerender } = render(<TradingChart {...chartProps()} />)
    expect(lw.chartOptions).toContainEqual({ rightPriceScale: { mode: 0 } })
    rerender(<TradingChart {...chartProps()} logScale />)
    expect(lw.chartOptions).toContainEqual({ rightPriceScale: { mode: 1 } })
  })
})

describe('OhlcHeader for a Tehran series', () => {
  it('shows only the close of a close-only index, in points whatever the toggle', () => {
    render(
      <OhlcHeader
        symbol={TEDPIX}
        label="TEDPIX"
        interval="1d"
        candles={INDEX_BARS.slice(0, 2)}
        hovered={null}
        unit="IRR"
        priceFields={['close']}
      />
    )
    expect(screen.getByText('TEDPIX')).toBeInTheDocument()
    expect(screen.getByText('C')).toBeInTheDocument()
    for (const key of ['O', 'H', 'L']) expect(screen.queryByText(key)).not.toBeInTheDocument()
    // Index points: the ×10 rial view does not apply.
    expect(screen.getByText('3,713,956')).toBeInTheDocument()
    expect(screen.getByText('index points')).toBeInTheDocument()
    expect(screen.queryByText('1 obs')).not.toBeInTheDocument()
    // A session is a trade date: no 03:30 (Tehran's rendering of UTC midnight).
    expect(screen.queryByText(/\d{2}:\d{2}$/)).not.toBeInTheDocument()
  })

  it('marks a closed-market session', () => {
    render(
      <OhlcHeader
        symbol={TEDPIX}
        interval="1d"
        candles={INDEX_BARS}
        hovered={null}
        unit="IRT"
        priceFields={['close']}
      />
    )
    expect(screen.getByText('no session')).toBeInTheDocument()
  })

  it('prints a share in rials, unscaled, with its official close and last trade', () => {
    render(
      <OhlcHeader
        symbol={FOOLAD}
        label="فولاد"
        interval="1d"
        candles={[shareBar(0, 2_800), shareBar(1, 2_881)]}
        hovered={null}
        unit="IRR"
        priceFields={['open', 'high', 'low', 'close']}
      />
    )
    expect(screen.getByText('Final')).toBeInTheDocument()
    expect(screen.getByText('2,881')).toBeInTheDocument()
    expect(screen.getByText('2,882')).toBeInTheDocument()
    expect(screen.queryByText('28,810')).not.toBeInTheDocument()
    // A share that traded at one price had a real session, not "one observation".
    expect(screen.queryByText('1 obs')).not.toBeInTheDocument()
  })

  it('says a halted session had no trade and shows no range for it', () => {
    render(
      <OhlcHeader
        symbol={FOOLAD}
        interval="1d"
        candles={[shareBar(0, 2_800), shareBar(1, 2_800, false)]}
        hovered={null}
        unit="IRT"
        priceFields={['open', 'high', 'low', 'close']}
      />
    )
    expect(screen.getByText('halted — no trade')).toBeInTheDocument()
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(3)
  })
})

describe('ChartStatusBar for a Tehran series', () => {
  it('measures freshness by the stored session, not by the clock heuristic', () => {
    // as_of three days old would be STALE for a tick series; for a Tehran
    // series three days is a weekend, and the server's data_age says so.
    render(
      <ChartStatusBar
        asOf={new Date(Date.now() - 3 * DAY * 1000).toISOString()}
        interval="1d"
        candles={INDEX_BARS}
        coverage={TSE_COVERAGE}
        source="TSETMC"
        dataAge={dataAge()}
      />
    )
    expect(screen.queryByText('STALE')).not.toBeInTheDocument()
    expect(screen.getByText(/last session .* 3 day\(s\) old/)).toBeInTheDocument()
    expect(screen.getByText('3 sessions')).toBeInTheDocument()
    expect(screen.getByText('1 of 3 repeat the previous close')).toBeInTheDocument()
    expect(screen.getByText('TSETMC')).toBeInTheDocument()
  })

  it('does not call a Tehran series fresh before its age has arrived', () => {
    render(
      <ChartStatusBar asOf={null} interval="1d" candles={[]} coverage={TSE_COVERAGE} dataAge={null} />
    )
    expect(screen.getByText('data age not known yet')).toBeInTheDocument()
    expect(document.querySelector('.dot-ok')).toBeNull()
  })

  it('is STALE exactly when the server says the data is', () => {
    render(
      <ChartStatusBar
        asOf={new Date().toISOString()}
        interval="1d"
        candles={[shareBar(0, 2_800), shareBar(1, 2_800, false)]}
        coverage={TSE_COVERAGE}
        dataAge={dataAge({ age_days: 15, stale: true, warning: 'These prices are 15 days old.' })}
      />
    )
    expect(screen.getByText('STALE')).toBeInTheDocument()
    expect(screen.getByText('1 of 2 had no trade')).toBeInTheDocument()
  })
})

describe('ChartToolbar picker', () => {
  const base = {
    onSymbolChange: vi.fn(),
    interval: '1d' as const,
    onIntervalChange: vi.fn(),
    coverage: null,
    fullscreen: false,
    onToggleFullscreen: vi.fn()
  }

  it('groups the static catalog and renders it before any list answers', () => {
    const { container } = render(<ChartToolbar {...base} symbol="IR_GOLD_18K" />)
    const groups = Array.from(container.querySelectorAll('optgroup')).map((g) => g.getAttribute('label'))
    expect(groups).toEqual(['Gold & coins', 'Global markets', 'Tehran · All-share'])
    expect(screen.getByRole('option', { name: /TEDPIX/ })).toBeEnabled()
  })

  it('shows a refused entry disabled with its reason, and keeps an unlisted symbol selectable', () => {
    render(
      <ChartToolbar
        {...base}
        symbol={FOOLAD}
        symbolLabel="فولاد"
        groups={[
          {
            id: 'tse:shares',
            label: 'Tehran · Shares',
            options: [
              {
                symbol: 'EQ:18027801615184692',
                label: 'کچاد — چادرملو (adjustment not validated)',
                short: 'کچاد',
                kind: 'tse_equity',
                disabled: 'Its corporate-action adjustment was not validated.',
                title: 'Its corporate-action adjustment was not validated.'
              }
            ]
          }
        ]}
      />
    )
    const refused = screen.getByRole('option', { name: /کچاد/ })
    expect(refused).toBeDisabled()
    expect(refused).toHaveAttribute('title', 'Its corporate-action adjustment was not validated.')
    expect(screen.getByLabelText('Symbol')).toHaveValue(FOOLAD)
    expect(screen.getByRole('option', { name: 'فولاد' })).toBeInTheDocument()
  })

  it('offers the log scale as a pressed toggle', () => {
    const onToggle = vi.fn()
    render(<ChartToolbar {...base} symbol={TEDPIX} logScale onToggleLogScale={onToggle} />)
    const log = screen.getByLabelText('Logarithmic price scale')
    expect(log).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(log)
    expect(onToggle).toHaveBeenCalled()
  })
})

describe('TradePanel on the Tehran market', () => {
  const GOLD_BARS = [bar(0, 8_000_000), bar(1, 8_060_000), bar(2, 8_120_000)]

  /** /market/candles answers per symbol; /prices/current answers with gold. */
  function serveMarket(bySymbol: Record<string, Partial<ChartCandlesResponse>>) {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('/market/candles')) {
        const symbol = new URLSearchParams(path.split('?')[1]).get('symbol') ?? ''
        return Promise.resolve({
          symbol,
          interval: '1d',
          interval_seconds: DAY,
          timezone: 'UTC',
          candles: [],
          has_more: false,
          next_before: null,
          support: null,
          resistance: null,
          as_of: new Date().toISOString(),
          ...(bySymbol[symbol] ?? {})
        })
      }
      if (path === '/prices/current') {
        const prices: CurrentPricesResponse = {
          as_of: new Date().toISOString(),
          prices: {
            IR_GOLD_18K: {
              value: 8_120_000,
              currency: 'IRT',
              unit: 'gram',
              source: 'hamrahgold',
              observed_at: new Date().toISOString(),
              stale: false,
              change_24h_pct: 0.5
            }
          }
        }
        return Promise.resolve(prices)
      }
      return new Promise(() => undefined)
    })
  }

  function paths(): string[] {
    return apiMock.mock.calls.map((c) => String(c[0]))
  }

  function candlePaths(): string[] {
    return paths().filter((p) => p.startsWith('/market/candles'))
  }

  function renderPanel() {
    return render(
      <MemoryRouter>
        <TradePanel />
      </MemoryRouter>
    )
  }

  it('charts TEDPIX at 1D and leaves the stored timeframe alone', async () => {
    window.localStorage.setItem('igp_chart_interval', '4h')
    serveMarket({
      IR_GOLD_18K: { candles: GOLD_BARS, coverage: coverage() },
      [TEDPIX]: indexResponse()
    })
    renderPanel()
    await waitFor(() => expect(lw.createChartCalls).toBe(1))
    expect(candlePaths()[0]).toContain('symbol=IR_GOLD_18K&interval=4h')

    fireEvent.change(screen.getByLabelText('Symbol'), { target: { value: TEDPIX } })

    await waitFor(() =>
      expect(candlePaths().some((p) => p.includes('symbol=IDX%3A32097828799138957&interval=1d'))).toBe(true)
    )
    expect(screen.getByText(/4H is unavailable — showing 1D instead/)).toBeInTheDocument()
    expect(screen.getByLabelText('1D candles')).toHaveAttribute('aria-pressed', 'true')
    // The reader's choice survives a symbol that cannot honour it.
    expect(window.localStorage.getItem('igp_chart_interval')).toBe('4h')
    await waitFor(() => expect(lw.seriesDefs).toContain('Line'))

    // …and comes back with a symbol that can.
    fireEvent.change(screen.getByLabelText('Symbol'), { target: { value: 'IR_GOLD_18K' } })
    await waitFor(() => expect(screen.getByLabelText('4H candles')).toHaveAttribute('aria-pressed', 'true'))
    expect(candlePaths()[candlePaths().length - 1]).toContain('symbol=IR_GOLD_18K&interval=4h')
  })

  it('opens a stored Tehran symbol on 1D and says why, without saving 1D', async () => {
    window.localStorage.setItem('igp_chart_symbol', TEDPIX)
    window.localStorage.setItem('igp_chart_interval', '4h')
    serveMarket({ [TEDPIX]: indexResponse() })
    renderPanel()

    await waitFor(() => expect(lw.createChartCalls).toBe(1))
    expect(candlePaths().every((p) => p.includes('interval=1d'))).toBe(true)
    expect(screen.getByText(/4H is unavailable — showing 1D instead/)).toBeInTheDocument()
    expect(window.localStorage.getItem('igp_chart_interval')).toBe('4h')
    // Every finer timeframe is disabled with the source's reason.
    expect(screen.getByLabelText('4H candles')).toBeDisabled()
  })

  it('describes the index it draws and draws nothing about gold beside it', async () => {
    window.localStorage.setItem('igp_chart_symbol', TEDPIX)
    serveMarket({ [TEDPIX]: indexResponse() })
    renderPanel()
    await waitFor(() => expect(screen.getByTestId('trade-instrument')).toBeInTheDocument())

    const card = screen.getByTestId('trade-instrument')
    expect(within(card).getByText('شاخص کل')).toBeInTheDocument()
    expect(within(card).getByText('Index points, not a price.')).toBeInTheDocument()
    expect(within(card).getByText(/Capitalisation-weighted, total return/)).toBeInTheDocument()

    // No gold advisory, no gold forecast, no gold headlines for a Tehran index.
    expect(paths().some((p) => p.startsWith('/predictions'))).toBe(false)
    expect(paths()).not.toContain('/signals/current')
    const titles = Array.from(document.querySelectorAll('.card-title')).map((el) => el.textContent)
    expect(titles).not.toContain('Signal')
    expect(titles).toContain('Provider quotes · 18k gold')
    expect(screen.getByText(/the models forecast 18k gold and XAU\/USD only/)).toBeInTheDocument()

    // The default board's SuperTrend and pivots are refused with the reason —
    // not "no data", which would suggest more history might fill them.
    for (const name of ['SuperTrend', 'Pivots']) {
      const row = screen.getByLabelText(`Remove ${name}`).closest('li') as HTMLElement
      expect(within(row).getByText(/needs a high and a low/)).toBeInTheDocument()
      expect(within(row).queryByText('no data')).not.toBeInTheDocument()
    }
    expect(screen.getByText(/SuperTrend, Pivots: Needs each session’s high and low/)).toBeInTheDocument()
    // Freshness from the data's own age.
    expect(screen.getByText(/last session/)).toBeInTheDocument()
  })

  it('asks for the charted symbol’s own forecast', async () => {
    window.localStorage.setItem('igp_chart_symbol', 'XAUUSD')
    serveMarket({ XAUUSD: { candles: [bar(0, 2_400), bar(1, 2_410)], coverage: coverage() } })
    renderPanel()
    await waitFor(() => expect(paths()).toContain('/predictions?symbol=XAUUSD'))
    // Never the bare path: that was 18k gold's forecast, drawn over XAU/USD.
    expect(paths()).not.toContain('/predictions')
    const titles = Array.from(document.querySelectorAll('.card-title')).map((el) => el.textContent)
    expect(titles).toContain('Signal · 18k gold')
  })

  it('shows the SuperTrend row under the gold card only for gold itself', async () => {
    window.localStorage.setItem('igp_chart_symbol', 'XAUUSD')
    const bullish = {
      candles: [bar(0, 2_400), bar(1, 2_410)],
      coverage: coverage(),
      overlays: { supertrend_dir: [1, 1] } as ChartCandlesResponse['overlays']
    }
    serveMarket({ XAUUSD: bullish, IR_GOLD_18K: { ...bullish, candles: GOLD_BARS.slice(0, 2) } })
    renderPanel()
    await waitFor(() => expect(lw.createChartCalls).toBe(1))
    await waitFor(() => expect(screen.getByText('8,120,000')).toBeInTheDocument())
    // XAU/USD's direction must not appear under the 18k gold card.
    expect(screen.queryByText('▲ bullish')).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Symbol'), { target: { value: 'IR_GOLD_18K' } })
    await waitFor(() => expect(screen.getByText('▲ bullish')).toBeInTheDocument())
  })

  it('does not keep saying "Loading…" beside an index the API refused', async () => {
    const refusal = 'no validated history is served for شاخص کل (هم وزن) (67130298613737946)'
    window.localStorage.setItem('igp_chart_symbol', TSE_INDEX.EQUAL_WEIGHTED)
    apiMock.mockImplementation((path: string) =>
      path.startsWith('/market/candles')
        ? Promise.reject(new Error(refusal))
        : new Promise(() => undefined)
    )
    renderPanel()
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(refusal))
    const card = screen.getByTestId('trade-instrument')
    // The request has answered — with a refusal — so the card must not claim
    // an answer is still on its way.
    expect(within(card).queryByText('Loading…')).not.toBeInTheDocument()
    expect(within(card).getByText(/No series is served for this symbol/)).toBeInTheDocument()
    // Nor does the status bar promise an age that is coming.
    expect(within(document.querySelector('.tchart-status') as HTMLElement).queryByText(/not known yet/)).toBeNull()
  })

  it('ages a registry series by its newest observation, not by when the response was built', async () => {
    // A daily settled close (TGJU) whose newest row is three days old. The
    // candle response's as_of is "now" — the moment it was built — and read
    // as freshness it said "0s ago" beside a three-day-old close.
    const observed = new Date(Date.now() - 3 * DAY * 1000 - 60_000).toISOString()
    window.localStorage.setItem('igp_chart_symbol', 'IR_SILVER_999')
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('/market/candles')) {
        return Promise.resolve({
          symbol: 'IR_SILVER_999',
          interval: '1d',
          interval_seconds: DAY,
          timezone: 'UTC',
          candles: [bar(0, 150_000), bar(1, 151_000)],
          coverage: coverage({ intraday_from: null }),
          has_more: false,
          next_before: null,
          support: null,
          resistance: null,
          as_of: new Date().toISOString()
        })
      }
      if (path === '/prices/current') {
        const prices: CurrentPricesResponse = {
          as_of: new Date().toISOString(),
          prices: {
            IR_SILVER_999: {
              value: 151_000,
              currency: 'IRT',
              unit: 'gram',
              source: 'tgju_history',
              observed_at: observed,
              stale: false,
              change_24h_pct: null
            }
          }
        }
        return Promise.resolve(prices)
      }
      return new Promise(() => undefined)
    })
    renderPanel()
    await waitFor(() => expect(lw.createChartCalls).toBe(1))
    const status = document.querySelector('.tchart-status') as HTMLElement
    await waitFor(() => expect(within(status).getByText('3d ago')).toBeInTheDocument())
    expect(within(status).queryByText(/^\d+s ago$/)).toBeNull()
    expect(within(status).getByText('tgju_history')).toBeInTheDocument()
  })

  it('dates a daily settled close instead of aging it, and draws it as closes', async () => {
    // The API marks silver 999 as one settled close a day. "13h ago · STALE"
    // and "1000 of 1000 bars are single-observation" beside it read as an
    // outage; the newest close is the newest that can exist.
    window.localStorage.setItem('igp_chart_symbol', 'IR_SILVER_999')
    const daily = (i: number, v: number) => ({ ...singleBar(i, v), open: v, high: v, low: v })
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('/market/candles')) {
        return Promise.resolve({
          symbol: 'IR_SILVER_999',
          interval: '1d',
          interval_seconds: DAY,
          timezone: 'UTC',
          candles: [daily(0, 505_000), daily(1, 506_570)],
          coverage: coverage({ intraday_from: null }),
          has_more: false,
          next_before: null,
          pivots: null,
          support: null,
          resistance: null,
          as_of: new Date().toISOString(),
          price_fields: ['close'],
          cadence: 'daily_close'
        })
      }
      if (path === '/prices/current') {
        const prices: CurrentPricesResponse = {
          as_of: new Date().toISOString(),
          prices: {
            IR_SILVER_999: {
              value: 50_657,
              currency: 'IRT',
              unit: 'gram',
              source: 'tgju_history',
              observed_at: '2026-09-28T23:00:00Z',
              stale: false,
              change_24h_pct: null,
              cadence: 'daily_close'
            }
          }
        }
        return Promise.resolve(prices)
      }
      return new Promise(() => undefined)
    })
    renderPanel()
    await waitFor(() => expect(lw.createChartCalls).toBe(1))
    const status = document.querySelector('.tchart-status') as HTMLElement
    await waitFor(() => expect(within(status).getByText('daily close of 1405/07/06')).toBeInTheDocument())
    expect(within(status).queryByText(/ago$/)).toBeNull()
    expect(within(status).queryByText('STALE')).toBeNull()
    expect(within(status).getByText('2 daily closes')).toBeInTheDocument()
    expect(within(status).queryByText(/single-observation/)).toBeNull()
    // Close-only, like an index: a line, and no pivot card from one price.
    await waitFor(() => expect(lw.seriesDefs).toContain('Line'))
    expect(screen.queryByText('Pivot levels (classic)')).toBeNull()
  })

  it('shows what a registry series is, not only in a tooltip', async () => {
    // The picker's hover title never shows in a native dropdown, so silver's
    // "TGJU is the only source; junk bars…" note was invisible.
    window.localStorage.setItem('igp_chart_symbol', 'IR_SILVER_999')
    const note = 'TGJU silver_999: one daily settled close; three junk bars are held as suspect.'
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('/instruments')) {
        return Promise.resolve({
          items: [{ code: 'IR_SILVER_999', kind: 'market_price', name_en: 'Silver 999 (gram, Tehran)',
            name_fa: 'نقره ۹۹۹', domain: 'silver', quote_currency: 'IRT', unit: 'gram', enabled: true,
            is_proxy: false, notes: note }]
        })
      }
      if (path.startsWith('/market/candles')) {
        return Promise.resolve({
          symbol: 'IR_SILVER_999', interval: '1d', interval_seconds: DAY, timezone: 'UTC',
          candles: [bar(0, 150_000)], coverage: coverage({ intraday_from: null }), has_more: false,
          next_before: null, support: null, resistance: null, as_of: new Date().toISOString()
        })
      }
      return new Promise(() => undefined)
    })
    renderPanel()
    const card = await screen.findByTestId('trade-registry-instrument')
    expect(within(card).getByText(note)).toBeInTheDocument()
  })

  it('offers no Retry for a share the API declines to chart, and keeps its link apart', async () => {
    // A 409 is a refusal, not an outage: asking again cannot change it. Its
    // sentence used to run straight into "The Tehran market page →".
    const refusal = 'کچاد cannot be charted: it has never been ingested, so no adjustment exists for it'
    window.localStorage.setItem('igp_chart_symbol', 'EQ:22811176775480091')
    apiMock.mockImplementation((path: string) =>
      path.startsWith('/market/candles')
        ? Promise.reject(Object.assign(new Error(refusal), { status: 409, code: 'adjustment_unavailable' }))
        : new Promise(() => undefined)
    )
    renderPanel()
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(refusal))
    expect(within(screen.getByRole('alert')).queryByRole('button', { name: 'Retry' })).toBeNull()
    const card = screen.getByTestId('trade-instrument')
    const link = within(card).getByRole('link')
    expect(link.parentElement).toHaveClass('tchart-card-link')
  })

  it('still offers Retry when the request failed rather than being declined', async () => {
    window.localStorage.setItem('igp_chart_symbol', TSE_INDEX.EQUAL_WEIGHTED)
    apiMock.mockImplementation((path: string) =>
      path.startsWith('/market/candles')
        ? Promise.reject(Object.assign(new Error('Network error'), { status: 0 }))
        : new Promise(() => undefined)
    )
    renderPanel()
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Network error'))
    expect(within(screen.getByRole('alert')).getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('claims no age for a registry series before its newest observation is known', async () => {
    window.localStorage.setItem('igp_chart_symbol', 'IR_SILVER_999')
    // Candles answer; /prices/current never does.
    serveCandles({ symbol: 'IR_SILVER_999', candles: [bar(0, 150_000)], coverage: coverage({ intraday_from: null }) })
    renderPanel()
    await waitFor(() => expect(lw.createChartCalls).toBe(1))
    const status = document.querySelector('.tchart-status') as HTMLElement
    expect(within(status).getByText('age not known yet')).toBeInTheDocument()
    expect(status.querySelector('.dot-ok')).toBeNull()
  })

  it('says plainly when a registered symbol has nothing stored yet', async () => {
    window.localStorage.setItem('igp_chart_symbol', 'IR_SILVER_999')
    serveMarket({ IR_SILVER_999: { candles: [], coverage: coverage({ intraday_from: null }) } })
    renderPanel()
    await waitFor(() => expect(screen.getByText('No candle data')).toBeInTheDocument())
    expect(screen.getByText(/Nothing is stored for IR_SILVER_999 yet/)).toBeInTheDocument()
    // One request, not a retry loop.
    expect(candlePaths()).toHaveLength(1)
    // And no freshness claimed for data that does not exist.
    expect(document.querySelector('.tchart-status .dot-ok')).toBeNull()
    expect(within(document.querySelector('.tchart-status') as HTMLElement).getByText('no data')).toBeInTheDocument()
  })
})

describe('useCandles on a Tehran series', () => {
  function serveRevision(revision: () => string) {
    apiMock.mockImplementation((path: string) => {
      if (!path.startsWith('/market/candles')) return new Promise(() => undefined)
      return Promise.resolve({
        ...indexResponse(),
        interval: '1d',
        interval_seconds: DAY,
        timezone: 'UTC',
        has_more: false,
        next_before: null,
        support: null,
        resistance: null,
        as_of: new Date().toISOString(),
        revision: revision()
      })
    })
  }

  const firstPages = () =>
    apiMock.mock.calls.map((c) => String(c[0])).filter((p) => p.includes('limit=500'))

  it('carries what the series is and how old it is', async () => {
    serveRevision(() => 'r1')
    const { result } = renderHook(() => useCandles(TEDPIX, '1d', { pollMs: 0 }))
    await waitFor(() => expect(result.current.candles).toHaveLength(3))
    expect(result.current.priceFields).toEqual(['close'])
    expect(result.current.unit).toBe('index_points')
    expect(result.current.source).toBe('TSETMC')
    expect(result.current.dataAge?.newest_trade_date).toBe('2026-09-28')
    expect(result.current.notes).toEqual(['Index points, not a price.'])
    expect(result.current.loadedFor).toBe(`${TEDPIX}|1d`)
  })

  it('reloads every page when the stored series is restated under it', async () => {
    let revision = 'r1'
    serveRevision(() => revision)
    const { result } = renderHook(() => useCandles(TEDPIX, '1d', { pollMs: 25 }))
    await waitFor(() => expect(result.current.revision).toBe('r1'))
    expect(firstPages()).toHaveLength(1)

    // Polls under the same revision patch the tail only.
    await waitFor(() =>
      expect(apiMock.mock.calls.some((c) => String(c[0]).includes('limit=3'))).toBe(true)
    )
    expect(firstPages()).toHaveLength(1)

    // An ingest recomputed the corrections: the three newest sessions cannot
    // show it, the revision does.
    revision = 'r2'
    await waitFor(() => expect(result.current.revision).toBe('r2'))
    expect(firstPages().length).toBeGreaterThanOrEqual(2)
  })

  it('never stitches an older page from a restated series under the pages it holds', async () => {
    // The first page says older sessions exist; by the time the reader pans
    // back, an ingest has re-adjusted the share and the older page comes back
    // under a new revision. Merged, the seam would show a step no session made.
    const OLDER = [indexBar(-2, 1_000), indexBar(-1, 1_001)]
    let revision = 'r1'
    apiMock.mockImplementation((path: string) => {
      if (!path.startsWith('/market/candles')) return new Promise(() => undefined)
      const older = path.includes('before=')
      return Promise.resolve({
        ...indexResponse(),
        candles: older ? OLDER : INDEX_BARS,
        interval: '1d',
        interval_seconds: DAY,
        timezone: 'UTC',
        has_more: !older,
        next_before: older ? null : new Date(INDEX_BARS[0].t * 1000).toISOString(),
        support: null,
        resistance: null,
        as_of: new Date().toISOString(),
        revision: older ? 'r2' : revision
      })
    })
    const newestPages = () => firstPages().filter((p) => !p.includes('before='))
    const { result } = renderHook(() => useCandles(TEDPIX, '1d', { pollMs: 0 }))
    await waitFor(() => expect(result.current.revision).toBe('r1'))
    expect(newestPages()).toHaveLength(1)

    revision = 'r2'
    result.current.loadOlder()
    await waitFor(() => expect(apiMock.mock.calls.some((c) => String(c[0]).includes('before='))).toBe(true))
    // The whole series is fetched again under the new revision…
    await waitFor(() => expect(result.current.revision).toBe('r2'))
    expect(newestPages()).toHaveLength(2)
    // …and nothing from the other revision was merged into it.
    expect(result.current.candles.map((c) => c.t)).toEqual(INDEX_BARS.map((c) => c.t))
    expect(result.current.loadingOlder).toBe(false)
  })
})
