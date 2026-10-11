import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Liquidity from '../pages/Liquidity'
import { SettingsProvider } from '../lib/settings'
import liquiditySource from '../pages/Liquidity.tsx?raw'
import liquidityLibSource from '../lib/liquidity.ts?raw'
import appSource from '../App.tsx?raw'
import type { LiquidityAggregatesResponse, LiquidityDollarResponse } from '../api/types'

// HAND-BUILT from the research data, pending a production capture — see
// fixtures/README.md ("Money & liquidity"). The endpoints were not deployed
// when this page was written. Every number is CBI's or TGJU's as the research
// read it (Esfand 1404 liquidity 155,812.2, the 265,100-toman close of
// 1405/07/18, the backtest's errors and band quantiles); the API's arithmetic
// is redone by the generator kept in that README; the prose is its own.
import monthlyJson from './fixtures/liquidity-aggregates-monthly.json'
import annualJson from './fixtures/liquidity-aggregates-annual.json'
import emptyJson from './fixtures/liquidity-aggregates-not-ingested.json'
import dollar12Json from './fixtures/liquidity-dollar-12.json'
import dollar24Json from './fixtures/liquidity-dollar-24.json'
import dollarEmptyJson from './fixtures/liquidity-dollar-not-ingested.json'

const MONTHLY = monthlyJson as LiquidityAggregatesResponse
const ANNUAL = annualJson as LiquidityAggregatesResponse
const EMPTY = emptyJson as LiquidityAggregatesResponse
const D12 = dollar12Json as unknown as LiquidityDollarResponse
const D24 = dollar24Json as unknown as LiquidityDollarResponse
const D_EMPTY = dollarEmptyJson as unknown as LiquidityDollarResponse

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, api: vi.fn() }
})
const { api } = await import('../api/client')
const apiMock = api as unknown as Mock

interface Payloads {
  monthly?: LiquidityAggregatesResponse | Error
  annual?: LiquidityAggregatesResponse
  d12?: LiquidityDollarResponse | Error
  d24?: LiquidityDollarResponse
}

function route(p: Payloads) {
  const reply = (v: unknown) => (v instanceof Error ? Promise.reject(v) : Promise.resolve(v))
  return (path: string): Promise<unknown> => {
    if (path === '/liquidity/aggregates?freq=monthly') return reply(p.monthly ?? MONTHLY)
    if (path === '/liquidity/aggregates?freq=annual') return reply(p.annual ?? ANNUAL)
    if (path === '/liquidity/dollar?horizon=12') return reply(p.d12 ?? D12)
    if (path === '/liquidity/dollar?horizon=24') return reply(p.d24 ?? D24)
    return Promise.reject(new Error(`unexpected path ${path}`))
  }
}

async function renderPage(p: Payloads = {}, ready = 'lq-table') {
  apiMock.mockImplementation(route(p))
  const result = render(
    <MemoryRouter>
      <SettingsProvider>
        <Liquidity />
      </SettingsProvider>
    </MemoryRouter>
  )
  await screen.findByTestId(ready)
  await screen.findByTestId('lq-dollar')
  return result
}

/** The month rows of the data table, top to bottom. */
function tableMonths(): string[] {
  return within(screen.getByTestId('lq-table'))
    .getAllByRole('row')
    .map((r) => r.getAttribute('data-testid'))
    .filter((id): id is string => id !== null && id.startsWith('lq-row-'))
    .map((id) => id.replace('lq-row-', ''))
}

/** Opens the data table past its first 24 rows. */
function showAllRows() {
  const more = screen.getByTestId('lq-more')
  expect(more).toHaveAttribute('aria-expanded', 'false')
  fireEvent.click(more)
  expect(more).toHaveAttribute('aria-expanded', 'true')
}

/** What a sighted reader sees in an element: its text without the screen-reader-only parts. */
function visible(el: Element): string {
  const copy = el.cloneNode(true) as Element
  copy.querySelectorAll('.sr-only').forEach((n) => n.remove())
  return copy.textContent ?? ''
}

function cellsOf(testId: string): string[] {
  return within(screen.getByTestId(testId))
    .getAllByRole('cell')
    .map(visible)
}

/**
 * The base-money change listed as ONE break at 1399-12, as Track M's API
 * lists it, instead of one entry per switch as the fixtures do. What the page
 * says about the change must not depend on which.
 */
const ONE_BREAK = [
  {
    code: 'CBI_BASE_MONEY',
    from: '1399-12',
    note: 'CBI redefined base money from Esfand 1399.',
    old_method_after: Array.from({ length: 11 }, (_, i) => `1400-${String(i + 1).padStart(2, '0')}`)
  }
]

beforeEach(() => {
  apiMock.mockReset()
})

// ---------------------------------------------------------------------------
// How old the money figures are
// ---------------------------------------------------------------------------

describe('the as-of line', () => {
  it('names the newest CBI month, its workbook date, the lag and the age', async () => {
    await renderPage()
    const line = screen.getByTestId('lq-as-of').textContent ?? ''
    expect(line).toContain('Newest CBI month: Esfand 1404 (اسفند ۱۴۰۴, ended 1404/12/29).')
    // The workbook's own saved time: CBI states no release date.
    expect(line).toContain('Its workbook was saved 1405/03/31, 93 days after the month ended')
    expect(line).toContain('CBI states no release date')
    expect(line).toContain('the newest month is 205 days old')
    expect(within(screen.getByTestId('lq-as-of')).getByRole('link', { name: 'CBI’s workbook' })).toHaveAttribute(
      'href',
      'https://www.cbi.ir/page/35570.aspx'
    )
  })

  it('follows the calendar setting for dates, and keeps CBI’s months Jalali', async () => {
    window.localStorage.setItem('igp_calendar', 'gregorian')
    await renderPage()
    const line = screen.getByTestId('lq-as-of').textContent ?? ''
    expect(line).toContain('Esfand 1404')
    expect(line).toContain('ended 2026-03-20')
    expect(line).toContain('saved 2026-06-21')
    expect(screen.getByTestId('lq-row-1404-12').textContent).toContain('ended 2026-03-20')
  })

  it('says so when CBI gave no date at all', async () => {
    await renderPage({ monthly: { ...MONTHLY, latest: { ...MONTHLY.latest!, published_at: null } } })
    const line = screen.getByTestId('lq-as-of').textContent ?? ''
    expect(line).toContain('CBI states no release date for it.')
    expect(line).not.toContain('days after the month ended')
  })

  it('warns when the newest month is older than CBI has ever taken', async () => {
    await renderPage()
    const notice = screen.getByTestId('lq-stale').textContent ?? ''
    expect(notice).toContain('Nothing newer than Esfand 1404 is stored: the newest money figures are 205 days old.')
    expect(notice).toContain('10 to 137 days after the month they describe')
    expect(notice).toContain('describes Esfand 1404, not today')
  })

  it('judges by its own bound when the server gives no verdict, and stays quiet for a fresh month', async () => {
    await renderPage({ monthly: { ...MONTHLY, data_age: {} } })
    expect(screen.getByTestId('lq-stale')).toBeInTheDocument()
  })

  it('stays quiet for a month inside CBI’s usual lag', async () => {
    const fresh = {
      ...MONTHLY,
      latest: { ...MONTHLY.latest!, age_days: 75 },
      data_age: { ...MONTHLY.data_age, stale: false, age_days: 75 }
    }
    await renderPage({ monthly: fresh })
    expect(screen.queryByTestId('lq-stale')).toBeNull()
    expect(screen.getByTestId('lq-as-of').textContent).toContain('the newest month is 75 days old')
  })
})

// ---------------------------------------------------------------------------
// Monthly and annual
// ---------------------------------------------------------------------------

describe('monthly and annual', () => {
  it('opens on ten years of months, newest first', async () => {
    await renderPage()
    expect(within(screen.getByTestId('lq-freq')).getByRole('button', { name: 'Monthly' })).toHaveAttribute(
      'aria-pressed',
      'true'
    )
    expect(within(screen.getByTestId('lq-window')).getByRole('button', { name: '10 years' })).toHaveAttribute(
      'aria-pressed',
      'true'
    )
    // Two years of months first, the rest one click away.
    expect(tableMonths()).toHaveLength(24)
    expect(screen.getByTestId('lq-more').textContent).toBe('Show all 102')
    showAllRows()
    const months = tableMonths()
    // Nine quarter-ends (1395-03 → 1397-03) and 93 months (1397-04 → 1404-12).
    expect(months).toHaveLength(102)
    expect(months[0]).toBe('1404-12')
    // Ten whole Jalali years back from Esfand 1404: from Farvardin 1395.
    expect(months[months.length - 1]).toBe('1395-03')
    expect(months).not.toContain('1394-12')
    // The quarter-end history says what it is.
    expect(screen.getByTestId('lq-row-1396-06').textContent).toContain('quarter-end')
    expect(screen.getByTestId('lq-row-1404-11').textContent).not.toContain('quarter-end')
  })

  it('shows the whole history on request', async () => {
    await renderPage()
    fireEvent.click(within(screen.getByTestId('lq-window')).getByRole('button', { name: 'All (from 1352)' }))
    showAllRows()
    const months = tableMonths()
    expect(months[months.length - 1]).toBe('1352-03')
    expect(screen.getByTestId('lq-levels-unit').textContent).toContain(
      'Before Tir 1397 only some months are stored — CBI’s quarter-ends — and the line joins them point to point.'
    )
  })

  it('names the month the monthly figures start from the data, not from a fixed date', async () => {
    // As the CBI ingest stores it: the monthly workbooks from Tir 1398, Esfand
    // 1397 alone among quarter-ends before it.
    const keep = (p: { period: string }) =>
      p.period >= '1398-04' || ['1397-06', '1397-09', '1397-12', '1398-03'].includes(p.period) || p.period < '1397-04'
    const asQ = <T extends { period: string }>(p: T): T =>
      p.period < '1398-04' && p.period !== '1397-12' ? { ...p, frequency: 'Q' } : p
    const ingest = { ...MONTHLY, series: MONTHLY.series.map((s) => ({ ...s, points: s.points.filter(keep).map(asQ) })) }
    await renderPage({ monthly: ingest })
    fireEvent.click(within(screen.getByTestId('lq-window')).getByRole('button', { name: 'All (from 1352)' }))
    const caption = screen.getByTestId('lq-levels-unit').textContent ?? ''
    expect(caption).toContain('Before Tir 1398 only some months are stored')
    expect(caption).not.toContain('1397')
    expect(screen.getByTestId('lq-notes').textContent).toContain('month by month from Tir 1398')
  })

  it('switches to Jalali years with Esfand-over-Esfand growth', async () => {
    await renderPage()
    fireEvent.click(within(screen.getByTestId('lq-freq')).getByRole('button', { name: 'Annual' }))
    await screen.findByTestId('lq-year-1404')
    expect(apiMock).toHaveBeenCalledWith('/liquidity/aggregates?freq=annual', expect.anything())
    const y1404 = cellsOf('lq-year-1404')
    expect(y1404).toEqual(['1404', '15,581', '+53.3%', '12,685', '+39.7%', '2,196', '+61.5%', '1,692', '+42.3%', '12'])
    // The growth chart's heading says which growth it is.
    expect(screen.getByTestId('lq-growth').textContent).toContain('Growth — Esfand over Esfand')
    fireEvent.click(within(screen.getByTestId('lq-measure')).getByRole('button', { name: 'Yearly average' }))
    expect(screen.getByTestId('lq-growth').textContent).toContain('Growth — yearly average over yearly average')
    // A year with only part of its months has no yearly average.
    fireEvent.click(within(screen.getByTestId('lq-window')).getByRole('button', { name: 'All (from 1352)' }))
    const y1397 = screen.getByTestId('lq-year-1397')
    expect(y1397.textContent).toContain('partial year')
    expect(cellsOf('lq-year-1397')[3]).toBe('—')
  })

  it('sorts from the keyboard: every sortable heading is a button, missing figures last', async () => {
    await renderPage()
    const table = screen.getByTestId('lq-table')
    const sortable = within(table)
      .getAllByRole('columnheader')
      .filter((th) => th.hasAttribute('aria-sort'))
    expect(sortable).toHaveLength(10)
    for (const th of sortable) expect(within(th).getByRole('button')).toHaveAttribute('type', 'button')

    // Base money y/y, largest first: Esfand 1404's 61.5%, and the months a
    // method change leaves unmeasured at the bottom.
    showAllRows()
    const baseYoy = within(table).getAllByRole('button', { name: 'y/y' })[1]
    fireEvent.click(baseYoy)
    expect(baseYoy.closest('th')).toHaveAttribute('aria-sort', 'descending')
    const months = tableMonths()
    expect(months[0]).toBe('1404-12')
    expect(months[months.length - 1]).toMatch(/^(1399-12|1401-(0\d|1[01]))$/)
    fireEvent.click(baseYoy)
    expect(baseYoy.closest('th')).toHaveAttribute('aria-sort', 'ascending')
    expect(tableMonths().slice(-1)[0]).toMatch(/^(1399-12|1401-(0\d|1[01]))$/)
  })

  it('labels every control group and states each toggle', async () => {
    await renderPage()
    for (const name of ['Frequency', 'Years shown', 'Months ahead', 'History shown']) {
      const group = screen.getByRole('group', { name })
      for (const b of within(group).getAllByRole('button')) expect(b).toHaveAttribute('aria-pressed')
    }
    expect(screen.getByTestId('lq-log-toggle')).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(screen.getByTestId('lq-log-toggle'))
    expect(screen.getByTestId('lq-log-toggle')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByTestId('lq-levels-unit').textContent).toContain('on a log scale equal heights are equal percentage growth')
  })
})

// ---------------------------------------------------------------------------
// Units
// ---------------------------------------------------------------------------

describe('units', () => {
  it('shows همت with the app on toman, and says so', async () => {
    await renderPage()
    expect(screen.getByTestId('lq-unit-line').textContent).toBe(
      'Values in همت — trillion toman (CBI’s thousand billion rial ÷ 10).'
    )
    const esfand = cellsOf('lq-row-1404-12')
    expect(esfand[1]).toBe('15,581')
    expect(esfand[5]).toBe('2,196')
    // The multiplier as CBI prints it: 155,812.2 ÷ 21,959.3 = 7.095.
    expect(esfand[10]).toBe('7.095')
    expect(within(screen.getByTestId('lq-table')).getAllByRole('button', { name: 'همت' })).toHaveLength(2)
    // The newest month's stocks, in the same unit, with the API's growth.
    expect(screen.getByTestId('lq-tile-m2').textContent).toBe(
      'Liquidity (M2) نقدینگی15,581 همت+53.3% on a year before · +6.4% on the month before'
    )
    expect(screen.getByTestId('lq-tile-base').textContent).toBe(
      'Base money پایه پولی2,196 همت+61.5% on a year before · +11.5% on the month before · revised method'
    )
    expect(screen.getByTestId('lq-tile-multiplier').textContent).toBe('Multiplier7.095liquidity ÷ base money, Esfand 1404')
    // The dollar in toman.
    expect(screen.getByTestId('lq-anchor').textContent).toContain('265,100 تومان')
    expect(screen.getByTestId('lq-scenario-last_12m').textContent).toContain('406,315')
  })

  it('shows the figure as CBI published it with the app on rial', async () => {
    window.localStorage.setItem('igp_unit', 'IRR')
    await renderPage()
    expect(screen.getByTestId('lq-unit-line').textContent).toContain('thousand billion rial (هزار میلیارد ریال), as CBI publishes it')
    const esfand = cellsOf('lq-row-1404-12')
    expect(esfand[1]).toBe('155,812')
    expect(esfand[5]).toBe('21,959')
    // Ratios and growth do not move with the unit.
    expect(esfand[10]).toBe('7.095')
    expect(esfand[3]).toBe('+53.3%')
    expect(screen.getByTestId('lq-tile-m2').textContent).toContain('155,812 thousand billion rial')
    expect(screen.getByTestId('lq-anchor').textContent).toContain('2,651,000 ریال')
    expect(screen.getByTestId('lq-scenario-last_12m').textContent).toContain('4,063,150')
    expect(screen.getByTestId('lq-notes').textContent).toContain('now: thousand billion rial')
  })
})

// ---------------------------------------------------------------------------
// The base-money method change
// ---------------------------------------------------------------------------

describe('the base-money method change', () => {
  it('is marked, and no growth is shown across it', async () => {
    await renderPage()
    showAllRows()
    for (const p of ['1399-12', '1400-01', '1400-12']) {
      expect(screen.getByTestId(`lq-break-${p}`).textContent).toBe('method change')
    }
    expect(screen.queryByTestId('lq-break-1401-12')).toBeNull()
    const row = screen.getByTestId('lq-row-1399-12')
    const cells = cellsOf('lq-row-1399-12')
    // Liquidity has no break: its growth is there.
    expect(cells[3]).toBe('+40.6%')
    // Base money's y/y would compare two definitions: a dash with the API's reason.
    expect(cells[7]).toBe('—')
    const reason = MONTHLY.series[1].points.find((p) => p.period === '1399-12')!.null_reason!.yoy_pct
    const yoy = within(row).getAllByRole('cell')[7]
    expect(within(yoy).getByTitle(reason)).toHaveTextContent('—')
    // The reason is read out too: a hover reaches neither a keyboard nor a phone.
    expect(yoy.querySelector('.sr-only')!.textContent).toBe(`not measured: ${reason}`)
    expect(yoy.querySelector('[aria-hidden="true"]')!.textContent).toBe('—')
    expect(cells[9]).toContain('revised')
    expect(screen.getByTestId('lq-levels').textContent).toContain('base-money method change — the line breaks there')
    expect(screen.getByTestId('lq-notes').textContent).toContain("Esfand 1399: CBI's base money is on its revised method")
  })

  it('never lets a chart line bridge a gap', () => {
    // Every line on the page is drawn with connectNulls={false}: a null in a
    // row is a gap, whether a missing month or a method change.
    const settings = liquiditySource.match(/connectNulls(=\{[^}]*\})?/g) ?? []
    expect(settings.length).toBeGreaterThan(0)
    expect(settings.every((s) => s === 'connectNulls={false}')).toBe(true)
  })

  it('says which years lack a base-money bar, and why', async () => {
    await renderPage()
    fireEvent.click(within(screen.getByTestId('lq-freq')).getByRole('button', { name: 'Annual' }))
    await screen.findByTestId('lq-year-1404')
    expect(screen.getByTestId('lq-growth-missing').textContent).toBe(
      'No base-money bar for 1399: CBI changed its base-money method inside the months that growth compares, and it is not measured across the change.'
    )
    const cells = within(screen.getByTestId('lq-year-1399')).getAllByRole('cell')
    expect(visible(cells[6])).toBe('—')
    expect(within(cells[6]).getByTitle(/changed its base-money method/)).toBeInTheDocument()
    // Nor a yearly average over months on two methods.
    expect(visible(cells[7])).toBe('—')
    expect(
      within(cells[7]).getByTitle('CBI changed its base-money method during 1399: a mean across the change would mix two definitions')
    ).toBeInTheDocument()
  })

  it('marks every switch and explains every gap when the API lists the change as one break', async () => {
    await renderPage({ monthly: { ...MONTHLY, breaks: ONE_BREAK }, annual: { ...ANNUAL, breaks: ONE_BREAK } })
    showAllRows()
    // The switches the stored methods show, not only the listed break.
    for (const p of ['1399-12', '1400-01', '1400-12']) {
      expect(screen.getByTestId(`lq-break-${p}`).textContent).toBe('method change')
    }
    fireEvent.click(within(screen.getByTestId('lq-freq')).getByRole('button', { name: 'Annual' }))
    await screen.findByTestId('lq-year-1401')
    fireEvent.click(within(screen.getByTestId('lq-measure')).getByRole('button', { name: 'Yearly average' }))
    // 1400 and 1401 each have twelve months; what stops the 1401 growth is 1400's two methods.
    expect(screen.getByTestId('lq-growth-missing').textContent).toContain('No base-money bar for 1399, 1400, 1401:')
    const y1401 = within(screen.getByTestId('lq-year-1401')).getAllByRole('cell')
    expect(visible(y1401[8])).toBe('—')
    expect(within(y1401[8]).getByTitle(/changed its base-money method/)).toBeInTheDocument()
  })

  it('shows no base-money yearly average over two methods, even when the API sends one', async () => {
    // As Track M's annual block does: the mean of all twelve months whatever their method.
    const annual = ANNUAL.annual!.map((a) =>
      a.year === 1399 || a.year === 1400 ? { ...a, avg_value: { ...a.avg_value!, CBI_BASE_MONEY: 5000 } } : a
    )
    await renderPage({ annual: { ...ANNUAL, breaks: ONE_BREAK, annual } })
    fireEvent.click(within(screen.getByTestId('lq-freq')).getByRole('button', { name: 'Annual' }))
    await screen.findByTestId('lq-year-1400')
    for (const y of [1399, 1400]) {
      const cells = within(screen.getByTestId(`lq-year-${y}`)).getAllByRole('cell')
      expect(visible(cells[7])).toBe('—')
      expect(within(cells[7]).getByTitle(`CBI changed its base-money method during ${y}: a mean across the change would mix two definitions`)).toBeInTheDocument()
      // Liquidity's average is one definition: shown.
      expect(visible(cells[3])).not.toBe('—')
    }
  })
})

// ---------------------------------------------------------------------------
// The dollar against liquidity-paced paths
// ---------------------------------------------------------------------------

describe('the dollar chart', () => {
  it('labels the band with its measured coverage, never a nominal one', async () => {
    await renderPage()
    const label = screen.getByTestId('lq-band-label').textContent ?? ''
    expect(label).toContain(
      'In the backtest from Farvardin 1394, a band built this way held the actual price about 84% of the time at 1–12 months and about 70% of the time at 24 months.'
    )
    expect(label).not.toMatch(/90%|95%|confiden|probability of/i)
    expect(label).toContain('the real price has left it more often than the way it is built would suggest')
    expect(screen.getByTestId('lq-dollar').textContent).not.toMatch(/\b90% band|\b95% band/)
  })

  it('says the band fell short only when its measured coverage did', async () => {
    await renderPage({ d12: { ...D12, band: { ...D12.band!, coverage: { '1-12': 0.93, '24': 0.91 } } } })
    const label = screen.getByTestId('lq-band-label').textContent ?? ''
    expect(label).toContain('about 93% of the time at 1–12 months')
    expect(label).not.toContain('left it more often')
  })

  it('opens on twelve months and states the 24-month coverage beside the choice', async () => {
    await renderPage()
    const horizon = screen.getByTestId('lq-horizon')
    expect(within(horizon).getByRole('button', { name: '12 months' })).toHaveAttribute('aria-pressed', 'true')
    const note = screen.getByTestId('lq-horizon-note')
    expect(note.textContent).toBe(
      '24 months: in the backtest the band held the actual price about 70% of the time at 24 months — less often than within 12 months (about 84% of the time at 1–12 months).'
    )
    expect(note.className).toContain('muted')
    expect(screen.getByTestId('lq-dollar-legend').textContent).not.toContain('12 months after the last close')
    const table = screen.getByTestId('lq-scenario-table')
    expect(within(table).queryByRole('columnheader', { name: '24 months' })).toBeNull()

    fireEvent.click(within(horizon).getByRole('button', { name: '24 months' }))
    await waitFor(() => expect(within(screen.getByTestId('lq-scenario-table')).getByRole('columnheader', { name: '24 months' })).toBeInTheDocument())
    expect(apiMock).toHaveBeenCalledWith('/liquidity/dollar?horizon=24', expect.anything())
    // With the longer view on screen, the lower coverage is said in the text ink.
    expect(screen.getByTestId('lq-horizon-note').className).toContain('lq-coverage-strong')
    // …and the chart marks where the twelve months end.
    expect(screen.getByTestId('lq-dollar-legend').textContent).toContain('12 months after the last close')
    expect(cellsOf('lq-scenario-last_12m')[5]).toMatch(/^622,754454,\d{3} – 1,471,\d{3}$/)
  })

  it('tabulates every path at 3, 6 and 12 months with the band, and the trend apart', async () => {
    await renderPage()
    const s1 = cellsOf('lq-scenario-last_12m')
    expect(s1[0]).toContain('Path 1: Liquidity growth of the last 12 months')
    expect(s1[0]).toContain('M2 growth Esfand 1403 → Esfand 1404')
    expect(s1[1]).toBe('53.3%')
    // 12 months: the path and the band the research reported (311k–860k).
    expect(s1[4]).toMatch(/^406,315310,9\d\d – 860,4\d\d$/)
    const trend = cellsOf('lq-scenario-trend')
    expect(trend[0]).toContain('a comparison line, not a liquidity path')
    expect(trend[1]).toBe('39.6%')
    expect(trend[4]).toBe('370,100')
    // How far apart the paths end against how wide the band is — from the numbers.
    expect(screen.getByTestId('lq-spread').textContent).toMatch(
      /At 12 months the paths end 80,8\d\d apart, while the band around one of them spans 549,\d{3}: the uncertainty the backtest measured is wider than the difference between the scenarios\./
    )
  })

  it('lets the reader choose which path the band surrounds when it applies to each', async () => {
    await renderPage()
    const pick = screen.getByTestId('lq-band-pick')
    expect(within(pick).getByRole('button', { name: 'Path 1' })).toHaveAttribute('aria-pressed', 'true')
    expect(cellsOf('lq-scenario-slowest_3y')[4]).toBe('325,490')
    fireEvent.click(within(pick).getByRole('button', { name: 'Path 3' }))
    expect(screen.getByTestId('lq-band-label').textContent).toContain('around path 3')
    expect(cellsOf('lq-scenario-slowest_3y')[4]).toMatch(/^325,490249,\d{3} – 689,\d{3}$/)
    expect(cellsOf('lq-scenario-last_12m')[4]).toBe('406,315')
  })

  it('offers no choice when the API names the path', async () => {
    await renderPage({ d12: { ...D12, band: { ...D12.band!, applied_to: 'avg_3y' } } })
    expect(screen.queryByTestId('lq-band-pick')).toBeNull()
    expect(screen.getByTestId('lq-band-label').textContent).toContain('around path 2')
  })

  it('names the anchor, the money month and the scenario label', async () => {
    await renderPage()
    expect(screen.getByTestId('lq-anchor').textContent).toBe(
      'Every path starts at the last stored close, 265,100 تومان on 1405/07/18 — free-market rate (USDT/toman since 2022-04). ' +
        'Their growth rates are read from Esfand 1404, the newest liquidity month stored (workbook saved 1405/03/31).'
    )
    expect(screen.getByTestId('lq-dollar').textContent).toContain('مسیر هم‌پای رشد نقدینگی — سناریو، نه پیش‌بینی')
    const legend = screen.getByTestId('lq-dollar-legend').textContent ?? ''
    // Its newest point is the running month's latest close, and says so.
    expect(legend).toContain('Dollar, month-end close (Mehr 1405, still running: its latest close)')
    expect(legend).toContain('Path 3: Slowest 3-year liquidity growth since 1390 (22.8% a year)')
    expect(legend).toContain('The dollar’s own trend since 1390-10 (39.6% a year)')
    expect(legend).toContain('Band around path 1')
  })
})

describe('the dollar per unit of liquidity and the liquidity dollar', () => {
  it('states the index’s base and range', async () => {
    await renderPage()
    expect(screen.getByTestId('lq-index-caption').textContent).toContain(
      'as an index equal to 100 at Tir 1397. Lowest 54.9 (Shahrivar 1396), highest 209.3 (Bahman 1404), newest 204.6 (Esfand 1404).'
    )
  })

  it('shows the liquidity dollar as a range over base months, with its caveat', async () => {
    await renderPage()
    const card = screen.getByTestId('lq-liquidity-dollar')
    expect(card.textContent).toContain(
      'at Esfand 1404, across 118 base months, it runs from 43,573 تومان to 166,208 تومان. It is arithmetic about the month you pick — not a fair value, and not a forecast.'
    )
    const rows = within(screen.getByTestId('lq-ld-table')).getAllByRole('row').slice(1)
    // The research's median of 118 bases: the mean of the two middle ones.
    expect(rows.map((r) => r.textContent)).toEqual([
      'LowestShahrivar 139643,573 تومان',
      'MedianAzar 1400 and Azar 1402 (the mean of the two middle values)104,421 تومان',
      'HighestBahman 1404166,208 تومان'
    ])
    expect(screen.getByTestId('lq-ld-caveat').textContent).toBe(
      'The dollar’s actual average in Esfand 1404 was 162,495 تومان. Not a fair value: the base month chosen decides it.'
    )
  })
})

describe('the backtest card', () => {
  it('leads with the API’s verdict, then says what its numbers show', async () => {
    await renderPage()
    expect(screen.getByTestId('lq-verdict').textContent).toBe(D12.backtest!.verdict)
    const items = within(screen.getByTestId('lq-statements')).getAllByRole('listitem').map((li) => li.textContent)
    expect(items).toEqual([
      'Against assuming no change: measurably smaller misses at every horizon tested, 3, 6, 12 and 24 months (p ≤ 0.002).',
      'Against the dollar’s own past trend: no measurable difference at any horizon tested, 3, 6, 12 and 24 months (p 0.16–0.30).',
      'In this test, pacing the dollar with liquidity growth added no skill the dollar’s own trend did not already have.'
    ])
    // The error table, smallest per horizon in bold weight, not colour.
    const paced = within(screen.getByTestId('lq-rmse-m2_paced')).getAllByRole('cell')
    expect(paced.map((c) => c.textContent)).toEqual(['0.160', '0.236', '0.339', '0.419'])
    for (const c of paced) expect(c.className).toContain('lq-best')
    expect(within(screen.getByTestId('lq-rmse-random_walk')).getAllByRole('cell')[3].textContent).toBe('0.803')
    expect(screen.getByTestId('lq-backtest').textContent).toContain('Target: the month-end close.')
  })

  it('says something else when the numbers say something else', async () => {
    const bt = D12.backtest!
    const changed: LiquidityDollarResponse = {
      ...D12,
      backtest: {
        ...bt,
        rmse: { ...bt.rmse, m2_paced: { ...bt.rmse.m2_paced, '24': 0.5 } },
        dm_vs_trend_p: { m2_paced: { '3': 0.01, '6': 0.02, '12': 0.03, '24': 0.04 } }
      }
    }
    await renderPage({ d12: changed })
    const items = within(screen.getByTestId('lq-statements')).getAllByRole('listitem').map((li) => li.textContent)
    expect(items[1]).toBe(
      'Against the dollar’s own past trend: measurably smaller misses at every horizon tested, 3, 6, 12 and 24 months (p ≤ 0.04).'
    )
    expect(items[2]).toContain('added measurable skill over the dollar’s own trend at every horizon tested')
    // The bold follows the smallest error, wherever it is.
    expect(within(screen.getByTestId('lq-rmse-dollar_trend')).getAllByRole('cell')[3].className).toContain('lq-best')
    expect(within(screen.getByTestId('lq-rmse-m2_paced')).getAllByRole('cell')[3].className).not.toContain('lq-best')
  })
})

// ---------------------------------------------------------------------------
// Degraded states
// ---------------------------------------------------------------------------

describe('before the CBI series are ingested', () => {
  it('says so and draws no money chart', async () => {
    await renderPage({ monthly: EMPTY, d12: D_EMPTY }, 'lq-not-ingested')
    const card = screen.getByTestId('lq-not-ingested')
    expect(card.textContent).toContain('The CBI money series are not stored yet')
    expect(card.textContent).toContain('No observation of either is stored in this deployment yet')
    expect(screen.queryByTestId('lq-money')).toBeNull()
    expect(screen.queryByTestId('lq-as-of')).toBeNull()
    expect(screen.queryByTestId('lq-stale')).toBeNull()
  })

  it('draws the dollar without paths, band, backtest or index', async () => {
    await renderPage({ monthly: EMPTY, d12: D_EMPTY }, 'lq-not-ingested')
    expect(screen.getByTestId('lq-no-scenarios').textContent).toBe(
      'No scenario path is drawn: each is paced by a liquidity growth rate, and no CBI liquidity figure is stored.'
    )
    expect(screen.queryByTestId('lq-band-label')).toBeNull()
    expect(screen.queryByTestId('lq-no-band')).toBeNull()
    expect(screen.getByTestId('lq-backtest-empty')).toBeInTheDocument()
    expect(screen.getByTestId('lq-index-empty')).toBeInTheDocument()
    expect(screen.getByTestId('lq-liquidity-dollar').textContent).toContain('Not available')
    // The dollar's own trend needs no liquidity: still tabulated.
    expect(screen.getByTestId('lq-scenario-trend')).toBeInTheDocument()
  })
})

describe('a missing scenario', () => {
  it('says how many paths could be drawn', async () => {
    await renderPage({ d12: { ...D12, scenarios: D12.scenarios.slice(0, 2) } })
    expect(screen.getByTestId('lq-scenario-missing').textContent).toBe(
      '2 of the three scenario paths could be drawn. The API returned 2 scenarios.'
    )
    expect(screen.queryByTestId('lq-scenario-slowest_3y')).toBeNull()
  })

  it('names a scenario whose growth could not be measured', async () => {
    const scenarios = D12.scenarios.map((s) =>
      s.key === 'slowest_3y' ? { ...s, annual_growth_pct: null, path: [] } : s
    )
    await renderPage({ d12: { ...D12, scenarios } })
    expect(screen.getByTestId('lq-scenario-missing').textContent).toBe(
      '2 of the three scenario paths could be drawn. Slowest 3-year liquidity growth since 1390: its liquidity growth could not be measured from the stored months.'
    )
    expect(cellsOf('lq-scenario-slowest_3y').slice(1)).toEqual(['—', '—', '—', '—'])
    expect(screen.getByTestId('lq-dollar-legend').textContent).not.toContain('Path 3')
  })

  it('keeps each path’s number and colour when the one before it cannot be drawn', async () => {
    const scenarios = D12.scenarios.map((s) => (s.key === 'avg_3y' ? { ...s, annual_growth_pct: null, path: [] } : s))
    await renderPage({ d12: { ...D12, scenarios } })
    const pick = screen.getByTestId('lq-band-pick')
    expect(within(pick).getAllByRole('button').map((b) => b.textContent)).toEqual(['Path 1', 'Path 3'])
    fireEvent.click(within(pick).getByRole('button', { name: 'Path 3' }))
    expect(screen.getByTestId('lq-band-label').textContent).toContain('around path 3')
    const legend = screen.getByTestId('lq-dollar-legend').textContent ?? ''
    expect(legend).toContain('Path 3: Slowest 3-year liquidity growth since 1390')
    expect(legend).not.toContain('Path 2')
  })

  it('says when there is no band', async () => {
    await renderPage({ d12: { ...D12, band: null } })
    expect(screen.getByTestId('lq-no-band').textContent).toBe('The API returned no band for these paths, so none is drawn.')
    expect(screen.queryByTestId('lq-band-pick')).toBeNull()
    expect(screen.queryByTestId('lq-horizon-note')).toBeNull()
    expect(screen.getByTestId('lq-dollar').textContent).not.toContain('the figure under a value is the band')
    expect(screen.queryByTestId('lq-spread')).toBeNull()
  })
})

describe('failures and old prices', () => {
  it('keeps the dollar section when the money series fail to load', async () => {
    apiMock.mockImplementation(route({ monthly: new Error('upstream timeout') }))
    render(
      <MemoryRouter>
        <SettingsProvider>
          <Liquidity />
        </SettingsProvider>
      </MemoryRouter>
    )
    expect(await screen.findByText('The money series could not be loaded')).toBeInTheDocument()
    expect(screen.getByText('upstream timeout')).toBeInTheDocument()
    expect(await screen.findByTestId('lq-dollar')).toBeInTheDocument()
  })

  it('says when the dollar close every path starts from is old', async () => {
    await renderPage({ d12: { ...D12, data_age: { stale: true, age_days: 9 } } })
    expect(screen.getByTestId('lq-dollar-stale').textContent).toBe(
      'The newest dollar close stored is 1405/07/18, 9 days ago: every path starts from it.'
    )
  })

  it('reads the dollar close’s age from the API’s per-input block too', async () => {
    const data_age = {
      money: { stale: true, age_days: 205 },
      usd: { newest_day: '2026-10-01', age_days: 9, stale: true, warning: 'The scenarios start from a stale price.' }
    }
    await renderPage({ d12: { ...D12, data_age } })
    expect(screen.getByTestId('lq-dollar-stale').textContent).toBe(
      'The newest dollar close stored is 1405/07/18, 9 days ago: every path starts from it. The scenarios start from a stale price.'
    )
  })

  it('stays quiet when only the money block is old', async () => {
    const data_age = { money: { stale: true, age_days: 205 }, usd: { age_days: 1, stale: false } }
    await renderPage({ d12: { ...D12, data_age } })
    expect(screen.queryByTestId('lq-dollar-stale')).toBeNull()
  })

  it('blames the missing dollar, not liquidity, when no close is stored', async () => {
    const noDollar: LiquidityDollarResponse = {
      ...D12,
      available: false,
      unavailable: ['USD_IRT has no stored daily close on this deployment, so there is no dollar to draw or model.'],
      anchor: null,
      history: [],
      scenarios: [],
      trend: null,
      band: null,
      backtest: null,
      liquidity_dollar: null
    }
    await renderPage({ d12: noDollar })
    expect(screen.getByTestId('lq-no-scenarios').textContent).toBe(
      'No scenario path is drawn: every path starts from the last dollar close, and no dollar close is stored. ' +
        'USD_IRT has no stored daily close on this deployment, so there is no dollar to draw or model.'
    )
    expect(screen.getByTestId('lq-no-history')).toBeInTheDocument()
    expect(screen.queryByTestId('lq-dollar-stale')).toBeNull()
  })

  it('refuses money figures in a unit it does not convert from', async () => {
    await renderPage({ monthly: { ...MONTHLY, unit: 'trillion_toman' } }, 'lq-unit-refused')
    expect(screen.getByTestId('lq-unit-refused').textContent).toContain('The API reports these stocks in “trillion_toman”')
    expect(screen.queryByTestId('lq-money')).toBeNull()
    expect(screen.queryByTestId('lq-table')).toBeNull()
    // The dollar section is in toman whatever the money unit: still there.
    expect(screen.getByTestId('lq-dollar')).toBeInTheDocument()
  })

  it('says so when an annual answer comes without its years', async () => {
    const { annual: _dropped, ...noYears } = ANNUAL
    await renderPage({ annual: noYears })
    fireEvent.click(within(screen.getByTestId('lq-freq')).getByRole('button', { name: 'Annual' }))
    expect(await screen.findByTestId('lq-annual-missing')).toHaveTextContent(
      'The API answered the annual request without its yearly figures'
    )
  })
})

// ---------------------------------------------------------------------------
// What the page says, and what it never says
// ---------------------------------------------------------------------------

/**
 * Advice, valuation and causal words. The page has to be able to say what a
 * figure is NOT — "not a fair value", "not forecasts" — so those negations
 * are removed before the check; nothing else is.
 */
const NEVER =
  /\b(buy|sell|buying|selling|recommend\w*|undervalued|overvalued|cheap|expensive|bargain|opportunit\w*|should|equilibrium|fair price|target price|will (reach|rise|fall|hit)|caus(e|es|ed|ing)|drives?|driven)\b|fair value|خرید|فروش|ارزان|گران/i

function said(text: string): string {
  return text.replace(/not a fair value/gi, '').replace(/not (a )?forecasts?/gi, '')
}

describe('language', () => {
  it('uses no advice, valuation or causal wording on the page', async () => {
    const { container } = await renderPage()
    expect(said(container.textContent ?? '')).not.toMatch(NEVER)
    fireEvent.click(within(screen.getByTestId('lq-horizon')).getByRole('button', { name: '24 months' }))
    await within(screen.getByTestId('lq-scenario-table')).findByRole('columnheader', { name: '24 months' })
    fireEvent.click(within(screen.getByTestId('lq-freq')).getByRole('button', { name: 'Annual' }))
    await screen.findByTestId('lq-year-1404')
    expect(said(container.textContent ?? '')).not.toMatch(NEVER)
  })

  it('uses none in its degraded states either', async () => {
    const { container } = await renderPage({ monthly: EMPTY, d12: D_EMPTY }, 'lq-not-ingested')
    expect(said(container.textContent ?? '')).not.toMatch(NEVER)
  })

  it('has none in its source', () => {
    // Comments included: they are where a later edit copies its wording from.
    expect(said(liquiditySource)).not.toMatch(NEVER)
    expect(said(liquidityLibSource)).not.toMatch(NEVER)
  })

  it('says it is a scenario, that correlation is not causation, and what the paths cannot see', async () => {
    await renderPage()
    const notes = screen.getByTestId('lq-notes').textContent ?? ''
    expect(notes).toContain('A scenario, not a forecast.')
    expect(notes).toContain('Correlation is not causation.')
    expect(notes).toContain(
      'Sanctions, oil revenue, war and politics, expectations, Iran’s multiple exchange rates and the central bank’s foreign-exchange interventions.'
    )
    expect(notes).toContain('CBI publishes these stocks in هزار میلیارد ریال (thousand billion rial); ÷10 = همت (trillion toman).')
    expect(notes).toContain('Its monthly workbooks have arrived 10 to 137 days after the month they describe')
    // The API's own notes travel with it, each once.
    const api = within(screen.getByTestId('lq-api-notes')).getAllByRole('listitem').map((li) => li.textContent)
    expect(api).toEqual([...MONTHLY.notes, ...D12.notes])
  })
})

describe('navigation', () => {
  it('sits right after Economy, at /liquidity', () => {
    const economy = appSource.indexOf("{ to: '/economy', label: 'Economy' }")
    const liquidity = appSource.indexOf("{ to: '/liquidity', label: 'Money & liquidity' }")
    const next = appSource.indexOf("{ to: '/relative-value'")
    expect(economy).toBeGreaterThan(0)
    expect(liquidity).toBeGreaterThan(economy)
    expect(next).toBeGreaterThan(liquidity)
    // Nothing between them.
    expect(appSource.slice(economy + 1, liquidity)).not.toContain('{ to:')
    expect(appSource).toContain('<Route path="/liquidity" element={<Liquidity />} />')
  })
})
