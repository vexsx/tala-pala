import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Bourse, { BourseAgeNotice } from '../pages/Bourse'
import { SettingsProvider } from '../lib/settings'
import bourseSource from '../pages/Bourse.tsx?raw'
import {
  breadthWidths,
  closureRuns,
  formatTomanScaled,
  formatUsdScaled,
  jalaliYearStart,
  sortIndices
} from '../lib/bourse'
import type {
  BourseBreadthResponse,
  BourseFlowsResponse,
  BourseHistoryResponse,
  BourseIndicesResponse,
  BourseMarketValueResponse,
  BourseOverviewResponse
} from '../api/types'

// Captured from production on 2026-09-29 against fb1b204 — see
// fixtures/README.md. TEDPIX's year contains the 50-session closure from
// 2026-02-25 and the second-market index carries 31 values TSETMC stores off by
// a factor of ten: the two things this page exists to not misrepresent, and
// the reason these are captures and not hand-written.
import overviewJson from './fixtures/bourse-overview.json'
import indicesJson from './fixtures/bourse-indices.json'
import tedpix1y from './fixtures/bourse-history-tedpix-1y.json'
import tedpixReal from './fixtures/bourse-history-tedpix-real-max.json'
import tedpixUsd from './fixtures/bourse-history-tedpix-usd-1y.json'
import secondMarket3m from './fixtures/bourse-history-second-market-3m.json'
import breadth1y from './fixtures/bourse-breadth-1y.json'
import marketValue1y from './fixtures/bourse-market-value-1y.json'
import flowsJson from './fixtures/bourse-flows.json'

const OVERVIEW = overviewJson as BourseOverviewResponse
const INDICES = indicesJson as BourseIndicesResponse
const TEDPIX_1Y = tedpix1y as BourseHistoryResponse
const TEDPIX_REAL = tedpixReal as BourseHistoryResponse
const TEDPIX_USD = tedpixUsd as BourseHistoryResponse
const SECOND_3M = secondMarket3m as BourseHistoryResponse
const BREADTH = breadth1y as BourseBreadthResponse
const MV = marketValue1y as BourseMarketValueResponse
const FLOWS = flowsJson as BourseFlowsResponse

const SECOND_MARKET = '71704845530629737'
const REFINED = '12331083953323969'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, api: vi.fn() }
})
const { api } = await import('../api/client')
const apiMock = api as unknown as Mock

function route(path: string): Promise<unknown> {
  if (path === '/bourse/overview') return Promise.resolve(OVERVIEW)
  if (path.startsWith('/bourse/indices?')) return Promise.resolve(INDICES)
  if (path.startsWith(`/bourse/indices/${SECOND_MARKET}/history`)) return Promise.resolve(SECOND_3M)
  if (path.startsWith('/bourse/indices/')) {
    if (path.includes('unit=real')) return Promise.resolve(TEDPIX_REAL)
    if (path.includes('unit=usd')) return Promise.resolve(TEDPIX_USD)
    return Promise.resolve(TEDPIX_1Y)
  }
  if (path.startsWith('/bourse/breadth')) return Promise.resolve(BREADTH)
  if (path.startsWith('/bourse/market-value')) return Promise.resolve(MV)
  if (path.startsWith('/bourse/flows')) return Promise.resolve(FLOWS)
  return Promise.reject(new Error(`unexpected path ${path}`))
}

async function renderPage() {
  apiMock.mockImplementation(route)
  const result = render(
    <MemoryRouter>
      <SettingsProvider>
        <Bourse />
      </SettingsProvider>
    </MemoryRouter>
  )
  await screen.findByTestId('bx-unit-label')
  return result
}

beforeEach(() => {
  apiMock.mockReset()
  // scrollIntoView is absent from jsdom.
  Element.prototype.scrollIntoView = vi.fn()
})

// ---------------------------------------------------------------------------
// The calendar and the helpers
// ---------------------------------------------------------------------------

describe('the Tehran year', () => {
  it('starts on 1 Farvardin, in Tehran', () => {
    expect(jalaliYearStart(new Date('2026-09-29T08:00:00Z'))).toEqual({ iso: '2026-03-21', jy: 1405 })
    // 20:31 UTC on 20 March is already 1 Farvardin 1405 in Tehran (UTC+03:30).
    expect(jalaliYearStart(new Date('2026-03-20T20:31:00Z')).jy).toBe(1405)
    expect(jalaliYearStart(new Date('2026-03-20T20:00:00Z')).jy).toBe(1404)
  })
})

describe('a closed market is shaded, not read as calm', () => {
  it('finds the 2026 closure in TEDPIX', () => {
    expect(closureRuns(TEDPIX_1Y.points)).toEqual([{ from: '2026-02-25', to: '2026-05-18', sessions: 50 }])
  })

  it('ignores a short repeat', () => {
    const pts = [
      { date: '2026-01-01', value: 1 },
      { date: '2026-01-02', value: 1, unchanged: true },
      { date: '2026-01-03', value: 2 }
    ]
    expect(closureRuns(pts)).toEqual([])
    expect(closureRuns(pts, 1)).toEqual([{ from: '2026-01-01', to: '2026-01-02', sessions: 1 }])
  })
})

describe('big numbers at the scale the market speaks in', () => {
  it('states market value in trillion toman (همت) and dollars', () => {
    expect(formatTomanScaled(24812138915034710)).toBe('24,812T toman')
    expect(formatTomanScaled(3.8e13)).toBe('38T toman')
    expect(formatTomanScaled(1.4566e12)).toBe('1.5T toman')
    expect(formatTomanScaled(null)).toBe('—')
    expect(formatUsdScaled(100356085418.82)).toBe('$100.4B')
  })

  it('never lets a missing figure sort as the worst', () => {
    const items = INDICES.items.slice(0, 3).map((it, i) => ({
      ...it,
      returns: { ...it.returns, '1y': i === 1 ? null : it.returns['1y'] }
    }))
    for (const desc of [true, false]) {
      const sorted = sortIndices(items, '1y', desc)
      expect(sorted[sorted.length - 1].returns['1y']).toBeNull()
    }
  })

  it('splits breadth into widths that sum to 100', () => {
    const [a, b, c, d] = breadthWidths(OVERVIEW.sector_totals)
    expect(a + b + c + d).toBeCloseTo(100, 9)
  })
})

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

describe('the headline', () => {
  it('shows the settled close and the live snapshot, labelled as such', async () => {
    await renderPage()
    const tedpix = screen.getByTestId('bx-tile-32097828799138957')
    expect(tedpix.textContent).toContain('7,459,338')
    expect(tedpix.textContent).toContain('+2.55%')
    const tv = screen.getByTestId('bx-tile-trade-value')
    expect(tv.textContent).toContain('124T toman')
    expect(tv.textContent).toContain('Live figure as of')
  })
})

describe('the index chart', () => {
  it('names the unit before any number, and names the closure', async () => {
    await renderPage()
    expect(screen.getByTestId('bx-unit-label').textContent).toContain('Index points — a level, not a price')
    const closures = await screen.findByTestId('bx-closures')
    expect(closures.textContent).toContain('50 sessions')
    expect(closures.textContent).toContain('closed, not calm')
  })

  it('asks for constant prices and says the series is monthly', async () => {
    await renderPage()
    fireEvent.click(screen.getByRole('button', { name: 'Real (CPI)' }))
    await waitFor(() =>
      expect(apiMock.mock.calls.some(([p]) => String(p).includes('unit=real'))).toBe(true)
    )
    await waitFor(() => expect(screen.getByTestId('bx-unit-label').textContent).toContain('one point per month'))
    // A monthly series has no closure rows to shade.
    expect(screen.queryByTestId('bx-closures')).toBeNull()
  })

  it('draws the dollar view rebased, never as a level', async () => {
    await renderPage()
    fireEvent.click(screen.getByRole('button', { name: 'In dollars' }))
    await waitFor(() => expect(screen.getByTestId('bx-unit-label').textContent).toContain('rebased to 100'))
  })

  it('counts the corrected values of a decimal-shifted index', async () => {
    await renderPage()
    fireEvent.change(screen.getByTestId('bx-index-picker'), { target: { value: SECOND_MARKET } })
    await waitFor(() =>
      expect(screen.getByTestId('bx-index-chart').textContent).toContain(
        '31 value(s) in this window were stored by TSETMC off by a factor of ten'
      )
    )
  })
})

describe('the index tables', () => {
  it('marks every index that needed the factor-of-ten correction, and only those', async () => {
    await renderPage()
    const table = screen.getByTestId('bx-indices-table')
    const second = within(table).getByTestId(`bx-row-${SECOND_MARKET}`)
    expect(second.textContent).toContain('×10 corrected')
    const tedpix = within(table).getByTestId('bx-row-32097828799138957')
    expect(tedpix.textContent).not.toContain('×10 corrected')
  })

  it('links a sector to the roster shares in it', async () => {
    await renderPage()
    const row = within(screen.getByTestId('bx-sectors-table')).getByTestId(`bx-row-${REFINED}`)
    const links = within(row).getAllByRole('link')
    expect(links.map((a) => decodeURIComponent(a.getAttribute('href') ?? ''))).toEqual([
      '/stocks/شبندر',
      '/stocks/شتران',
      '/stocks/شپنا'
    ])
  })

  it('shows the year-to-date column from 1 Farvardin', async () => {
    await renderPage()
    const row = within(screen.getByTestId('bx-indices-table')).getByTestId('bx-row-32097828799138957')
    expect(row.textContent).toContain('+100.8%')
  })
})

describe('breadth', () => {
  it('says which side carried the year, with the spread in points', async () => {
    await renderPage()
    const s = await screen.findByTestId('bx-breadth-summary')
    expect(s.textContent).toContain('-30.3 pp')
    expect(s.textContent).toContain('the large companies carried it')
    expect(s.textContent).toContain('87 of 190 sessions')
  })

  it('draws the ratio on its own axis, not a second one', () => {
    // One axis per chart: the ratio is a separate small chart under the lines.
    // A second YAxis inside one chart would be the dual-axis anti-pattern.
    const breadthCard = bourseSource.slice(bourseSource.indexOf('function BreadthCard'), bourseSource.indexOf('// --- the index and sector tables'))
    expect(breadthCard).not.toContain('yAxisId')
  })
})

describe('market value and flow', () => {
  it('states size in toman and dollars, and says it is not a return', async () => {
    await renderPage()
    const latest = await screen.findByTestId('bx-mv-latest')
    expect(latest.textContent).toContain('24,812T toman')
    expect(latest.textContent).toContain('$100.4B')
    expect(screen.getByTestId('bx-market-value').textContent).toContain('It is not a return')
  })

  it('labels the roster total as the roster, not the market', async () => {
    await renderPage()
    const roster = await screen.findByTestId('bx-flows-roster')
    expect(roster.textContent).toContain('the roster, not the market')
    expect(roster.textContent).toContain('+1.5T')
  })
})

describe('the age of the data', () => {
  it('stays silent on fresh data', () => {
    const { container } = render(<BourseAgeNotice age={INDICES.data_age} calendar="gregorian" />)
    expect(container.textContent).toBe('')
  })

  it("repeats the API's own warning when the refresh has stopped", () => {
    const stale = {
      ...INDICES.data_age,
      stale: true,
      age_days: 15,
      warning: 'This market data is 15 days old.'
    }
    render(<BourseAgeNotice age={stale} calendar="gregorian" />)
    expect(screen.getByTestId('bx-data-age').textContent).toContain('This market data is 15 days old.')
  })
})
