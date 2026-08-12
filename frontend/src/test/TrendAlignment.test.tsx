import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { TrendAlignmentCard, TrendAlignmentTable } from '../components/TrendAlignment'
import { SettingsProvider } from '../lib/settings'
// The components' own source, so the "no EMA maths here" guard cannot drift.
import trendSource from '../components/TrendAlignment.tsx?raw'
import overviewSource from '../pages/Overview.tsx?raw'
import technicalSource from '../pages/Technical.tsx?raw'
import type {
  TrendAlignmentResponse,
  TrendAlignmentState,
  TrendState,
  TrendTimeframe,
  TrendTimeframeKey
} from '../api/types'

// Both views fetch through the shared client; mock the transport so the tests
// stay hermetic (same pattern as OsintStream.test / AdvisorCard.test).
vi.mock('../api/client', () => ({
  api: vi.fn(() => new Promise(() => undefined)),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : 'Unexpected error')
}))
import { api } from '../api/client'

const apiMock = api as unknown as Mock

/** 2026-08-12T09:00:00Z is 12:30 Tehran, i.e. 1405/05/21 in the Jalali calendar. */
const CLOSE_1D = '2026-08-11T20:30:00Z'
const CLOSE_4H = '2026-08-12T08:30:00Z'
const CLOSE_1H = '2026-08-12T09:00:00Z'

function timeframe(
  key: TrendTimeframeKey,
  trend: TrendState,
  overrides: Partial<TrendTimeframe> = {}
): TrendTimeframe {
  return {
    timeframe: key,
    trend,
    price: 8_120_000,
    ma26: 8_050_000,
    ma48: 7_990_000,
    ma220: 7_400_000,
    candle_open_time: '2026-08-12T08:00:00Z',
    candle_close_time: key === '1d' ? CLOSE_1D : key === '4h' ? CLOSE_4H : CLOSE_1H,
    confirmed: true,
    data_fresh: true,
    ma_type: 'ema',
    history_points: 900,
    reason: '',
    ...overrides
  }
}

function response(
  alignment: TrendAlignmentState,
  trends: Record<TrendTimeframeKey, TrendState>,
  overrides: Partial<TrendAlignmentResponse> = {}
): TrendAlignmentResponse {
  return {
    symbol: 'IR_GOLD_18K',
    alignment,
    previous_alignment: 'not_aligned',
    timeframes: {
      '1d': timeframe('1d', trends['1d']),
      '4h': timeframe('4h', trends['4h']),
      '1h': timeframe('1h', trends['1h'])
    },
    ma_type: 'ema',
    periods: { fast: 26, mid: 48, slow: 220 },
    data_fresh: true,
    calculated_at: '2026-08-12T09:05:00Z',
    last_transition_at: '2026-08-11T06:00:00Z',
    last_alert_at: null,
    ...overrides
  }
}

const ALL_BULLISH: Record<TrendTimeframeKey, TrendState> = {
  '1d': 'bullish',
  '4h': 'bullish',
  '1h': 'bullish'
}
const ALL_BEARISH: Record<TrendTimeframeKey, TrendState> = {
  '1d': 'bearish',
  '4h': 'bearish',
  '1h': 'bearish'
}

/** Render a view against a settled payload and wait for the skeleton to go. */
async function renderView(node: React.ReactElement, payload: TrendAlignmentResponse | null) {
  if (payload !== null) apiMock.mockResolvedValue(payload)
  render(node)
  await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())
}

beforeEach(() => {
  apiMock.mockReset()
  apiMock.mockImplementation(() => new Promise(() => undefined))
})

describe('TrendAlignmentCard (Overview compact view)', () => {
  it('requests the contracted path and shows a skeleton on first load only', () => {
    render(<TrendAlignmentCard />)
    expect(apiMock.mock.calls[0][0]).toBe('/market/trend-alignment?symbol=IR_GOLD_18K')
    expect(screen.getByRole('status')).toBeInTheDocument()
    // Nothing is invented while the first request is in flight.
    expect(screen.queryByText(/BULLISH|BEARISH|NOT ALIGNED/)).not.toBeInTheDocument()
  })

  it('reads full bullish with a glyph and a word on every row', async () => {
    await renderView(<TrendAlignmentCard />, response('full_bullish', ALL_BULLISH))

    expect(screen.getByText('TREND ALIGNMENT')).toBeInTheDocument()
    for (const label of ['1D', '4H', '1H']) {
      const row = screen.getByRole('listitem', { name: `${label} BULLISH` })
      expect(row.textContent).toContain('▲')
      expect(row.textContent).toContain('BULLISH')
    }
    expect(screen.getAllByText('FULL BULLISH').length).toBeGreaterThan(0)
  })

  it('reads full bearish', async () => {
    await renderView(<TrendAlignmentCard />, response('full_bearish', ALL_BEARISH))

    const row = screen.getByRole('listitem', { name: '1H BEARISH' })
    expect(row.textContent).toContain('▼')
    expect(screen.getAllByText('FULL BEARISH').length).toBeGreaterThan(0)
    expect(screen.queryByText('FULL BULLISH')).not.toBeInTheDocument()
  })

  it('reads a mixed board as NOT ALIGNED without averaging the disagreement away', async () => {
    await renderView(
      <TrendAlignmentCard />,
      response('not_aligned', { '1d': 'bullish', '4h': 'bearish', '1h': 'neutral' })
    )

    expect(screen.getByRole('listitem', { name: '1D BULLISH' })).toBeInTheDocument()
    expect(screen.getByRole('listitem', { name: '4H BEARISH' })).toBeInTheDocument()
    expect(screen.getByRole('listitem', { name: '1H NEUTRAL' })).toBeInTheDocument()
    expect(screen.getAllByText('NOT ALIGNED').length).toBeGreaterThan(0)
  })

  it('marks a neutral timeframe with its own glyph', async () => {
    await renderView(
      <TrendAlignmentCard />,
      response('not_aligned', { '1d': 'neutral', '4h': 'neutral', '1h': 'neutral' })
    )
    const row = screen.getByRole('listitem', { name: '1D NEUTRAL' })
    expect(row.textContent).toContain('●')
    expect(row.textContent).not.toContain('▲')
    expect(row.textContent).not.toContain('▼')
  })

  it('says UNAVAILABLE — and why — instead of guessing a missing timeframe', async () => {
    const payload = response('not_aligned', { '1d': 'bullish', '4h': 'bullish', '1h': 'bullish' })
    payload.timeframes['1h'] = timeframe('1h', 'unavailable', {
      price: null,
      ma26: null,
      ma48: null,
      ma220: null,
      data_fresh: false,
      reason: '120 completed candles < 220 required for the slow MA'
    })
    await renderView(<TrendAlignmentCard />, payload)

    const row = screen.getByRole('listitem', { name: '1H UNAVAILABLE' })
    expect(row.textContent).toContain('—')
    expect(row).toHaveAttribute('title', '120 completed candles < 220 required for the slow MA')
  })

  it('flags a stale read with a STALE chip', async () => {
    await renderView(
      <TrendAlignmentCard />,
      response('full_bullish', ALL_BULLISH, { data_fresh: false })
    )
    expect(screen.getByText('STALE')).toBeInTheDocument()
  })

  it('shows no STALE chip on a fresh read', async () => {
    await renderView(<TrendAlignmentCard />, response('full_bullish', ALL_BULLISH))
    expect(screen.queryByText('STALE')).not.toBeInTheDocument()
  })

  it('keeps its frame on failure and offers a working retry', async () => {
    apiMock.mockRejectedValue(new Error('trend service unavailable'))
    render(<TrendAlignmentCard />)
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())

    expect(screen.getByText('TREND ALIGNMENT')).toBeInTheDocument()
    expect(screen.getByText('TREND READ UNAVAILABLE')).toBeInTheDocument()
    expect(screen.getByText('trend service unavailable')).toBeInTheDocument()
    // No verdict is claimed while the read failed.
    expect(screen.queryByText(/FULL BULLISH|FULL BEARISH/)).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(apiMock).toHaveBeenCalledTimes(2))
  })

  it('says NEVER EVALUATED rather than inventing a not-aligned read', async () => {
    await renderView(
      <TrendAlignmentCard />,
      response('not_aligned', ALL_BULLISH, {
        timeframes: {},
        calculated_at: null,
        previous_alignment: null,
        last_transition_at: null,
        note: 'never_evaluated'
      })
    )
    expect(screen.getByText('NEVER EVALUATED')).toBeInTheDocument()
    expect(screen.queryAllByRole('listitem')).toHaveLength(0)
    expect(screen.queryByText('NOT ALIGNED')).not.toBeInTheDocument()
  })

  it('summarises the whole state in the card aria-label', async () => {
    await renderView(
      <TrendAlignmentCard />,
      response('not_aligned', { '1d': 'bullish', '4h': 'bearish', '1h': 'neutral' })
    )
    const card = screen.getByRole('region', {
      name: 'Trend alignment, 18k gold: not aligned. 1D bullish, 4H bearish, 1H neutral.'
    })
    expect(card).toBeInTheDocument()
  })

  it('names the stale condition in the aria-label too', async () => {
    await renderView(
      <TrendAlignmentCard />,
      response('full_bearish', ALL_BEARISH, { data_fresh: false })
    )
    expect(
      screen.getByRole('region', {
        name: 'Trend alignment, 18k gold: full bearish, data stale. 1D bearish, 4H bearish, 1H bearish.'
      })
    ).toBeInTheDocument()
  })
})

describe('TrendAlignmentTable (Technical detailed view)', () => {
  it('renders the contracted columns, labelled from the API periods', async () => {
    await renderView(<TrendAlignmentTable />, response('full_bullish', ALL_BULLISH))

    const headers = screen.getAllByRole('columnheader').map((h) => h.textContent)
    expect(headers).toEqual([
      'Timeframe',
      'Price',
      'EMA26',
      'EMA48',
      'EMA220',
      'Trend',
      'Candle close',
      'Fresh'
    ])
  })

  it('states the overall verdict above the table', async () => {
    await renderView(<TrendAlignmentTable />, response('full_bullish', ALL_BULLISH))
    expect(screen.getByText('Overall')).toBeInTheDocument()
    expect(screen.getByText('FULL BULLISH')).toBeInTheDocument()
  })

  it('formats 18k prices with the toman toggle and Jalali candle times', async () => {
    await renderView(<TrendAlignmentTable />, response('full_bullish', ALL_BULLISH))

    expect(screen.getAllByText('8,120,000').length).toBe(3)
    expect(screen.getAllByText('8,050,000').length).toBe(3)
    expect(screen.getAllByText('7,990,000').length).toBe(3)
    expect(screen.getAllByText('7,400,000').length).toBe(3)
    // Tehran wall clock in the Jalali calendar (the default toggle position).
    expect(screen.getByText('1405/05/21 12:30')).toBeInTheDocument()
    expect(screen.getByText(/Prices in IRT per gram\./)).toBeInTheDocument()
  })

  it('honours the rial toggle', async () => {
    window.localStorage.setItem('igp_unit', 'IRR')
    await renderView(
      <SettingsProvider>
        <TrendAlignmentTable />
      </SettingsProvider>,
      response('full_bullish', ALL_BULLISH)
    )
    expect(screen.getAllByText('81,200,000').length).toBe(3)
    expect(screen.queryByText('8,120,000')).not.toBeInTheDocument()
    expect(screen.getByText(/Prices in IRR per gram\./)).toBeInTheDocument()
  })

  it('honours the Gregorian toggle for candle close times', async () => {
    window.localStorage.setItem('igp_calendar', 'gregorian')
    await renderView(
      <SettingsProvider>
        <TrendAlignmentTable />
      </SettingsProvider>,
      response('full_bullish', ALL_BULLISH)
    )
    expect(screen.getByText('2026-08-12 12:30')).toBeInTheDocument()
    expect(screen.queryByText('1405/05/21 12:30')).not.toBeInTheDocument()
  })

  it('formats XAU/USD in dollars, not toman', async () => {
    const payload = response('full_bearish', ALL_BEARISH, { symbol: 'XAUUSD' })
    for (const key of ['1d', '4h', '1h'] as TrendTimeframeKey[]) {
      payload.timeframes[key] = timeframe(key, 'bearish', {
        price: 3412.5,
        ma26: 3450.25,
        ma48: 3480,
        ma220: 3100.75
      })
    }
    await renderView(<TrendAlignmentTable symbol="XAUUSD" />, payload)

    expect(apiMock.mock.calls[0][0]).toBe('/market/trend-alignment?symbol=XAUUSD')
    expect(screen.getAllByText('$3,412.50').length).toBe(3)
    expect(screen.getAllByText('$3,450.25').length).toBe(3)
    expect(screen.getByText(/Prices in USD per troy ounce\./)).toBeInTheDocument()
    expect(screen.queryByText(/تومان/)).not.toBeInTheDocument()
  })

  it('keeps numeric cells inside the bidi-isolating classes', async () => {
    apiMock.mockResolvedValue(response('full_bullish', ALL_BULLISH))
    const { container } = render(<TrendAlignmentTable />)
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())

    const cells = Array.from(container.querySelectorAll('td')).filter((td) =>
      td.textContent?.includes('8,120,000')
    )
    expect(cells.length).toBeGreaterThan(0)
    for (const cell of cells) {
      expect(cell.className).toContain('num')
      expect(cell.className).toContain('mono')
    }
  })

  it('marks per-timeframe freshness with a word, not just a colour', async () => {
    const payload = response('not_aligned', { '1d': 'bullish', '4h': 'bullish', '1h': 'neutral' }, {
      data_fresh: false
    })
    payload.timeframes['1h'] = timeframe('1h', 'neutral', { data_fresh: false })
    await renderView(<TrendAlignmentTable />, payload)

    expect(screen.getAllByText('✓ FRESH')).toHaveLength(2)
    expect(screen.getByText('! STALE')).toBeInTheDocument()
    expect(screen.getByText('STALE')).toBeInTheDocument()
  })

  it('shows an em dash — never a made-up number — for a missing value', async () => {
    const payload = response('not_aligned', { '1d': 'bullish', '4h': 'bullish', '1h': 'bullish' })
    payload.timeframes['1h'] = timeframe('1h', 'unavailable', {
      price: null,
      ma26: null,
      ma48: null,
      ma220: null,
      candle_close_time: null,
      data_fresh: false,
      reason: 'no completed candles'
    })
    await renderView(<TrendAlignmentTable />, payload)

    const rows = screen.getAllByRole('row')
    const hourRow = rows[rows.length - 1]
    expect(hourRow.textContent).toContain('UNAVAILABLE')
    expect(hourRow.textContent).not.toMatch(/\d,\d{3}/)
  })

  it('surfaces a failed read with a retry instead of an empty table', async () => {
    apiMock.mockRejectedValue(new Error('trend service unavailable'))
    render(<TrendAlignmentTable />)
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())

    expect(screen.getByText('TREND READ UNAVAILABLE')).toBeInTheDocument()
    expect(screen.queryAllByRole('table')).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(apiMock).toHaveBeenCalledTimes(2))
  })

  it('says NEVER EVALUATED for a symbol with no stored state', async () => {
    await renderView(
      <TrendAlignmentTable />,
      response('not_aligned', ALL_BULLISH, {
        timeframes: {},
        calculated_at: null,
        previous_alignment: null,
        last_transition_at: null,
        note: 'never_evaluated'
      })
    )
    expect(screen.getByText('NEVER EVALUATED')).toBeInTheDocument()
    expect(screen.queryAllByRole('table')).toHaveLength(0)
  })
})

describe('the frontend never computes a moving average', () => {
  it('contains no smoothing, accumulation or period arithmetic', () => {
    for (const pattern of [
      /\.reduce\(/,
      /Math\.pow/,
      /Math\.exp/,
      /alpha/i,
      /smoothing/i,
      /multiplier/i,
      /\bfor\s*\(/,
      /\bwhile\s*\(/
    ]) {
      expect(trendSource).not.toMatch(pattern)
    }
  })

  it('never combines a price or a moving average with an operator', () => {
    expect(trendSource).not.toMatch(/(ma26|ma48|ma220|\.price)\s*[-+*/]\s*/)
    expect(trendSource).not.toMatch(/[-+*/]\s*\w*\.(ma26|ma48|ma220|price)\b/)
  })

  it('takes the MA periods from the payload rather than hard-coding them', () => {
    // 26 / 48 / 220 must never appear as literals: the column labels are built
    // from response.periods, so a server config change cannot make them lie.
    expect(trendSource).not.toMatch(/\b26\b/)
    expect(trendSource).not.toMatch(/\b48\b/)
    expect(trendSource).not.toMatch(/\b220\b/)
    expect(trendSource).toContain("maColumn(data, 'fast')")
    expect(trendSource).toContain('data.periods[key]')
  })

  it('renders titles as text, never as HTML', () => {
    expect(trendSource).not.toContain('dangerouslySetInnerHTML')
    expect(trendSource).not.toContain('innerHTML')
  })

  it('pairs every state with a distinct glyph and a word', () => {
    for (const glyph of ['▲', '▼', '●', '—', '◆']) {
      expect(trendSource).toContain(glyph)
    }
    for (const word of ['BULLISH', 'BEARISH', 'NEUTRAL', 'UNAVAILABLE', 'NOT ALIGNED']) {
      expect(trendSource).toContain(word)
    }
  })
})

describe('the pages actually render the components', () => {
  it('Overview imports and renders the compact card', () => {
    expect(overviewSource).toMatch(
      /import\s+\{\s*TrendAlignmentCard\s*\}\s+from\s+'\.\.\/components\/TrendAlignment'/
    )
    expect(overviewSource).toMatch(/<TrendAlignmentCard\s*\/>/)
  })

  it('Overview renders it unconditionally, not behind a data or role guard', () => {
    const line = overviewSource.split('\n').find((l: string) => l.includes('<TrendAlignmentCard'))
    expect(line).toBeDefined()
    expect(line).not.toMatch(/&&|\?|role|admin|isAdmin/)
  })

  it('Technical imports and renders the detailed table', () => {
    expect(technicalSource).toMatch(
      /import\s+\{\s*TrendAlignmentTable\s*\}\s+from\s+'\.\.\/components\/TrendAlignment'/
    )
    expect(technicalSource).toMatch(/<TrendAlignmentTable\s*\/>/)
  })

  it('Technical renders it unconditionally, not behind a data or role guard', () => {
    const line = technicalSource.split('\n').find((l: string) => l.includes('<TrendAlignmentTable'))
    expect(line).toBeDefined()
    expect(line).not.toMatch(/&&|\?|role|admin|isAdmin/)
  })

  it('inserts each view exactly once', () => {
    expect(overviewSource.match(/<TrendAlignmentCard/g) ?? []).toHaveLength(1)
    expect(technicalSource.match(/<TrendAlignmentTable/g) ?? []).toHaveLength(1)
  })
})
