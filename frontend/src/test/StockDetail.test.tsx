import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import StockDetail, { haltRuns, toChartPoints } from '../pages/StockDetail'
import { SettingsProvider } from '../lib/settings'
import detailSource from '../pages/StockDetail.tsx?raw'
import type { StockBarsResponse } from '../api/types'

// Captured from production GET /api/v1/stocks/{symbol}/bars on 2026-09-24 —
// see fixtures/README.md. فولاد's window is 43 halted sessions out of 66, a
// real two-month suspension, which is the whole reason these are captures.
import fooladAdjusted from './fixtures/stock-bars-foolad-adjusted.json'
import fooladRaw from './fixtures/stock-bars-foolad-raw.json'
import zoobAdjusted from './fixtures/stock-bars-zoob-adjusted.json'

const FOOLAD = fooladAdjusted as StockBarsResponse
const FOOLAD_RAW = fooladRaw as StockBarsResponse
const ZOOB = zoobAdjusted as StockBarsResponse

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, api: vi.fn() }
})
const { api } = await import('../api/client')

function mockApi(payload: StockBarsResponse) {
  ;(api as unknown as Mock).mockImplementation(() => Promise.resolve(payload))
}

async function renderDetail(payload: StockBarsResponse) {
  mockApi(payload)
  const result = render(
    <MemoryRouter initialEntries={[`/stocks/${encodeURIComponent(payload.symbol)}`]}>
      <SettingsProvider>
        <Routes>
          <Route path="/stocks/:symbol" element={<StockDetail />} />
        </Routes>
      </SettingsProvider>
    </MemoryRouter>
  )
  await screen.findByTestId('sd-basis')
  return result
}

beforeEach(() => {
  ;(api as unknown as Mock).mockReset()
})

// ---------------------------------------------------------------------------
// The halt. Everything else on this page is presentation; this is correctness.
// ---------------------------------------------------------------------------

describe('a halted session is not a flat day', () => {
  it('drops the price to null on every untraded session', () => {
    const points = toChartPoints(FOOLAD.items)
    const halted = FOOLAD.items.filter((b) => !b.traded)
    expect(halted.length).toBe(43)

    const nulls = points.filter((p) => p.price === null)
    expect(nulls.length).toBe(halted.length)

    // And crucially NOT the carried reference price, which is what sits in
    // `close` on a halted bar and is what a naive implementation would plot.
    for (const b of halted) {
      const point = points.find((p) => p.date === b.date)
      expect(point?.price).toBeNull()
    }
  })

  it('keeps the real prints', () => {
    const points = toChartPoints(FOOLAD.items)
    const traded = FOOLAD.items.filter((b) => b.traded)
    for (const b of traded.slice(-5)) {
      expect(points.find((p) => p.date === b.date)?.price).toBe(b.final_close)
    }
  })

  // connectNulls={true} would bridge the suspension with a straight line
  // between the last and next real print — a price path through days on which
  // nothing traded. A source assertion because no rendered output distinguishes
  // the two in jsdom, recharts drawing to an unmeasured SVG.
  it('never bridges the gap it just created', () => {
    expect(detailSource).toContain('connectNulls={false}')
    expect(detailSource).not.toContain('connectNulls={true}')
  })

  it('counts the suspension in prose and names its longest run', async () => {
    await renderDetail(FOOLAD)
    const halts = screen.getByTestId('sd-halts')
    expect(halts.textContent).toContain('43')
    expect(halts.textContent).toContain('66')
    expect(halts.textContent).toMatch(/no trade/i)
    // The longest run of فولاد's window, computed not hardcoded.
    const longest = haltRuns(FOOLAD.items)[0]
    expect(longest.sessions).toBeGreaterThan(30)
    expect(halts.textContent).toContain(String(longest.sessions))
  })

  it('says so plainly when nothing was halted', async () => {
    const allTraded: StockBarsResponse = {
      ...ZOOB,
      items: ZOOB.items.filter((b) => b.traded)
    }
    await renderDetail(allTraded)
    expect(screen.getByTestId('sd-halts').textContent).toMatch(/Every one of the/i)
  })
})

describe('haltRuns', () => {
  it('groups contiguous halts and sorts the longest first', () => {
    const runs = haltRuns(FOOLAD.items)
    expect(runs.length).toBeGreaterThan(0)
    for (let i = 1; i < runs.length; i++) {
      expect(runs[i - 1].sessions).toBeGreaterThanOrEqual(runs[i].sessions)
    }
    // Every run's session count is the number of untraded bars in [from, to].
    const total = runs.reduce((n, r) => n + r.sessions, 0)
    expect(total).toBe(FOOLAD.items.filter((b) => !b.traded).length)
  })

  it('returns nothing when every session traded', () => {
    expect(haltRuns(FOOLAD.items.filter((b) => b.traded))).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// The unit. This is the ONLY endpoint on the site serving rials.
// ---------------------------------------------------------------------------

describe('the currency is rial, and the page says so before any number', () => {
  it('names rials and flags that the rest of the site is toman', async () => {
    await renderDetail(FOOLAD)
    const basis = screen.getByTestId('sd-basis')
    expect(basis.textContent).toMatch(/rial/i)
    expect(basis.textContent).toMatch(/toman/i)
  })

  // A source assertion: the app-wide toman/rial display toggle multiplies by
  // ten, and applying it to a series already in rials would be wrong. This
  // page must not reach for it at all.
  it('does not apply the app-wide unit conversion', () => {
    expect(detailSource).not.toMatch(/\*\s*10\b|formatToman|displayUnit/)
  })
})

// ---------------------------------------------------------------------------
// Adjusted vs raw
// ---------------------------------------------------------------------------

describe('adjusted is the default and raw is labelled', () => {
  it('explains that an adjusted close is not a TSETMC screen price', async () => {
    await renderDetail(FOOLAD)
    const basis = screen.getByTestId('sd-basis')
    expect(basis.textContent).toMatch(/adjusted/i)
    expect(basis.textContent).toContain('31')
    expect(basis.textContent).toMatch(/capital increases/i)
  })

  it('warns that a raw series is not a price history', async () => {
    await renderDetail(FOOLAD_RAW)
    const basis = screen.getByTestId('sd-basis')
    expect(basis.textContent).toMatch(/\bRaw\b/)
    expect(basis.textContent).toMatch(/never happened|not a price history/i)
  })

  it('publishes the chain so the difference is checkable', async () => {
    await renderDetail(FOOLAD)
    expect(screen.getByText(/corporate-action chain/i)).toBeTruthy()
    expect(screen.getByText(/validated/)).toBeTruthy()
    // The worst surviving session return is the number that would betray a
    // missed action, so it must be on the page rather than only in the API.
    expect(screen.getByText(/Worst session return/i)).toBeTruthy()
  })
})

describe('an instrument with fewer actions still renders its chain', () => {
  it('shows ذوب with its 3 actions', async () => {
    await renderDetail(ZOOB)
    expect(screen.getByTestId('sd-basis').textContent).toContain('3')
    expect(screen.getByTestId('sd-halts').textContent).toContain('6')
  })
})

describe('a refused adjustment offers the raw series rather than a dead end', () => {
  it('renders a recovery action when the request fails', async () => {
    ;(api as unknown as Mock).mockImplementation(() =>
      Promise.reject(new Error('adjusted read refused for this instrument'))
    )
    render(
      <MemoryRouter initialEntries={['/stocks/%D9%81%D9%88%D9%84%D8%A7%D8%AF']}>
        <SettingsProvider>
          <Routes>
            <Route path="/stocks/:symbol" element={<StockDetail />} />
          </Routes>
        </SettingsProvider>
      </MemoryRouter>
    )
    await waitFor(() => expect(screen.getByText(/could not be loaded/i)).toBeTruthy())
    expect(screen.getByText(/Show the raw series instead/i)).toBeTruthy()
    expect(document.body.textContent).toMatch(/quietly-raw/i)
  })
})
