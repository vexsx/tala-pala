import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Liquidity from '../pages/Liquidity'
import { SettingsProvider } from '../lib/settings'
import liquiditySource from '../pages/Liquidity.tsx?raw'
import liquidityLibSource from '../lib/liquidity.ts?raw'
import appSource from '../App.tsx?raw'
import type { LiquidityAggregatesResponse, LiquidityDollarResponse, LiquidityPoint } from '../api/types'

// CAPTURED from the routes on the local rehearsal of real data — see
// fixtures/README.md ("Money & liquidity"): CBI's 48 monthly workbooks and its
// tsdview quarter-ends, USD_IRT's TGJU history and one BitMax close, served by
// internal/liquidity on 2026-10-11. The two not-ingested files are the same
// routes on a copy of that database with the CBI series removed.
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

/**
 * What a sighted reader sees in an element: its text without the
 * screen-reader-only parts or the reason a closed dash keeps folded away.
 */
function visible(el: Element): string {
  const copy = el.cloneNode(true) as Element
  copy.querySelectorAll('.sr-only').forEach((n) => n.remove())
  copy.querySelectorAll('details:not([open]) > :not(summary)').forEach((n) => n.remove())
  return copy.textContent ?? ''
}

/** The reason a missing figure opens to. */
function reasonOf(cell: Element): string | null {
  return cell.querySelector('.lq-why-text')?.textContent ?? null
}

function cellsOf(testId: string): string[] {
  return within(screen.getByTestId(testId))
    .getAllByRole('cell')
    .map(visible)
}

/**
 * The capture with some base-money months restated on the revised method,
 * as a deployment that ingested more editions would hold them (the Esfand
 * 1402 workbook restates Esfand 1400 revised; the rehearsal never ingested
 * it). Only `method` moves: a stated transformation for a layout the page
 * must also handle, not a fixture.
 */
function withMethods<T extends LiquidityAggregatesResponse>(resp: T, methods: Record<string, LiquidityPoint['method']>): T {
  return {
    ...resp,
    series: resp.series.map((s) =>
      s.code === 'CBI_BASE_MONEY'
        ? { ...s, points: s.points.map((p) => (p.period in methods ? { ...p, method: methods[p.period] } : p)) }
        : s
    )
  }
}

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
    // Seen here, not reconstructed: no qualifier.
    expect(line).toContain('First readable here 1405/07/19.')
    expect(line).not.toContain('approximately')
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

  it('says when the date it was first readable is reconstructed, not seen', async () => {
    // As the website backfill stores it: the workbook's saved time plus a day.
    await renderPage({
      monthly: {
        ...MONTHLY,
        latest: { ...MONTHLY.latest!, available_at: '2026-06-22T05:41:31Z', available_at_approx: true }
      }
    })
    expect(screen.getByTestId('lq-as-of').textContent).toContain(
      'First readable here 1405/04/01 (approximately: reconstructed from the workbook’s saved time, not seen then).'
    )
  })

  it('says so when CBI gave no date at all', async () => {
    await renderPage({ monthly: { ...MONTHLY, latest: { ...MONTHLY.latest!, published_at: null } } })
    const line = screen.getByTestId('lq-as-of').textContent ?? ''
    expect(line).toContain('CBI states no release date for it.')
    expect(line).not.toContain('days after the month ended')
  })

  it('warns once when the newest month is older than CBI has ever taken, with the API’s lag', async () => {
    await renderPage()
    const notice = screen.getByTestId('lq-stale').textContent ?? ''
    expect(notice).toBe(
      'Nothing newer than Esfand 1404 is stored, and it ended 205 days ago.' +
        'CBI’s workbooks have arrived 21 to 164 days after the month they describe, so the next month is overdue: ' +
        'CBI has not published it, or it has not been read yet. Every money figure below, and every scenario rate ' +
        'built on them, describes Esfand 1404.'
    )
    // Said once: the API's own warning, which says the same, is not appended.
    expect(MONTHLY.data_age.warning).toContain('205 days ago')
    expect(notice).not.toContain(MONTHLY.data_age.warning!)
  })

  it('states the lag the API sends, not one of its own', async () => {
    await renderPage({ monthly: { ...MONTHLY, data_age: { ...MONTHLY.data_age, publication_lag_days: { min: 30, max: 250 } } } })
    expect(screen.getByTestId('lq-stale').textContent).toContain('arrived 30 to 250 days after the month')
    expect(screen.getByTestId('lq-notes').textContent).toContain('arrived 30 to 250 days after the month they describe')
  })

  it('judges by the API’s bound, or the measured one, when the server gives no verdict', async () => {
    await renderPage({ monthly: { ...MONTHLY, data_age: {} } })
    // 205 days against the longest lag plus a month, 195.
    expect(screen.getByTestId('lq-stale')).toBeInTheDocument()
  })

  it('takes the API’s bound when it sends one without a verdict', async () => {
    await renderPage({ monthly: { ...MONTHLY, data_age: { stale_after_days: 231 } } })
    expect(screen.queryByTestId('lq-stale')).toBeNull()
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
    expect(screen.getByTestId('lq-more').textContent).toBe('Show all 94')
    showAllRows()
    const months = tableMonths()
    // Eleven tsdview quarter-ends (1395-03 → 1397-09), the oldest workbook's
    // Esfand 1397, tsdview's 1398-03, and 81 months (1398-04 → 1404-12).
    expect(months).toHaveLength(94)
    expect(months[0]).toBe('1404-12')
    // Ten whole Jalali years back from Esfand 1404: from Farvardin 1395.
    expect(months[months.length - 1]).toBe('1395-03')
    expect(months).not.toContain('1394-12')
    // The quarter-end history says what it is.
    expect(screen.getByTestId('lq-row-1396-06').textContent).toContain('quarter-end')
    expect(screen.getByTestId('lq-row-1398-03').textContent).toContain('quarter-end')
    expect(visible(screen.getByTestId('lq-row-1397-12'))).not.toContain('quarter-end')
    expect(screen.getByTestId('lq-row-1404-11').textContent).not.toContain('quarter-end')
  })

  it('shows the whole history on request', async () => {
    await renderPage()
    fireEvent.click(within(screen.getByTestId('lq-window')).getByRole('button', { name: 'All (from 1352)' }))
    showAllRows()
    const months = tableMonths()
    expect(months).toHaveLength(266)
    expect(months[months.length - 1]).toBe('1352-03')
    expect(screen.getByTestId('lq-levels-unit').textContent).toContain(
      'Before Tir 1398 only some months are stored — CBI’s quarter-ends — and the line joins them point to point.'
    )
  })

  it('names one month, the API’s, wherever it says where the monthly figures start', async () => {
    // monthly_from is Tir 1398. Read from the dollar history alone, tsdview's
    // Khordad 1398 quarter-end would touch it and give another month; every
    // caption names the API's instead.
    await renderPage()
    fireEvent.click(within(screen.getByTestId('lq-window')).getByRole('button', { name: 'All (from 1352)' }))
    fireEvent.click(within(screen.getByRole('group', { name: 'History shown' })).getByRole('button', { name: 'Since 1390' }))
    const captions = ['lq-levels-unit', 'lq-growth-caption', 'lq-index-caption', 'lq-notes'].map(
      (id) => screen.getByTestId(id).textContent ?? ''
    )
    for (const c of captions) {
      expect(c).toContain('Tir 1398')
      expect(c).not.toContain('Khordad 1398')
      expect(c).not.toContain('1397')
    }
    expect(captions[3]).toContain('month by month from Tir 1398')
    expect(captions[2]).toContain('Before Tir 1398 liquidity is stored only at some months — CBI’s quarter-ends — and so is the index.')
    // The API's null reasons name the same month.
    expect(MONTHLY.series[0].points.find((p) => p.period === '1398-05')!.null_reason!.yoy_pct).toContain('before 1398-04')
  })

  it('explains the gaps in liquidity’s own year-on-year line', async () => {
    await renderPage()
    // A month a year after Tir 1398 is the first whose comparison runs
    // unbroken; before it only quarter-ends compare, drawn as dots.
    expect(screen.getByTestId('lq-growth-caption').textContent).toContain(
      'Both lines run unbroken only from Tir 1399: before Tir 1398 only CBI’s quarter-ends are stored, so a month a year after that has a comparison only at a quarter-end, drawn as a dot.'
    )
    fireEvent.click(within(screen.getByTestId('lq-window')).getByRole('button', { name: '5 years' }))
    expect(screen.getByTestId('lq-growth-caption').textContent).not.toContain('run unbroken only from')
  })

  it('switches to Jalali years with Esfand-over-Esfand growth', async () => {
    await renderPage()
    fireEvent.click(within(screen.getByTestId('lq-freq')).getByRole('button', { name: 'Annual' }))
    await screen.findByTestId('lq-year-1404')
    expect(apiMock).toHaveBeenCalledWith('/liquidity/aggregates?freq=annual', expect.anything())
    const y1404 = cellsOf('lq-year-1404')
    expect(y1404).toEqual(['1404', '15,581', '+53.3%', '12,685', '+39.7%', '2,196', '+61.5%', '1,692', '+42.3%', '12'])
    // A year short of months says what it holds.
    expect(screen.getByTestId('lq-year-1398').textContent).toContain('10 of 12 months')
    // The growth chart's heading says which growth it is.
    expect(screen.getByTestId('lq-growth').textContent).toContain('Growth — Esfand over Esfand')
    fireEvent.click(within(screen.getByTestId('lq-measure')).getByRole('button', { name: 'Yearly average' }))
    expect(screen.getByTestId('lq-growth').textContent).toContain('Growth — yearly average over yearly average')
    // A year with only part of its months has no yearly average.
    fireEvent.click(within(screen.getByTestId('lq-window')).getByRole('button', { name: 'All (from 1352)' }))
    const y1397 = screen.getByTestId('lq-year-1397')
    expect(y1397.textContent).toContain('quarter-ends only')
    expect(y1397.textContent).not.toContain('partial year')
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

    // Base money y/y, largest first: Esfand 1404's 61.5%, and the months
    // left unmeasured — before the monthly record, and across the method
    // change at 1401-12 — at the bottom, in month order.
    showAllRows()
    const baseYoy = within(table).getAllByRole('button', { name: 'y/y' })[1]
    fireEvent.click(baseYoy)
    expect(baseYoy.closest('th')).toHaveAttribute('aria-sort', 'descending')
    const months = tableMonths()
    expect(months.slice(0, 3)).toEqual(['1404-12', '1404-11', '1404-10'])
    expect(months.slice(-2)).toEqual(['1402-10', '1402-11'])
    fireEvent.click(baseYoy)
    expect(baseYoy.closest('th')).toHaveAttribute('aria-sort', 'ascending')
    expect(tableMonths().slice(-1)[0]).toBe('1402-11')
  })

  it('labels every control group, on screen too, and states each toggle', async () => {
    await renderPage()
    for (const [name, shown] of [
      ['Frequency', 'View'],
      ['Years shown', 'Years'],
      ['Months ahead', 'Paths'],
      ['History shown', 'History']
    ]) {
      const group = screen.getByRole('group', { name })
      for (const b of within(group).getAllByRole('button')) expect(b).toHaveAttribute('aria-pressed')
      // "3 years" beside "24 months" must not read as a horizon: each row is named.
      expect(group.querySelector('.lq-chips-label')!.textContent).toBe(shown)
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
    // Always three decimals: Khordad 1396's 7.100329 is not "7.1".
    showAllRows()
    expect(cellsOf('lq-row-1396-03')[10]).toBe('7.100')
    expect(cellsOf('lq-row-1395-12')[10]).toBe('6.970')
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
    expect(screen.getByTestId('lq-anchor').textContent).toContain('264,630 تومان')
    expect(screen.getByTestId('lq-scenario-last_12m').textContent).toContain('405,595')
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
    expect(screen.getByTestId('lq-anchor').textContent).toContain('2,646,300 ریال')
    expect(screen.getByTestId('lq-scenario-last_12m').textContent).toContain('4,055,950')
    expect(screen.getByTestId('lq-notes').textContent).toContain('now: thousand billion rial')
  })
})

// ---------------------------------------------------------------------------
// The base-money method change
// ---------------------------------------------------------------------------

describe('the base-money method change', () => {
  it('is marked where the stored months change method, and no growth is shown across it', async () => {
    await renderPage()
    showAllRows()
    // The only switch in the stored months: old through 1401-11, revised from
    // 1401-12. CBI's footnote dates the redefinition from 1399-12, but every
    // stored month from there to 1401-11 is an old-method print, and the line
    // runs straight through it: no marker there.
    expect(MONTHLY.breaks[0].from).toBe('1399-12')
    expect(screen.getByTestId('lq-break-1401-12').textContent).toBe('method change')
    for (const p of ['1399-12', '1400-01', '1400-12', '1402-01']) expect(screen.queryByTestId(`lq-break-${p}`)).toBeNull()
    const row = screen.getByTestId('lq-row-1401-12')
    const cells = cellsOf('lq-row-1401-12')
    // Liquidity has no break: its growth is there.
    expect(cells[3]).toBe('+31.1%')
    // Base money's y/y would compare two definitions: a dash that opens to the API's reason.
    expect(cells[7]).toBe('—')
    const reason = MONTHLY.series[1].points.find((p) => p.period === '1401-12')!.null_reason!.yoy_pct
    expect(reason).toBe(
      '1401-12 is on the revised base-money method and 1400-12 on the old one (CBI redefined base money from 1399-12); growth across the two methods is not computed'
    )
    const yoy = within(row).getAllByRole('cell')[7]
    expect(reasonOf(yoy)).toBe(reason)
    expect(cells[9]).toContain('revised')
    // The month before the switch is the old method, its growth measured.
    expect(cellsOf('lq-row-1401-11')[7]).toBe('+39.2%')
    expect(cellsOf('lq-row-1401-11')[9]).toBe('old')
    expect(screen.getByTestId('lq-levels').textContent).toContain('base-money method change — the line breaks there')
    expect(screen.getByTestId('lq-notes').textContent).toContain(
      'In the stored data the first month on the revised definition is 1401-12'
    )
  })

  it('opens a missing figure to its reason by tap, click or key — not only on hover', async () => {
    await renderPage()
    const yoy = within(screen.getByTestId('lq-row-1404-12')).getAllByRole('cell')[3]
    expect(yoy.querySelector('details')).toBeNull()
    showAllRows()
    const cell = within(screen.getByTestId('lq-row-1401-12')).getAllByRole('cell')[7]
    const details = cell.querySelector('details')!
    const summary = within(cell).getByText('not measured; show why').closest('summary')!
    expect(details.open).toBe(false)
    expect(visible(cell)).toBe('—')
    fireEvent.click(summary)
    details.open = true // jsdom does not toggle <details> on a click; a browser does
    expect(visible(cell)).toContain('growth across the two methods is not computed')
    // The table says how to reach it.
    expect(screen.getByTestId('lq-data').textContent).toContain('select it for the reason')
    expect(screen.getByTestId('lq-data').textContent).not.toContain('hover')
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
      'No base-money bar for 1401: CBI changed its base-money method inside the months that growth compares, and it is not measured across the change.'
    )
    const cells = within(screen.getByTestId('lq-year-1401')).getAllByRole('cell')
    // Esfand 1401 (revised) over Esfand 1400 (old): the API's own reason.
    expect(visible(cells[6])).toBe('—')
    expect(reasonOf(cells[6])).toBe(ANNUAL.annual!.find((a) => a.year === 1401)!.null_reason!.CBI_BASE_MONEY.end_growth_pct)
    // Nor a yearly average over months on two methods.
    expect(visible(cells[7])).toBe('—')
    expect(reasonOf(cells[7])).toBe(
      'the months of 1401 are not all on one recorded base-money method; an average across the two definitions is not computed'
    )
    fireEvent.click(within(screen.getByTestId('lq-measure')).getByRole('button', { name: 'Yearly average' }))
    expect(screen.getByTestId('lq-growth-missing').textContent).toContain('No base-money bar for 1401, 1402:')
  })

  it('marks every switch of a layout with more of them', async () => {
    // Esfand 1400 restated revised between old-method months: three switches.
    await renderPage({ monthly: withMethods(MONTHLY, { '1400-12': 'revised' }) })
    showAllRows()
    for (const p of ['1400-12', '1401-01', '1401-12']) {
      expect(screen.getByTestId(`lq-break-${p}`).textContent).toBe('method change')
    }
    expect(screen.queryByTestId('lq-break-1399-12')).toBeNull()
  })

  it('marks the listed break only when no stored month records its method', async () => {
    const unrecorded = withMethods(
      MONTHLY,
      Object.fromEntries(MONTHLY.series[1].points.map((p) => [p.period, null]))
    )
    await renderPage({ monthly: unrecorded })
    showAllRows()
    expect(screen.getByTestId('lq-break-1399-12').textContent).toBe('method change')
    expect(screen.queryByTestId('lq-break-1401-12')).toBeNull()
  })

  it('shows no base-money yearly average over two methods, even when an answer carries one', async () => {
    // An answer with a mean over 1401's two methods and no reason beside it.
    const annual = ANNUAL.annual!.map((a) =>
      a.year === 1401 ? { ...a, avg_value: { ...a.avg_value!, CBI_BASE_MONEY: 7000 }, null_reason: undefined } : a
    )
    await renderPage({ annual: { ...ANNUAL, annual } })
    fireEvent.click(within(screen.getByTestId('lq-freq')).getByRole('button', { name: 'Annual' }))
    await screen.findByTestId('lq-year-1401')
    const cells = within(screen.getByTestId('lq-year-1401')).getAllByRole('cell')
    expect(visible(cells[7])).toBe('—')
    expect(reasonOf(cells[7])).toBe('CBI changed its base-money method during 1401: a mean across the change would mix two definitions')
    // Liquidity's average is one definition: shown.
    expect(visible(cells[3])).toBe('5,582')
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
      'In the backtest from Farvardin 1394, a band built this way held the actual price about 80% of the time at 1–12 months, about 86% of the time at 13–24 months and about 90% of the time at 24 months.'
    )
    // The only percentages are measured hit rates; the quantiles' nominal share is never stated.
    expect(label.match(/\d+%/g)).toEqual(['80%', '86%', '90%'])
    expect(label).not.toMatch(/95%|confiden|probability of|nominal/i)
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
    // The measured rates, with the few independent outcomes the 24-month one
    // rests on, and no comparison with the first year either way.
    expect(note.textContent).toBe(
      '24 months: in the backtest the band held the actual price about 86% of the time at 13–24 months and about 90% of the time at 24 months (79 overlapping origins, about 3 independent outcomes). So few independent outcomes cannot say whether it holds more or less often that far out than within a year.'
    )
    expect(note.textContent).not.toMatch(/(more|less) often than within 12 months/)
    expect(note.className).toContain('muted')
    expect(screen.getByTestId('lq-dollar-legend').textContent).not.toContain('12 months after the last close')
    const table = screen.getByTestId('lq-scenario-table')
    expect(within(table).queryByRole('columnheader', { name: '24 months' })).toBeNull()

    fireEvent.click(within(horizon).getByRole('button', { name: '24 months' }))
    await waitFor(() => expect(within(screen.getByTestId('lq-scenario-table')).getByRole('columnheader', { name: '24 months' })).toBeInTheDocument())
    expect(apiMock).toHaveBeenCalledWith('/liquidity/dollar?horizon=24', expect.anything())
    // With the longer view on screen, its coverage is said in the text ink.
    expect(screen.getByTestId('lq-horizon-note').className).toContain('lq-coverage-strong')
    // …and the chart marks where the twelve months end.
    expect(screen.getByTestId('lq-dollar-legend').textContent).toContain('12 months after the last close')
    // The API's own band edges at 24 months.
    expect(cellsOf('lq-scenario-last_12m')[5]).toBe('621,650453,416 – 1,469,087')
  })

  it('tabulates every path at 3, 6 and 12 months with the band, and the trend apart', async () => {
    await renderPage()
    const s1 = cellsOf('lq-scenario-last_12m')
    expect(s1[0]).toContain('Path 1: M2 growth over the latest 12 months')
    expect(s1[0]).toContain('M2 growth 1403-12→1404-12')
    expect(s1[1]).toBe('53.3%')
    // 12 months: the path and the API's band around it (311k–860k).
    expect(s1[4]).toBe('405,595311,301 – 860,294')
    const trend = cellsOf('lq-scenario-trend')
    expect(trend[0]).toContain('a comparison line, not a liquidity path')
    expect(trend[1]).toBe('39.6%')
    expect(trend[4]).toBe('369,408')
    // How far apart the paths end against how wide the band is — from the numbers.
    expect(screen.getByTestId('lq-spread').textContent).toBe(
      ' At 12 months the paths end 80,682 apart, while the band around one of them spans 548,994: the uncertainty the backtest measured is wider than the difference between the scenarios.'
    )
  })

  it('lets the reader choose which path the band surrounds when it applies to each', async () => {
    await renderPage()
    const pick = screen.getByTestId('lq-band-pick')
    expect(within(pick).getByRole('button', { name: 'Path 1' })).toHaveAttribute('aria-pressed', 'true')
    expect(cellsOf('lq-scenario-slowest_three_year')[4]).toBe('324,913')
    fireEvent.click(within(pick).getByRole('button', { name: 'Path 3' }))
    expect(screen.getByTestId('lq-band-label').textContent).toContain('around path 3')
    expect(cellsOf('lq-scenario-slowest_three_year')[4]).toBe('324,913249,376 – 689,163')
    expect(cellsOf('lq-scenario-last_12m')[4]).toBe('405,595')
  })

  it('marks the cells whose text wraps, so the values beside them stay on screen', () => {
    // styles.css wraps `.lq-table td.lq-scn-name` and `.lq-table td.lq-wrap`
    // (a bare .lq-scn-name lost to .table td's nowrap and pushed every value
    // off the card; checked in the browser at 1280 and 375 px).
    expect(liquiditySource).toContain('<td className="lq-scn-name">')
    expect(liquiditySource).toContain('<td className="lq-wrap">')
  })

  it('offers no choice when the API names the path', async () => {
    await renderPage({ d12: { ...D12, band: { ...D12.band!, applied_to: 'three_year' } } })
    expect(screen.queryByTestId('lq-band-pick')).toBeNull()
    expect(screen.getByTestId('lq-band-label').textContent).toContain('around path 2')
  })

  it('names the anchor, the money month and the scenario label', async () => {
    await renderPage()
    // What the close is comes from the splice register: on this database
    // USD_IRT turns into the USDT/toman market on 2026-10-11, the day of its
    // backfill — not production's 2022-04.
    expect(screen.getByTestId('lq-anchor').textContent).toBe(
      'Every path starts at the last stored close, 264,630 تومان on 1405/07/19 — free-market rate: the 24/7 USDT/toman ' +
        "market used as the free-market proxy, which carries its own stablecoin premium over cash dollars (USD_IRT's " +
        'definition since 2026-10-11). Their growth rates are read from Esfand 1404, the newest liquidity month stored ' +
        '(workbook saved 1405/03/31).'
    )
    expect(screen.getByTestId('lq-dollar-section').textContent).not.toContain('2022-04')
    expect(screen.getByTestId('lq-dollar').textContent).toContain('مسیر هم‌پای رشد نقدینگی — سناریو، نه پیش‌بینی')
    const legend = screen.getByTestId('lq-dollar-legend').textContent ?? ''
    // Its newest point is the running month's latest close, and says so.
    expect(legend).toContain('Dollar, month-end close (Mehr 1405, still running: its latest close)')
    expect(legend).toContain('Path 3: Slowest 3-year M2 growth since 1390 (22.8% a year)')
    expect(legend).toContain('The dollar’s own trend since 1390-10 (39.6% a year)')
    expect(legend).toContain('Band around path 1')
  })
})

describe('the dollar per unit of liquidity and the liquidity dollar', () => {
  it('states the index’s base and range', async () => {
    await renderPage()
    expect(screen.getByTestId('lq-index-caption').textContent).toContain(
      'as an index equal to 100 at Esfand 1390. Lowest 52.2 (Shahrivar 1396), highest 199.2 (Bahman 1404), newest 194.8 (Esfand 1404).'
    )
  })

  it('shows the liquidity dollar as a range over base months, with its caveat', async () => {
    await renderPage()
    const card = screen.getByTestId('lq-liquidity-dollar')
    expect(card.textContent).toContain(
      'at Esfand 1404, across 110 base months, it runs from 43,573 تومان to 166,208 تومان. It is arithmetic about the month you pick — not a fair value, and not a forecast.'
    )
    const rows = within(screen.getByTestId('lq-ld-table')).getAllByRole('row').slice(1)
    // The median of 110 bases: the mean of the two middle ones.
    expect(rows.map((r) => r.textContent)).toEqual([
      'LowestShahrivar 139643,573 تومان',
      'MedianAzar 1400 and Azar 1402 (the mean of the two middle values)104,421 تومان',
      'HighestBahman 1404166,208 تومان'
    ])
    expect(screen.getByTestId('lq-ld-caveat').textContent).toBe(
      `The dollar’s actual average in Esfand 1404 was 162,495 تومان. ${D12.liquidity_dollar!.note}`
    )
    expect(D12.liquidity_dollar!.note).toMatch(/^Not a fair value\./)
  })
})

describe('the backtest card', () => {
  it('leads with the API’s verdict, then says what its numbers show', async () => {
    await renderPage()
    const verdict = screen.getByTestId('lq-verdict').textContent ?? ''
    expect(verdict).toBe(D12.backtest!.verdict)
    // A failure to reject, said as one — never "added no skill".
    expect(verdict).toContain(
      'against the dollar\'s own trend it was not measurably better at any horizon tested (RMSE 0.8% to 6.0% lower, one-sided p = 0.186 to 0.303)'
    )
    const items = within(screen.getByTestId('lq-statements')).getAllByRole('listitem').map((li) => li.textContent)
    expect(items).toEqual([
      'Against assuming no change: measurably smaller misses at every horizon tested, 3, 6, 12 and 24 months (p ≤ 0.002).',
      'Against the dollar’s own past trend: no measurable difference at any horizon tested, 3, 6, 12 and 24 months (p 0.19–0.30).',
      'In this test, pacing the dollar with liquidity growth was not measurably better than the dollar’s own trend: its typical miss was 0.8–6.0% smaller, a difference the test could not tell from chance.'
    ])
    // The error table. Its smallest figures are the liquidity-paced path's,
    // but not measurably smaller than the trend's: nothing is in bold.
    const paced = within(screen.getByTestId('lq-rmse-m2_paced')).getAllByRole('cell')
    expect(paced.map((c) => c.textContent)).toEqual(['0.160', '0.236', '0.339', '0.418'])
    expect(screen.getByTestId('lq-rmse').querySelectorAll('.lq-best')).toHaveLength(0)
    expect(within(screen.getByTestId('lq-rmse-random_walk')).getAllByRole('cell')[3].textContent).toBe('0.803')
    expect(screen.getByTestId('lq-backtest').textContent).toContain('Target: the month-end close.')
  })

  it('says how the backtest was run, with the lag it assumed and the one CBI took', async () => {
    await renderPage()
    const method = screen.getByTestId('lq-backtest-method').textContent ?? ''
    expect(method).toContain('No figure is in bold: at no horizon was the smallest measurably smaller than the others (p = 0.05).')
    expect(method).toContain(
      'Forecasts were made at each month from Farvardin 1394 and scored where the outcome was known by Shahrivar 1405 — ' +
        '135 origins at 3 months, 132 origins at 6 months, 126 origins at 12 months, 114 origins at 24 months.'
    )
    expect(method).toContain(
      'Each read liquidity as stored today, taken as known a fixed 3 months after its month ended; CBI’s workbooks actually arrived 21 to 164 days after the month, so for some origins that is early.'
    )
    expect(method).not.toContain('as it would have been known')
    // The rerun at six months, from the API's own numbers.
    expect(screen.getByTestId('lq-lag-check').textContent).toBe(`At a 6-month lag. ${D12.backtest!.lag_check!.note}`)
    expect(D12.backtest!.lag_check!.same_conclusions).toBe(true)
  })

  it('says something else when the numbers say something else', async () => {
    const bt = D12.backtest!
    const changed: LiquidityDollarResponse = {
      ...D12,
      backtest: {
        ...bt,
        rmse: { ...bt.rmse, m2_paced: { ...bt.rmse.m2_paced, '24': 0.5 } },
        dm_vs_trend_p: { '3': 0.01, '6': 0.02, '12': 0.03, '24': 0.04 }
      }
    }
    await renderPage({ d12: changed })
    const items = within(screen.getByTestId('lq-statements')).getAllByRole('listitem').map((li) => li.textContent)
    expect(items[1]).toBe(
      'Against the dollar’s own past trend: measurably smaller misses at every horizon tested, 3, 6, 12 and 24 months (p ≤ 0.04).'
    )
    expect(items[2]).toContain('added measurable skill over the dollar’s own trend at every horizon tested')
    // Bold where a figure is measurably the smallest, and only there.
    const paced = within(screen.getByTestId('lq-rmse-m2_paced')).getAllByRole('cell')
    expect(paced.map((c) => c.className.includes('lq-best'))).toEqual([true, true, true, false])
    // At 24 months the trend's error is the smaller one, not measurably so.
    expect(within(screen.getByTestId('lq-rmse-dollar_trend')).getAllByRole('cell')[3].className).not.toContain('lq-best')
    expect(screen.getByTestId('lq-backtest-method').textContent).toContain('A figure is in bold where it was measurably the smallest')
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
      'No scenario path is drawn: each is paced by a liquidity growth rate, and no CBI liquidity figure is stored. ' +
        'CBI_LIQUIDITY is not registered on this deployment: the CBI monetary ingest has not run yet.'
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
    expect(screen.queryByTestId('lq-scenario-slowest_three_year')).toBeNull()
  })

  it('names a scenario whose growth could not be measured', async () => {
    const scenarios = D12.scenarios.map((s) =>
      s.key === 'slowest_three_year' ? { ...s, annual_growth_pct: null, path: [] } : s
    )
    await renderPage({ d12: { ...D12, scenarios } })
    expect(screen.getByTestId('lq-scenario-missing').textContent).toBe(
      '2 of the three scenario paths could be drawn. Slowest 3-year M2 growth since 1390: its liquidity growth could not be measured from the stored months.'
    )
    expect(cellsOf('lq-scenario-slowest_three_year').slice(1)).toEqual(['—', '—', '—', '—'])
    expect(screen.getByTestId('lq-dollar-legend').textContent).not.toContain('Path 3')
  })

  it('keeps each path’s number and colour when the one before it cannot be drawn', async () => {
    const scenarios = D12.scenarios.map((s) => (s.key === 'three_year' ? { ...s, annual_growth_pct: null, path: [] } : s))
    await renderPage({ d12: { ...D12, scenarios } })
    const pick = screen.getByTestId('lq-band-pick')
    expect(within(pick).getAllByRole('button').map((b) => b.textContent)).toEqual(['Path 1', 'Path 3'])
    fireEvent.click(within(pick).getByRole('button', { name: 'Path 3' }))
    expect(screen.getByTestId('lq-band-label').textContent).toContain('around path 3')
    const legend = screen.getByTestId('lq-dollar-legend').textContent ?? ''
    expect(legend).toContain('Path 3: Slowest 3-year M2 growth since 1390')
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
      'The newest dollar close stored is 1405/07/19, 9 days ago: every path starts from it.'
    )
  })

  it('reads the dollar close’s age from the API’s per-input block too', async () => {
    const data_age = {
      money: { stale: true, age_days: 205 },
      usd: { newest_day: '2026-10-01', age_days: 9, stale: true, warning: 'The scenarios start from a stale price.' }
    }
    await renderPage({ d12: { ...D12, data_age } })
    expect(screen.getByTestId('lq-dollar-stale').textContent).toBe(
      'The newest dollar close stored is 1405/07/19, 9 days ago: every path starts from it. The scenarios start from a stale price.'
    )
  })

  it('stays quiet when only the money block is old', async () => {
    // As captured: the close is today's, the liquidity month 205 days old.
    expect(D12.data_age.usd!.stale).toBe(false)
    expect(D12.data_age.money!.stale).toBe(true)
    await renderPage()
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

  it('never takes the monthly answer for an annual one while the annual is on its way', async () => {
    // The annual request never answers: the page waits, and says nothing is missing.
    apiMock.mockImplementation((path: string) =>
      path === '/liquidity/aggregates?freq=annual' ? new Promise(() => {}) : route({})(path)
    )
    render(
      <MemoryRouter>
        <SettingsProvider>
          <Liquidity />
        </SettingsProvider>
      </MemoryRouter>
    )
    await screen.findByTestId('lq-table')
    fireEvent.click(within(screen.getByTestId('lq-freq')).getByRole('button', { name: 'Annual' }))
    expect(screen.queryByTestId('lq-annual-missing')).toBeNull()
    expect(screen.queryByTestId('lq-table')).toBeNull()
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
    expect(notes).toContain('Its monthly workbooks have arrived 21 to 164 days after the month they describe')
    expect(notes).not.toMatch(/\b137\b|four and a half/)
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
