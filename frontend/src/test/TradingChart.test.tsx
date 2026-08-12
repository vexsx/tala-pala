import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import type { CandleCoverage, ChartCandle, ChartCandlesResponse } from '../api/types'

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
  reset() {
    lw.createChartCalls = 0
    lw.removeCalls = 0
    lw.setDataCalls = []
    lw.updateCalls = []
    lw.rangeHandlers = []
    lw.crosshairHandlers = []
    lw.visibleRangeCalls = []
    lw.chartOptions = []
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
    addSeries: () => series,
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
import { SettingsProvider } from '../lib/settings'
import { INTERVALS } from '../chart/intervals'

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
    expect(handle.candleSeries).toBeTruthy()
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
        <OhlcHeader symbol="IR_GOLD_18K" interval="1d" candles={[stamped]} hovered={null} unit="IRT" />
      </SettingsProvider>
    )
    expect(screen.getByText('1405/05/21 12:30')).toBeInTheDocument()
  })

  it('renders the bucket time in the Gregorian calendar when the user picked it', () => {
    window.localStorage.setItem('igp_calendar', 'gregorian')
    render(
      <SettingsProvider>
        <OhlcHeader symbol="IR_GOLD_18K" interval="1d" candles={[stamped]} hovered={null} unit="IRT" />
      </SettingsProvider>
    )
    expect(screen.getByText('2026-08-12 12:30')).toBeInTheDocument()
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
