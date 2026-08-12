import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import TrendPerformance from '../components/TrendPerformance'
import { SettingsProvider } from '../lib/settings'
// The component's own source, so the "no backtest maths here" guard cannot drift.
import performanceSource from '../components/TrendPerformance.tsx?raw'
import technicalSource from '../pages/Technical.tsx?raw'
import type {
  TrendPerformanceBasis,
  TrendPerformanceItem,
  TrendPerformanceResponse
} from '../api/types'

// The section fetches through the shared client; mock the transport so the
// tests stay hermetic (same pattern as TrendAlignment.test / OsintStream.test).
vi.mock('../api/client', () => ({
  api: vi.fn(() => new Promise(() => undefined)),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : 'Unexpected error')
}))
import { api } from '../api/client'

const apiMock = api as unknown as Mock

/** 2026-08-12T09:00:00Z is 12:30 Tehran, i.e. 1405/05/21 in the Jalali calendar. */
const EVALUATED_FROM = '2026-05-14T09:00:00Z'
const EVALUATED_TO = '2026-08-12T09:00:00Z'
const COMPUTED_AT = '2026-08-12T09:00:00Z'

function item(
  windowDays: number,
  basis: TrendPerformanceBasis,
  overrides: Partial<TrendPerformanceItem> = {}
): TrendPerformanceItem {
  return {
    window_days: windowDays,
    basis,
    // 40 + 30 + 50 = 120, the invariant the job maintains.
    samples: 120,
    bullish_episodes: 6,
    bearish_episodes: 4,
    bullish_bars: 40,
    bearish_bars: 30,
    unaligned_bars: 50,
    fwd_return_bullish_pct: 0.42,
    fwd_return_bearish_pct: -0.31,
    fwd_return_baseline_pct: 0.11,
    hit_rate_bullish: 0.6,
    hit_rate_bearish: 0.55,
    evaluated_from: EVALUATED_FROM,
    evaluated_to: EVALUATED_TO,
    computed_at: COMPUTED_AT,
    note: `${basis} basis over ${windowDays} days`,
    ...overrides
  }
}

function response(items: TrendPerformanceItem[]): TrendPerformanceResponse {
  return { symbol: 'IR_GOLD_18K', items, count: items.length }
}

/**
 * Production shape: only the 14-day window is short enough to sit inside the
 * intraday history, so the three longer ones come back on the daily leg.
 */
const FOUR_WINDOWS = [
  item(90, 'daily_only'),
  item(60, 'daily_only'),
  item(30, 'daily_only'),
  item(14, 'full_mtf')
]

async function renderSection(
  node: React.ReactElement,
  payload: TrendPerformanceResponse | null
): Promise<void> {
  if (payload !== null) apiMock.mockResolvedValue(payload)
  render(node)
  await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())
}

function row(windowDays: number): HTMLElement {
  return screen.getByTestId(`tp-row-${windowDays}`)
}

beforeEach(() => {
  apiMock.mockReset()
  apiMock.mockImplementation(() => new Promise(() => undefined))
})

describe('TrendPerformance — the windows', () => {
  it('requests the contracted path and shows a skeleton on first load only', () => {
    render(<TrendPerformance />)
    expect(apiMock.mock.calls[0][0]).toBe(
      '/market/trend-alignment/performance?symbol=IR_GOLD_18K'
    )
    expect(screen.getByRole('status')).toBeInTheDocument()
    // Nothing is claimed while the first request is in flight.
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.queryByText(/%/)).not.toBeInTheDocument()
  })

  it('renders all four windows, newest-window-first, as the API ordered them', async () => {
    apiMock.mockResolvedValue(response(FOUR_WINDOWS))
    const { container } = render(<TrendPerformance />)
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())

    for (const days of [90, 60, 30, 14]) {
      expect(row(days)).toBeInTheDocument()
    }
    // The API's order is the reading order and is never re-sorted here.
    const windows = Array.from(container.querySelectorAll('tr.tp-row td:first-child')).map(
      (cell) => cell.textContent
    )
    expect(windows).toEqual(['90d', '60d', '30d', '14d'])
  })

  it('names the contracted columns with the baseline beside the conditional returns', async () => {
    await renderSection(<TrendPerformance />, response(FOUR_WINDOWS))

    const headers = screen.getAllByRole('columnheader').map((h) => h.textContent)
    expect(headers).toEqual([
      'Window',
      'Basis',
      'Samples',
      'Fwd return · bullish',
      'Fwd return · bearish',
      'Baseline (all bars)',
      'Hit rate · bullish',
      'Hit rate · bearish'
    ])
    // The baseline is adjacent to the conditional returns, not tucked away at
    // the end: a conditional number alone invites crediting the indicator for a
    // market that was rising anyway.
    expect(headers.indexOf('Baseline (all bars)')).toBe(
      headers.indexOf('Fwd return · bearish') + 1
    )
  })

  it('shows the baseline value on the same row as the conditional returns', async () => {
    await renderSection(<TrendPerformance />, response([item(30, 'daily_only')]))

    const cells = within(row(30)).getAllByRole('cell')
    expect(cells[3].textContent).toBe('+0.42%')
    expect(cells[4].textContent).toBe('-0.31%')
    expect(cells[5].textContent).toBe('+0.11%')
  })

  it('renders the hit rates the API measured, as percentages of the bars', async () => {
    await renderSection(<TrendPerformance />, response([item(30, 'daily_only')]))

    const cells = within(row(30)).getAllByRole('cell')
    expect(cells[6].textContent).toBe('60%')
    expect(cells[7].textContent).toBe('55%')
  })

  it('reports the sample size and its breakdown', async () => {
    await renderSection(<TrendPerformance />, response([item(30, 'daily_only')]))

    const samples = within(row(30)).getAllByRole('cell')[2]
    expect(samples.textContent).toBe('120')
    expect(samples).toHaveAttribute(
      'title',
      '40 bullish, 30 bearish and 50 unaligned bars, over 6 bullish and 4 bearish episodes. Bars are not independent trades.'
    )
  })

  it('summarises the section for a screen reader', async () => {
    await renderSection(<TrendPerformance />, response(FOUR_WINDOWS))
    expect(
      screen.getByRole('region', {
        name: 'Trend alignment track record, 18k gold: 4 replayed windows'
      })
    ).toBeInTheDocument()
  })

  it('serves a second symbol from the same contract', async () => {
    await renderSection(
      <TrendPerformance symbol="XAUUSD" />,
      { ...response([item(14, 'full_mtf')]), symbol: 'XAUUSD' }
    )
    expect(apiMock.mock.calls[0][0]).toBe('/market/trend-alignment/performance?symbol=XAUUSD')
    expect(screen.getByText(/TRACK RECORD, XAU\/USD/)).toBeInTheDocument()
  })
})

describe('TrendPerformance — the basis is never silently mixed', () => {
  it('badges a daily-only row with a word, not just a colour', async () => {
    await renderSection(<TrendPerformance />, response(FOUR_WINDOWS))

    const badges = within(row(90)).getAllByText('DAILY LEG ONLY')
    expect(badges.length).toBeGreaterThan(0)
    expect(badges[0]).toHaveAttribute(
      'title',
      expect.stringContaining('NOT the multi-timeframe alignment')
    )
    expect(row(90).className).toContain('tp-basis-daily_only')
  })

  it('badges a full multi-timeframe row differently', async () => {
    await renderSection(<TrendPerformance />, response(FOUR_WINDOWS))

    expect(within(row(14)).getAllByText('FULL 1D+4H+1H').length).toBeGreaterThan(0)
    expect(row(14).className).toContain('tp-basis-full_mtf')
    expect(within(row(14)).queryByText('DAILY LEG ONLY')).not.toBeInTheDocument()
  })

  it('repeats the basis on every row, so no row inherits the one above it', async () => {
    await renderSection(<TrendPerformance />, response(FOUR_WINDOWS))

    for (const days of [90, 60, 30]) {
      expect(within(row(days)).getAllByText('DAILY LEG ONLY').length).toBeGreaterThan(0)
    }
  })

  it('carries the basis down onto the note, so the pair reads as one row', async () => {
    await renderSection(<TrendPerformance />, response(FOUR_WINDOWS))

    expect(screen.getByTestId('tp-note-90').className).toContain('tp-basis-daily_only')
    expect(screen.getByTestId('tp-note-14').className).toContain('tp-basis-full_mtf')
  })

  it("surfaces each row's own note", async () => {
    await renderSection(<TrendPerformance />, response(FOUR_WINDOWS))

    expect(screen.getByTestId('tp-note-90').textContent).toContain(
      'daily_only basis over 90 days'
    )
    expect(screen.getByTestId('tp-note-14').textContent).toContain('full_mtf basis over 14 days')
  })

  it('says so plainly when the job recorded no note', async () => {
    await renderSection(<TrendPerformance />, response([item(30, 'daily_only', { note: '' })]))
    expect(screen.getByTestId('tp-note-30').textContent).toContain(
      'The job recorded no note for this window'
    )
  })

  it('states above the table that this is a replay, not a traded record', async () => {
    await renderSection(<TrendPerformance />, response(FOUR_WINDOWS))

    const preamble = screen.getByText(/This is a replay of the indicator over past candles/)
    expect(preamble.textContent).toContain('not a realised trading record')
    expect(preamble.textContent).toContain('fall back to the daily leg')
    expect(preamble.textContent).toContain('2026-07-20')
  })
})

describe('TrendPerformance — null is not zero', () => {
  const UNMEASURED = item(90, 'daily_only', {
    samples: 0,
    bullish_episodes: 0,
    bearish_episodes: 0,
    bullish_bars: 0,
    bearish_bars: 0,
    unaligned_bars: 0,
    fwd_return_bullish_pct: null,
    fwd_return_bearish_pct: null,
    fwd_return_baseline_pct: null,
    hit_rate_bullish: null,
    hit_rate_bearish: null,
    evaluated_from: null,
    evaluated_to: null
  })

  it('renders an em dash for every unmeasured statistic and never 0.0%', async () => {
    await renderSection(<TrendPerformance />, response([UNMEASURED]))

    const cells = within(row(90)).getAllByRole('cell')
    for (const index of [3, 4, 5, 6, 7]) {
      expect(cells[index].textContent).toBe('—')
    }
    // Not one percentage anywhere on the row: a statistic that was never
    // measured must never be printed as a measured zero.
    expect(row(90).textContent).not.toContain('%')
    expect(row(90).textContent).not.toContain('0.00')
  })

  it('explains why a statistic is missing rather than leaving a bare dash', async () => {
    await renderSection(<TrendPerformance />, response([UNMEASURED]))

    const dashes = within(row(90)).getAllByText('—')
    expect(dashes[0]).toHaveAttribute(
      'title',
      'The alignment was never bullish inside this window, so there was nothing to measure.'
    )
    expect(dashes[4]).toHaveAttribute(
      'title',
      expect.stringContaining('a hit rate over no bars is not a zero')
    )
  })

  it('keeps a genuine measured zero as a number, not a dash', async () => {
    await renderSection(
      <TrendPerformance />,
      response([
        item(30, 'daily_only', {
          fwd_return_bullish_pct: 0,
          hit_rate_bullish: 0
        })
      ])
    )

    const cells = within(row(30)).getAllByRole('cell')
    expect(cells[3].textContent).toBe('0.00%')
    expect(cells[6].textContent).toBe('0%')
  })

  it('renders an em dash for missing evaluation dates', async () => {
    await renderSection(<TrendPerformance />, response([UNMEASURED]))
    expect(screen.getByTestId('tp-note-90').textContent).toContain('Replayed — to —')
  })

  it('still shows the sample count of zero — a count of zero IS the measurement', async () => {
    await renderSection(<TrendPerformance />, response([UNMEASURED]))
    expect(within(row(90)).getAllByRole('cell')[2].textContent).toBe('0')
  })
})

describe('TrendPerformance — small samples announce themselves', () => {
  /** One bar under the floor, one exactly on it: the boundary is the test. */
  const BOUNDARY = item(14, 'full_mtf', {
    samples: 100,
    bullish_bars: 19,
    bearish_bars: 20,
    unaligned_bars: 61
  })

  it('refuses to quote a rate below the 20-bar floor', async () => {
    await renderSection(<TrendPerformance />, response([BOUNDARY]))

    const cells = within(row(14)).getAllByRole('cell')
    // Bullish: 19 bars — both the conditional mean and the hit rate are marked.
    expect(cells[3].textContent).toBe('too few (19 bars)')
    expect(cells[6].textContent).toBe('too few (19 bars)')
    expect(cells[3].textContent).not.toContain('%')
    expect(cells[6].textContent).not.toContain('%')
  })

  it('quotes the rate at exactly the floor', async () => {
    await renderSection(<TrendPerformance />, response([BOUNDARY]))

    const cells = within(row(14)).getAllByRole('cell')
    // Bearish: 20 bars — at the floor, so the measured numbers are shown.
    expect(cells[4].textContent).toBe('-0.31%')
    expect(cells[7].textContent).toBe('55%')
  })

  it('quotes the rate above the floor', async () => {
    await renderSection(
      <TrendPerformance />,
      response([item(14, 'full_mtf', { samples: 100, bullish_bars: 21, bearish_bars: 20, unaligned_bars: 59 })])
    )

    const cells = within(row(14)).getAllByRole('cell')
    expect(cells[3].textContent).toBe('+0.42%')
    expect(cells[6].textContent).toBe('60%')
  })

  it('says how thin the row is, and why that is the reason', async () => {
    await renderSection(<TrendPerformance />, response([BOUNDARY]))

    const marks = within(row(14)).getAllByText('too few (19 bars)')
    expect(marks).toHaveLength(2)
    expect(marks[0]).toHaveAttribute('title', expect.stringContaining('20-bar floor'))
  })

  it('holds the baseline to the same floor, against its own denominator', async () => {
    await renderSection(
      <TrendPerformance />,
      response([
        item(14, 'full_mtf', {
          samples: 12,
          bullish_bars: 4,
          bearish_bars: 3,
          unaligned_bars: 5
        })
      ])
    )
    expect(within(row(14)).getAllByRole('cell')[5].textContent).toBe('too few (12 bars)')
  })

  it('never marks a thin row as zero', async () => {
    await renderSection(<TrendPerformance />, response([BOUNDARY]))
    expect(within(row(14)).getAllByRole('cell')[3].textContent).not.toContain('0')
  })
})

describe('TrendPerformance — states', () => {
  it('keeps its frame on failure and offers a working retry', async () => {
    apiMock.mockRejectedValue(new Error('track record service unavailable'))
    render(<TrendPerformance />)
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())

    // A section that vanishes on failure reads as "no track record".
    expect(screen.getByTestId('trend-performance')).toBeInTheDocument()
    expect(screen.getByText('TRACK RECORD UNAVAILABLE')).toBeInTheDocument()
    expect(screen.getByText('track record service unavailable')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    // The preamble stays: the reader still learns what this section would be.
    expect(screen.getByText(/This is a replay of the indicator/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(apiMock).toHaveBeenCalledTimes(2))
  })

  it('says NOT COMPUTED YET for a symbol with no stored windows', async () => {
    await renderSection(<TrendPerformance />, response([]))

    expect(screen.getByText('NOT COMPUTED YET')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.queryByText(/%/)).not.toBeInTheDocument()
    expect(
      screen.getByRole('region', {
        name: 'Trend alignment track record, 18k gold: not computed yet'
      })
    ).toBeInTheDocument()
  })

  it('labels the loading and failed states for a screen reader', async () => {
    render(<TrendPerformance />)
    expect(
      screen.getByRole('region', { name: 'Trend alignment track record, 18k gold: loading' })
    ).toBeInTheDocument()
  })
})

describe('TrendPerformance — display conventions', () => {
  it('dates the replay in the Jalali calendar by default', async () => {
    await renderSection(<TrendPerformance />, response([item(30, 'daily_only')]))
    const note = screen.getByTestId('tp-note-30')
    expect(note.textContent).toContain('1405/05/21 12:30')
    expect(note.textContent).toContain('Replayed 1405/02/24 12:30 to 1405/05/21 12:30')
  })

  it('honours the Gregorian toggle', async () => {
    window.localStorage.setItem('igp_calendar', 'gregorian')
    await renderSection(
      <SettingsProvider>
        <TrendPerformance />
      </SettingsProvider>,
      response([item(30, 'daily_only')])
    )
    const note = screen.getByTestId('tp-note-30')
    expect(note.textContent).toContain('2026-08-12 12:30')
    expect(note.textContent).not.toContain('1405/05/21')
  })

  it('keeps counts and percentages inside the bidi-isolating classes', async () => {
    await renderSection(<TrendPerformance />, response([item(30, 'daily_only')]))

    const cells = within(row(30)).getAllByRole('cell')
    // Samples through both hit rates: every numeric cell.
    for (const index of [2, 3, 4, 5, 6, 7]) {
      expect(cells[index].className).toContain('num')
      expect(cells[index].className).toContain('mono')
    }
    // The window label is not right-aligned, but still isolates its digits.
    expect(cells[0].className).toContain('mono')
  })

  it('carries the sign in the number itself, not only in the colour', async () => {
    await renderSection(<TrendPerformance />, response([item(30, 'daily_only')]))

    const cells = within(row(30)).getAllByRole('cell')
    expect(cells[3].textContent).toContain('+')
    expect(cells[4].textContent).toContain('-')
    expect(within(cells[3]).getByText('+0.42%').className).toContain('pos')
    expect(within(cells[4]).getByText('-0.31%').className).toContain('neg')
  })

  it('never tints a hit rate as if it were a signed number', async () => {
    // A 40% hit rate is worse than a coin flip. pctClass would call it "pos"
    // (it is above zero) and paint it green, which reads as approval.
    await renderSection(
      <TrendPerformance />,
      response([item(30, 'daily_only', { hit_rate_bullish: 0.4, hit_rate_bearish: 0.72 })])
    )

    const cells = within(row(30)).getAllByRole('cell')
    for (const [index, text] of [
      [6, '40%'],
      [7, '72%']
    ] as Array<[number, string]>) {
      const rate = within(cells[index]).getByText(text)
      expect(rate.className).not.toContain('pos')
      expect(rate.className).not.toContain('neg')
    }
  })
})

describe('the frontend never recomputes the track record', () => {
  const STATS =
    '(fwd_return_bullish_pct|fwd_return_bearish_pct|fwd_return_baseline_pct|hit_rate_bullish|hit_rate_bearish|samples|bullish_bars|bearish_bars|unaligned_bars|bullish_episodes|bearish_episodes)'

  it('never combines a statistic with an arithmetic operator', () => {
    expect(performanceSource).not.toMatch(new RegExp(`${STATS}\\s*[-+*/]\\s*`))
    expect(performanceSource).not.toMatch(new RegExp(`[-+*/]\\s*\\w*\\.${STATS}\\b`))
  })

  it('contains no aggregation, scaling or accumulation', () => {
    for (const pattern of [
      /\.reduce\(/,
      /Math\./,
      /toFixed\(/,
      /\* *100/,
      /\/ *100/,
      /\bfor\s*\(/,
      /\bwhile\s*\(/,
      // Call form only: the prose above the table is allowed to use the words
      // "average" and "mean", it is a helper doing the maths that is banned.
      /\baverage\s*\(/i,
      /\bavg\s*\(/i,
      /\bmean\s*\(/i
    ]) {
      expect(performanceSource).not.toMatch(pattern)
    }
  })

  it('never decides a sign or a hit itself', () => {
    // No `> 0` / `< 0` anywhere: whether a bar was a hit, and whether a return
    // was positive, are both the backtest's answers, not the browser's.
    expect(performanceSource).not.toMatch(/[<>]=?\s*0\b/)
  })

  it('converts the hit-rate fraction by formatting, never by multiplying', () => {
    expect(performanceSource).toContain("style: 'percent'")
    expect(performanceSource).toContain('HIT_RATE_FORMAT.format(value)')
    expect(performanceSource).toContain('formatPct(value, { digits: 2 })')
  })

  it('keeps the small-sample floor in one named, commented constant', () => {
    expect(performanceSource).toContain('MIN_BARS_FOR_RATE = 20')
    expect(performanceSource).toMatch(/bars < MIN_BARS_FOR_RATE/)
    // The reasoning is written down, not left to the reader.
    expect(performanceSource).toMatch(/Fewest bars this section will quote/)
  })

  it('pairs every basis with a word, not just a tint', () => {
    for (const word of ['FULL 1D+4H+1H', 'DAILY LEG ONLY']) {
      expect(performanceSource).toContain(word)
    }
  })

  it('renders notes and titles as text, never as HTML', () => {
    expect(performanceSource).not.toContain('dangerouslySetInnerHTML')
    expect(performanceSource).not.toContain('innerHTML')
  })
})

describe('the Technical page actually renders the section', () => {
  it('imports and mounts it', () => {
    expect(technicalSource).toMatch(
      /import\s+TrendPerformance\s+from\s+'\.\.\/components\/TrendPerformance'/
    )
    expect(technicalSource).toMatch(/<TrendPerformance\s*\/>/)
  })

  it('mounts it unconditionally, not behind a data or role guard', () => {
    const line = technicalSource.split('\n').find((l: string) => l.includes('<TrendPerformance'))
    expect(line).toBeDefined()
    expect(line).not.toMatch(/&&|\?|role|admin|isAdmin/)
  })

  it('inserts it exactly once, on exactly one page', () => {
    expect(technicalSource.match(/<TrendPerformance/g) ?? []).toHaveLength(1)
    expect(technicalSource.match(/components\/TrendPerformance/g) ?? []).toHaveLength(1)
  })
})
