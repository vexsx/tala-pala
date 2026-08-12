import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import type { IChartApi } from 'lightweight-charts'
import type {
  ChartCandle,
  TrendAlignmentResponse,
  TrendAlignmentState,
  TrendState,
  TrendTimeframe,
  TrendTimeframeKey
} from '../api/types'
import { ChartLegend, type ChartLegendProps } from '../chart/ChartLegend'
import { IndicatorMenu } from '../chart/IndicatorMenu'
import { IndicatorPanes } from '../chart/indicators/panes'
import {
  applyPreset,
  buildPlots,
  coldInstances,
  makeInstance,
  type ChartIndicatorState,
  type IndicatorInstance
} from '../chart/indicators/registry'
import type { TrendOverlayReading } from '../chart/overlays'
// The components' own source, so the "no maths here" guards cannot drift.
import legendSource from '../chart/ChartLegend.tsx?raw'
import overlaySource from '../chart/overlays.ts?raw'

const DAY = 86_400
const BASE_T = Date.parse('2026-08-01T00:00:00Z') / 1000

function candle(index: number, close: number): ChartCandle {
  return {
    t: BASE_T + index * DAY,
    open: close,
    high: close,
    low: close,
    close,
    volume: null,
    ticks: 9,
    confirmed: true,
    synthetic: false
  }
}

const CANDLES = [
  candle(0, 8_000_000),
  candle(1, 8_010_000),
  candle(2, 8_020_000),
  candle(3, 8_030_000)
]

const CTX = { candles: CANDLES, overlays: null, overlayTimes: [] as number[] }

function instance(kind: Parameters<typeof makeInstance>[0], settings = {}): IndicatorInstance {
  const made = makeInstance(kind, settings)
  if (!made.ok) throw new Error(made.message)
  return made.value
}

const EMPTY_TREND: TrendOverlayReading = {
  data: null,
  loading: false,
  error: null,
  reload: vi.fn(),
  unsupported: false
}

function legendProps(overrides: Partial<ChartLegendProps> = {}): ChartLegendProps {
  const state: ChartIndicatorState = overrides.state ?? {
    instances: [instance('ma', { method: 'sma', period: 3 })],
    overlays: { forecast: false, events: false, trend: false }
  }
  const plots = overrides.plots ?? buildPlots(state.instances, CTX)
  return {
    state,
    onChange: vi.fn(),
    plots,
    time: null,
    symbol: 'IR_GOLD_18K',
    unit: 'IRT',
    levels: { pivots: null, support: null, resistance: null },
    cold: coldInstances(state.instances, plots),
    forecast: { points: [], loading: false, error: null },
    events: {
      placement: { events: [], undated: 0, outside: 0 },
      loading: false,
      error: null,
      collectionEnabled: true
    },
    trend: EMPTY_TREND,
    ...overrides
  }
}

function timeframe(key: TrendTimeframeKey, trend: TrendState): TrendTimeframe {
  return {
    timeframe: key,
    trend,
    price: 8_120_000,
    ma26: 8_050_000,
    ma48: 7_990_000,
    ma220: 7_400_000,
    candle_open_time: '2026-08-12T08:00:00Z',
    candle_close_time: '2026-08-12T09:00:00Z',
    confirmed: true,
    data_fresh: true,
    ma_type: 'ema',
    history_points: 900,
    reason: ''
  }
}

function trendResponse(
  alignment: TrendAlignmentState,
  trends: Record<TrendTimeframeKey, TrendState>,
  overrides: Partial<TrendAlignmentResponse> = {}
): TrendAlignmentResponse {
  return {
    symbol: 'IR_GOLD_18K',
    alignment,
    previous_alignment: null,
    timeframes: {
      '1d': timeframe('1d', trends['1d']),
      '4h': timeframe('4h', trends['4h']),
      '1h': timeframe('1h', trends['1h'])
    },
    ma_type: 'ema',
    periods: { fast: 26, mid: 48, slow: 220 },
    data_fresh: true,
    calculated_at: '2026-08-12T09:05:00Z',
    last_transition_at: null,
    last_alert_at: null,
    ...overrides
  }
}

// ---------------------------------------------------------------------------
// Legend rows
// ---------------------------------------------------------------------------

describe('ChartLegend rows', () => {
  it('gives every active indicator a row with a value and all three controls', () => {
    render(<ChartLegend {...legendProps()} />)

    expect(screen.getByText('SMA 3')).toBeInTheDocument()
    expect(screen.getByLabelText('Hide SMA 3')).toBeInTheDocument()
    expect(screen.getByLabelText('SMA 3 settings')).toBeInTheDocument()
    expect(screen.getByLabelText('Remove SMA 3')).toBeInTheDocument()
  })

  it('shows the crosshair value, then falls back to the latest one', () => {
    const props = legendProps()
    const { rerender } = render(<ChartLegend {...props} time={CANDLES[2].t} />)
    // SMA 3 at bucket 2 is the mean of the first three closes.
    expect(screen.getByText('8,010,000')).toBeInTheDocument()

    rerender(<ChartLegend {...props} time={null} />)
    expect(screen.getByText('8,020,000')).toBeInTheDocument()
    expect(screen.queryByText('8,010,000')).not.toBeInTheDocument()
  })

  it('shows an em dash — never a borrowed number — for a bucket with no value', () => {
    render(<ChartLegend {...legendProps()} time={CANDLES[0].t} />)
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('honours the rial toggle in the legend value', () => {
    render(<ChartLegend {...legendProps()} unit="IRR" />)
    expect(screen.getByText('80,200,000')).toBeInTheDocument()
  })

  it('formats XAU/USD in dollars regardless of the toman toggle', () => {
    const state: ChartIndicatorState = {
      instances: [instance('ma', { method: 'sma', period: 2 })],
      overlays: { forecast: false, events: false, trend: false }
    }
    const plots = buildPlots(state.instances, {
      candles: [candle(0, 3_400), candle(1, 3_420)],
      overlays: null,
      overlayTimes: []
    })
    render(<ChartLegend {...legendProps({ state, plots })} symbol="XAUUSD" unit="IRR" />)
    expect(screen.getByText('$3,410.00')).toBeInTheDocument()
  })

  it('keeps the value in the repo numeric classes', () => {
    const { container } = render(<ChartLegend {...legendProps()} />)
    const cell = Array.from(container.querySelectorAll('.tchart-legend-value')).find((el) =>
      el.textContent?.includes('8,020,000')
    )
    expect(cell?.className).toContain('num')
    expect(cell?.className).toContain('mono')
  })

  it('says an indicator has no data rather than drawing a blank row', () => {
    const state: ChartIndicatorState = {
      instances: [instance('ma', { method: 'ema', period: 220 })],
      overlays: { forecast: false, events: false, trend: false }
    }
    render(<ChartLegend {...legendProps({ state })} />)
    expect(screen.getByText('no data')).toBeInTheDocument()
  })

  it('reports the levels a levels-only indicator contributes', () => {
    const state: ChartIndicatorState = {
      instances: [instance('sr')],
      overlays: { forecast: false, events: false, trend: false }
    }
    render(
      <ChartLegend
        {...legendProps({ state })}
        levels={{ pivots: null, support: 7_900_000, resistance: 8_300_000 }}
      />
    )
    expect(screen.getByText('7,900,000')).toBeInTheDocument()
    expect(screen.getByText('8,300,000')).toBeInTheDocument()
  })

  it('says so plainly when nothing is on the chart', () => {
    render(<ChartLegend {...legendProps({ state: applyPreset('clean') })} />)
    expect(screen.getByText(/No indicators\. Candles only/)).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// Legend controls
// ---------------------------------------------------------------------------

describe('ChartLegend controls', () => {
  it('toggles visibility without losing the row', () => {
    const onChange = vi.fn()
    render(<ChartLegend {...legendProps({ onChange })} />)

    const toggle = screen.getByLabelText('Hide SMA 3')
    expect(toggle).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(toggle)

    const next = onChange.mock.calls[0][0] as ChartIndicatorState
    expect(next.instances).toHaveLength(1)
    expect(next.instances[0].visible).toBe(false)
  })

  it('labels a hidden indicator by word and glyph, not by dimming alone', () => {
    const state: ChartIndicatorState = {
      instances: [{ ...instance('ma', { method: 'sma', period: 3 }), visible: false }],
      overlays: { forecast: false, events: false, trend: false }
    }
    render(<ChartLegend {...legendProps({ state })} />)
    const toggle = screen.getByLabelText('Show SMA 3')
    expect(toggle).toHaveAttribute('aria-pressed', 'false')
    expect(toggle.textContent).toBe('○')
  })

  it('removes an indicator', () => {
    const onChange = vi.fn()
    render(<ChartLegend {...legendProps({ onChange })} />)
    fireEvent.click(screen.getByLabelText('Remove SMA 3'))
    expect((onChange.mock.calls[0][0] as ChartIndicatorState).instances).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// Settings and validation
// ---------------------------------------------------------------------------

describe('indicator settings', () => {
  function openSettings(onChange = vi.fn()) {
    render(<ChartLegend {...legendProps({ onChange })} />)
    fireEvent.click(screen.getByLabelText('SMA 3 settings'))
    return onChange
  }

  it('opens a form with the period, the method and the source', () => {
    openSettings()
    expect(screen.getByLabelText('Edit SMA 3')).toBeInTheDocument()
    expect(screen.getByLabelText('Period')).toHaveValue('3')
    expect(screen.getByLabelText('Method')).toHaveValue('sma')
    expect(screen.getByLabelText('Source')).toHaveValue('close')
  })

  const refusals: Array<[string, string, RegExp]> = [
    ['a fraction', '14.5', /whole number/i],
    ['zero', '0', /at least/i],
    ['a negative', '-5', /at least/i],
    ['an absurd period', '9999999', /1000 bars or fewer/i],
    ['text', 'twenty', /number of bars/i],
    ['nothing at all', '', /number of bars/i]
  ]

  for (const [name, typed, message] of refusals) {
    it(`refuses ${name} with a message and leaves the chart alone`, () => {
      const onChange = openSettings()
      fireEvent.change(screen.getByLabelText('Period'), { target: { value: typed } })
      fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

      expect(screen.getByRole('alert')).toHaveTextContent(message)
      expect(onChange).not.toHaveBeenCalled()
      // The form stays open on the bad value rather than closing on a lie.
      expect(screen.getByLabelText('Edit SMA 3')).toBeInTheDocument()
    })
  }

  it('applies a valid period and closes', () => {
    const onChange = openSettings()
    fireEvent.change(screen.getByLabelText('Period'), { target: { value: '50' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    const next = onChange.mock.calls[0][0] as ChartIndicatorState
    expect(next.instances[0].id).toBe('ma:sma:50')
    expect(screen.queryByLabelText('Edit SMA 3')).not.toBeInTheDocument()
  })

  it('switches method between EMA and SMA', () => {
    const onChange = openSettings()
    fireEvent.change(screen.getByLabelText('Method'), { target: { value: 'ema' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    expect((onChange.mock.calls[0][0] as ChartIndicatorState).instances[0].id).toBe('ma:ema:3')
  })

  it('refuses a change that would duplicate another row', () => {
    const state: ChartIndicatorState = {
      instances: [
        instance('ma', { method: 'sma', period: 3 }),
        instance('ma', { method: 'sma', period: 5 })
      ],
      overlays: { forecast: false, events: false, trend: false }
    }
    const onChange = vi.fn()
    render(<ChartLegend {...legendProps({ state, onChange })} />)
    fireEvent.click(screen.getByLabelText('SMA 3 settings'))
    fireEvent.change(screen.getByLabelText('Period'), { target: { value: '5' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    expect(screen.getByRole('alert')).toHaveTextContent(/already on the chart/)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('refuses MACD periods that are not in order', () => {
    const state: ChartIndicatorState = {
      instances: [instance('macd')],
      overlays: { forecast: false, events: false, trend: false }
    }
    render(<ChartLegend {...legendProps({ state })} />)
    fireEvent.click(screen.getByLabelText('MACD 12/26/9 settings'))
    fireEvent.change(screen.getByLabelText('Fast period'), { target: { value: '30' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    expect(screen.getByRole('alert')).toHaveTextContent(/shorter than the slow/i)
  })

  it('says a server indicator has no local settings instead of offering fake ones', () => {
    const state: ChartIndicatorState = {
      instances: [instance('bollinger')],
      overlays: { forecast: false, events: false, trend: false }
    }
    render(<ChartLegend {...legendProps({ state })} />)
    fireEvent.click(screen.getByLabelText('Bollinger settings'))
    expect(screen.getByText(/computed by the server/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Period')).not.toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// Menu and presets
// ---------------------------------------------------------------------------

describe('IndicatorMenu', () => {
  function openMenu(state = applyPreset('trend')) {
    const onChange = vi.fn()
    render(<IndicatorMenu state={state} onChange={onChange} />)
    fireEvent.click(screen.getByLabelText('Indicators'))
    return onChange
  }

  it('reports the active count on the trigger', () => {
    render(<IndicatorMenu state={applyPreset('trend')} onChange={vi.fn()} />)
    expect(screen.getByLabelText('Indicators')).toHaveTextContent('Indicators (4)')
  })

  it('applies the Trend Alignment preset: EMA 26/48/220, SuperTrend and the server read', () => {
    const onChange = openMenu()
    fireEvent.click(screen.getByLabelText('Trend alignment preset'))
    const next = onChange.mock.calls[0][0] as ChartIndicatorState
    expect(next.instances.map((i) => i.id)).toEqual([
      'ma:ema:26',
      'ma:ema:48',
      'ma:ema:220',
      'supertrend'
    ])
    expect(next.overlays.trend).toBe(true)
  })

  it('applies the Clean preset', () => {
    const onChange = openMenu()
    fireEvent.click(screen.getByLabelText('Clean preset'))
    expect((onChange.mock.calls[0][0] as ChartIndicatorState).instances).toEqual([])
  })

  it('applies the Momentum preset', () => {
    const onChange = openMenu()
    fireEvent.click(screen.getByLabelText('Momentum preset'))
    expect((onChange.mock.calls[0][0] as ChartIndicatorState).instances.map((i) => i.id)).toEqual([
      'rsi:14',
      'macd:12:26:9'
    ])
  })

  it('marks the preset the board currently matches', () => {
    render(<IndicatorMenu state={applyPreset('momentum')} onChange={vi.fn()} />)
    fireEvent.click(screen.getByLabelText('Indicators'))
    expect(screen.getByLabelText('Momentum preset')).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByLabelText('Trend preset')).toHaveAttribute('aria-checked', 'false')
  })

  it('toggles an indicator on and back off', () => {
    const onAdd = vi.fn()
    const first = render(<IndicatorMenu state={applyPreset('clean')} onChange={onAdd} />)
    fireEvent.click(screen.getByLabelText('Indicators'))
    const control = screen.getByRole('menuitemcheckbox', { name: 'Bollinger' })
    expect(control).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(control)
    const added = onAdd.mock.calls[0][0] as ChartIndicatorState
    expect(added.instances.map((i) => i.id)).toEqual(['bollinger'])
    first.unmount()

    const onRemove = vi.fn()
    render(<IndicatorMenu state={added} onChange={onRemove} />)
    fireEvent.click(screen.getByLabelText('Indicators'))
    const on = screen.getByRole('menuitemcheckbox', { name: 'Bollinger' })
    expect(on).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(on)
    expect((onRemove.mock.calls[0][0] as ChartIndicatorState).instances).toEqual([])
  })

  it('adds a moving average on a free period rather than refusing the second click', () => {
    const onChange = openMenu(applyPreset('clean'))
    fireEvent.click(screen.getByLabelText('Add moving average'))
    expect((onChange.mock.calls[0][0] as ChartIndicatorState).instances[0].id).toBe('ma:ema:26')
  })

  it('offers each of the three overlays and toggles them independently', () => {
    for (const label of ['Forecast', 'News events', 'Trend alignment']) {
      const onChange = vi.fn()
      const { unmount } = render(
        <IndicatorMenu state={applyPreset('clean')} onChange={onChange} />
      )
      fireEvent.click(screen.getByLabelText('Indicators'))
      const control = screen.getByRole('menuitemcheckbox', { name: label })
      expect(control).toHaveAttribute('aria-checked', 'false')
      fireEvent.click(control)
      const next = onChange.mock.calls[0][0] as ChartIndicatorState
      expect(Object.values(next.overlays).filter(Boolean)).toHaveLength(1)
      unmount()
    }
  })

  it('turns an overlay back off', () => {
    const state = applyPreset('alignment')
    const onChange = vi.fn()
    render(<IndicatorMenu state={state} onChange={onChange} />)
    fireEvent.click(screen.getByLabelText('Indicators'))
    fireEvent.click(screen.getByRole('menuitemcheckbox', { name: 'Trend alignment' }))
    expect((onChange.mock.calls[0][0] as ChartIndicatorState).overlays.trend).toBe(false)
  })

  it('never offers a volume pane — this data source carries no volume', () => {
    render(<IndicatorMenu state={applyPreset('full')} onChange={vi.fn()} />)
    fireEvent.click(screen.getByLabelText('Indicators'))
    expect(screen.queryByText(/volume/i)).not.toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// Forecast overlay block
// ---------------------------------------------------------------------------

describe('forecast overlay', () => {
  const state: ChartIndicatorState = {
    instances: [],
    overlays: { forecast: true, events: false, trend: false }
  }

  it('is labelled an ESTIMATE with its uncertainty spelled out', () => {
    render(
      <ChartLegend
        {...legendProps({
          state,
          forecast: {
            points: [
              { time: CANDLES[3].t, point: 8_030_000, lower: 8_030_000, upper: 8_030_000 },
              { time: CANDLES[3].t + DAY, point: 8_200_000, lower: 8_000_000, upper: 8_400_000 }
            ],
            loading: false,
            error: null
          }
        })}
      />
    )
    const block = screen.getByLabelText('Forecast overlay')
    expect(within(block).getByText('ESTIMATE')).toBeInTheDocument()
    expect(within(block).getByText('8,200,000')).toBeInTheDocument()
    expect(block.textContent).toContain('8,000,000 – 8,400,000')
    expect(block.textContent).toMatch(/not a path the price will take/)
  })

  it('says nothing is drawn rather than implying a forecast exists', () => {
    render(<ChartLegend {...legendProps({ state })} />)
    expect(screen.getByText(/No forecast lands after the last candle/)).toBeInTheDocument()
    expect(screen.queryByText(/–/)).not.toBeInTheDocument()
  })

  it('disappears entirely when the overlay is off', () => {
    render(<ChartLegend {...legendProps()} />)
    expect(screen.queryByLabelText('Forecast overlay')).not.toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// Event overlay block
// ---------------------------------------------------------------------------

describe('news event overlay', () => {
  const state: ChartIndicatorState = {
    instances: [],
    overlays: { forecast: false, events: true, trend: false }
  }

  it('shows an honest empty state — production has no stored events', () => {
    render(<ChartLegend {...legendProps({ state })} />)
    const block = screen.getByLabelText('News events overlay')
    expect(block.textContent).toMatch(/No stored headline carries a publication time/)
  })

  it('explains an empty feed caused by collection being switched off', () => {
    render(
      <ChartLegend
        {...legendProps({
          state,
          events: {
            placement: { events: [], undated: 0, outside: 0 },
            loading: false,
            error: null,
            collectionEnabled: false
          }
        })}
      />
    )
    expect(screen.getByText(/News collection is switched off/)).toBeInTheDocument()
  })

  it('counts the headlines it refused to place instead of hiding them', () => {
    render(
      <ChartLegend
        {...legendProps({
          state,
          events: {
            placement: {
              events: [
                {
                  id: 1,
                  time: CANDLES[1].t,
                  title: 'Headline',
                  source: 'Example',
                  urgent: true,
                  estimated: false
                }
              ],
              undated: 3,
              outside: 2
            },
            loading: false,
            error: null,
            collectionEnabled: true
          }
        })}
      />
    )
    const block = screen.getByLabelText('News events overlay')
    expect(block.textContent).toContain('1 marked')
    expect(block.textContent).toContain('3 without a publication time (never placed)')
    expect(block.textContent).toContain('2 outside this window')
  })

  it('disappears entirely when the overlay is off', () => {
    render(<ChartLegend {...legendProps()} />)
    expect(screen.queryByLabelText('News events overlay')).not.toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// Trend alignment overlay
// ---------------------------------------------------------------------------

describe('trend alignment overlay', () => {
  const state: ChartIndicatorState = {
    instances: [],
    overlays: { forecast: false, events: false, trend: true }
  }

  function renderTrend(reading: Partial<TrendOverlayReading>) {
    render(<ChartLegend {...legendProps({ state, trend: { ...EMPTY_TREND, ...reading } })} />)
  }

  it('renders the per-timeframe state and the overall verdict from the API', () => {
    renderTrend({
      data: trendResponse('not_aligned', { '1d': 'bullish', '4h': 'bullish', '1h': 'neutral' })
    })
    expect(screen.getByRole('listitem', { name: '1D BULLISH' })).toBeInTheDocument()
    expect(screen.getByRole('listitem', { name: '4H BULLISH' })).toBeInTheDocument()
    expect(screen.getByRole('listitem', { name: '1H NEUTRAL' })).toBeInTheDocument()
    expect(screen.getByText(/Overall: NOT ALIGNED/)).toBeInTheDocument()
  })

  it('pairs every state with a glyph as well as a word', () => {
    renderTrend({
      data: trendResponse('full_bearish', { '1d': 'bearish', '4h': 'bearish', '1h': 'bearish' })
    })
    expect(screen.getByRole('listitem', { name: '1D BEARISH' }).textContent).toContain('▼')
    expect(screen.getByText(/Overall: FULL BEARISH/)).toBeInTheDocument()
  })

  it('takes the verdict verbatim even when it contradicts the per-timeframe rows', () => {
    // The server evaluates CLOSED candles across three timeframes; the chart is
    // showing one, live. When the two disagree, the server wins — recomputing
    // here is exactly the bug this test exists to prevent.
    renderTrend({
      data: trendResponse('not_aligned', { '1d': 'bullish', '4h': 'bullish', '1h': 'bullish' })
    })
    expect(screen.getByText(/Overall: NOT ALIGNED/)).toBeInTheDocument()
    expect(screen.queryByText(/FULL BULLISH/)).not.toBeInTheDocument()
  })

  it('says UNAVAILABLE for a timeframe the server could not read', () => {
    const payload = trendResponse('not_aligned', {
      '1d': 'bullish',
      '4h': 'bullish',
      '1h': 'bullish'
    })
    payload.timeframes['1h'] = undefined
    renderTrend({ data: payload })
    expect(screen.getByRole('listitem', { name: '1H UNAVAILABLE' })).toBeInTheDocument()
  })

  it('flags a stale read', () => {
    renderTrend({
      data: trendResponse(
        'full_bullish',
        { '1d': 'bullish', '4h': 'bullish', '1h': 'bullish' },
        { data_fresh: false }
      )
    })
    expect(screen.getByText('STALE')).toBeInTheDocument()
  })

  it('claims no verdict while the read is failing, and offers a retry', () => {
    const reload = vi.fn()
    renderTrend({ error: 'trend service unavailable', reload })
    expect(screen.getByRole('alert')).toHaveTextContent('TREND READ UNAVAILABLE')
    expect(screen.queryByText(/Overall/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(reload).toHaveBeenCalled()
  })

  it('says the symbol is not served rather than showing a blank read', () => {
    renderTrend({ unsupported: true })
    expect(screen.getByText('Not served for this symbol.')).toBeInTheDocument()
  })

  it('disappears entirely when the overlay is off', () => {
    render(<ChartLegend {...legendProps()} />)
    expect(screen.queryByLabelText('Trend alignment overlay')).not.toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// The chart never re-derives the server's conclusions
// ---------------------------------------------------------------------------

describe('the chart displays the trend read, it never computes it', () => {
  it('does no smoothing or period arithmetic in the legend or the overlays', () => {
    for (const source of [legendSource, overlaySource]) {
      for (const pattern of [/Math\.pow/, /Math\.exp/, /\balpha\b/i, /smoothing/i, /\bmultiplier\b/i]) {
        expect(source).not.toMatch(pattern)
      }
    }
  })

  it('never combines a price or a moving average with an operator', () => {
    for (const source of [legendSource, overlaySource]) {
      expect(source).not.toMatch(/(ma26|ma48|ma220)\s*[-+*/<>]/)
      expect(source).not.toMatch(/[-+*/<>]\s*\w*\.(ma26|ma48|ma220)\b/)
    }
  })

  it('never imports the indicator maths into the display layers', () => {
    for (const source of [legendSource, overlaySource]) {
      expect(source).not.toMatch(/\b(emaSeries|smaSeries|rsiSeries|macdSeries)\b/)
    }
  })

  it('reads the alignment and each trend straight off the payload', () => {
    expect(legendSource).toContain('data.alignment')
    expect(legendSource).toContain('timeframe?.trend')
  })

  it('pairs every state with a distinct glyph and a word', () => {
    for (const glyph of ['▲', '▼', '●', '—', '◆']) {
      expect(legendSource).toContain(glyph)
    }
    for (const word of ['BULLISH', 'BEARISH', 'NEUTRAL', 'UNAVAILABLE', 'NOT ALIGNED']) {
      expect(legendSource).toContain(word)
    }
  })

  it('renders headlines and reasons as text, never as HTML', () => {
    for (const source of [legendSource, overlaySource]) {
      expect(source).not.toContain('dangerouslySetInnerHTML')
      expect(source).not.toContain('innerHTML')
    }
  })
})

// ---------------------------------------------------------------------------
// Oscillator panes
// ---------------------------------------------------------------------------

function fakeSeries() {
  return {
    setData: vi.fn(),
    applyOptions: vi.fn(),
    createPriceLine: vi.fn((o: unknown) => o),
    removePriceLine: vi.fn()
  }
}

function fakePane(index: number) {
  return {
    paneIndex: () => index,
    setStretchFactor: vi.fn(),
    getStretchFactor: () => 1,
    setHeight: vi.fn(),
    getHeight: () => 100
  }
}

function fakeChart() {
  const panes = [fakePane(0)]
  const added: number[] = []
  const removed: unknown[] = []
  const api = {
    addSeries: (_def: unknown, _opts: unknown, paneIndex = 0) => {
      added.push(paneIndex)
      return fakeSeries()
    },
    removeSeries: (s: unknown) => removed.push(s),
    addPane: () => {
      const pane = fakePane(panes.length)
      panes.push(pane)
      return pane
    },
    panes: () => panes,
    removePane: (i: number) => panes.splice(i, 1)
  }
  return { api: api as unknown as IChartApi, panes, added, removed }
}

const RSI = instance('rsi', { period: 2 })
const MACD = instance('macd', { fast: 2, slow: 3, signal: 2 })

describe('IndicatorPanes', () => {
  it('renders nothing at all when no oscillator is active', () => {
    const { api } = fakeChart()
    const { container } = render(
      <IndicatorPanes chart={api} plots={[]} height={440} time={null} symbol="IR_GOLD_18K" unit="IRT" />
    )
    expect(container.innerHTML).toBe('')
  })

  it('opens one pane per oscillator on the price chart itself, so the crosshair is shared', () => {
    const chart = fakeChart()
    const plots = buildPlots([RSI, MACD], CTX).filter((p) => p.paneKey !== null)
    render(
      <IndicatorPanes
        chart={chart.api}
        plots={plots}
        height={660}
        time={null}
        symbol="IR_GOLD_18K"
        unit="IRT"
      />
    )
    expect(chart.panes).toHaveLength(3)
    // RSI on pane 1, all three MACD series on pane 2.
    expect(chart.added).toEqual([1, 2, 2, 2])
  })

  it('keeps the candles the same size by stretching the price pane', () => {
    const chart = fakeChart()
    const plots = buildPlots([RSI], CTX).filter((p) => p.paneKey !== null)
    render(
      <IndicatorPanes
        chart={chart.api}
        plots={plots}
        height={550}
        time={null}
        symbol="IR_GOLD_18K"
        unit="IRT"
      />
    )
    // 550 total - 110 for the pane leaves 440 of candles, i.e. 4 : 1.
    expect(chart.panes[0].setStretchFactor).toHaveBeenCalledWith(4)
    expect(chart.panes[1].setStretchFactor).toHaveBeenCalledWith(1)
  })

  it('captions the pane with its name and the value at the crosshair', () => {
    const chart = fakeChart()
    const plots = buildPlots([RSI], CTX).filter((p) => p.paneKey !== null)
    const { rerender } = render(
      <IndicatorPanes
        chart={chart.api}
        plots={plots}
        height={550}
        time={CANDLES[2].t}
        symbol="IR_GOLD_18K"
        unit="IRT"
      />
    )
    const row = screen.getByLabelText('Oscillator panes')
    expect(row.textContent).toContain('RSI 2')
    // An unbroken rally pins Wilder's RSI at 100.
    expect(row.textContent).toContain('100.0')

    rerender(
      <IndicatorPanes
        chart={chart.api}
        plots={[]}
        height={440}
        time={null}
        symbol="IR_GOLD_18K"
        unit="IRT"
      />
    )
    expect(screen.queryByLabelText('Oscillator panes')).not.toBeInTheDocument()
  })

  it('tears the panes and their series down when the oscillator is removed', () => {
    const chart = fakeChart()
    const plots = buildPlots([RSI], CTX).filter((p) => p.paneKey !== null)
    const { unmount } = render(
      <IndicatorPanes
        chart={chart.api}
        plots={plots}
        height={550}
        time={null}
        symbol="IR_GOLD_18K"
        unit="IRT"
      />
    )
    expect(chart.panes).toHaveLength(2)
    unmount()
    expect(chart.removed).toHaveLength(1)
    expect(chart.panes).toHaveLength(1)
  })

  it('says so plainly when the chart engine has no sub-panes', () => {
    const plots = buildPlots([RSI], CTX).filter((p) => p.paneKey !== null)
    const bare = { addSeries: vi.fn(), removeSeries: vi.fn() } as unknown as IChartApi
    render(
      <IndicatorPanes
        chart={bare}
        plots={plots}
        height={550}
        time={null}
        symbol="IR_GOLD_18K"
        unit="IRT"
      />
    )
    expect(screen.getByRole('status').textContent).toMatch(/no sub-panes/)
  })
})
