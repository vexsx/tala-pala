import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import Stocks from '../pages/Stocks'
import { SettingsProvider } from '../lib/settings'
// The page's own source. Two rules on this screen are properties of the FILE
// rather than of any one render — that it never applies the toman→rial ×10 to
// figures that are already rials, and that it holds no copy of its own about
// the absent columns — and a source assertion is the only way they cannot
// quietly drift back.
import stocksSource from '../pages/Stocks.tsx?raw'
import appSource from '../App.tsx?raw'
import type { StockScreenResponse } from '../api/types'

// The fixtures are emitted by backend-go/internal/equities' own
// buildScreenResponse + encoding/json over roster metadata captured from
// production GET /api/v1/stocks — see fixtures/README.md. Nothing about their
// shape is hand-written, which is why they are cast rather than constructed:
// TypeScript infers a JSON module's literal types, and the cast asserts that
// the payload the SERVER produces satisfies the interface the page reads.
import screen1yIrr from './fixtures/stocks-screen-1y-irr.json'
import screen3mUsd from './fixtures/stocks-screen-3m-usd.json'
import screenNoGold from './fixtures/stocks-screen-1y-nogold.json'

const IRR = screen1yIrr as unknown as StockScreenResponse
const USD = screen3mUsd as unknown as StockScreenResponse
const NO_GOLD = screenNoGold as unknown as StockScreenResponse

vi.mock('../api/client', () => ({
  api: vi.fn(() => new Promise(() => undefined)),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : 'Unexpected error')
}))
import { api } from '../api/client'

const apiMock = api as unknown as Mock

function mockApi(payload: StockScreenResponse = IRR): void {
  apiMock.mockImplementation((path: string) => {
    if (path.startsWith('/stocks/screen')) return Promise.resolve(payload)
    return Promise.resolve({})
  })
}

async function renderPage(payload: StockScreenResponse = IRR) {
  mockApi(payload)
  const result = render(
    <SettingsProvider>
      <Stocks />
    </SettingsProvider>
  )
  await screen.findByTestId(`stk-row-${payload.items[0].symbol}`)
  return result
}

function paths(): string[] {
  return apiMock.mock.calls.map((c: unknown[]) => String(c[0]))
}

function lastPath(): string {
  const all = paths()
  return all[all.length - 1]
}

/** The order the rows are actually painted in, top to bottom. */
function rowSymbols(): string[] {
  return screen
    .getAllByTestId(/^stk-row-/)
    .map((el) => String(el.getAttribute('data-testid')).replace('stk-row-', ''))
}

function itemBySymbol(payload: StockScreenResponse, symbol: string) {
  const found = payload.items.find((i) => i.symbol === symbol)
  if (!found) throw new Error(`fixture has no row for ${symbol}`)
  return found
}

beforeEach(() => {
  window.localStorage.clear()
  apiMock.mockReset()
})

// --- the fixture is worth asserting about before anything is asserted WITH it -

describe('Stocks — the fixtures describe a payload the server can emit', () => {
  it('carries the roster the production capture carried, minus the excluded symbol', () => {
    expect(IRR.count).toBe(19)
    expect(IRR.excluded_count).toBe(1)
    expect(IRR.excluded[0].symbol).toBe('کچاد')
    expect(IRR.currency).toBe('IRR')
  })

  it('pairs every null figure with a non-empty reason, as the contract requires', () => {
    for (const item of IRR.items) {
      const pairs: Array<[number | null, string | null]> = [
        [item.return_pct, item.return_reason],
        [item.volatility_pct, item.volatility_reason],
        [item.max_drawdown_pct, item.max_drawdown_reason],
        [item.avg_value, item.liquidity_reason],
        [item.price_usd, item.price_usd_reason],
        [item.price_gold_grams, item.price_gold_grams_reason]
      ]
      for (const [value, reason] of pairs) {
        if (value === null) expect((reason ?? '').length).toBeGreaterThan(0)
        else expect(reason).toBeNull()
      }
    }
  })
})

// --- sorting -----------------------------------------------------------------

describe('Stocks — sorting', () => {
  it('opens in the order the server sorted it: turnover, highest first', async () => {
    await renderPage()

    const turnovers = rowSymbols().map((s) => itemBySymbol(IRR, s).avg_value ?? -Infinity)
    for (let i = 1; i < turnovers.length; i++) {
      expect(turnovers[i - 1]).toBeGreaterThanOrEqual(turnovers[i])
    }
  })

  it('re-sorts on the clicked column, and flips direction on a second click', async () => {
    await renderPage()

    fireEvent.click(screen.getByRole('button', { name: /^Return/ }))
    const desc = rowSymbols().map((s) => itemBySymbol(IRR, s).return_pct)
    const measuredDesc = desc.filter((v): v is number => v !== null)
    expect(measuredDesc.length).toBeGreaterThan(1)
    for (let i = 1; i < measuredDesc.length; i++) {
      expect(measuredDesc[i - 1]).toBeGreaterThanOrEqual(measuredDesc[i])
    }

    fireEvent.click(screen.getByRole('button', { name: /^Return/ }))
    const asc = rowSymbols()
      .map((s) => itemBySymbol(IRR, s).return_pct)
      .filter((v): v is number => v !== null)
    for (let i = 1; i < asc.length; i++) {
      expect(asc[i - 1]).toBeLessThanOrEqual(asc[i])
    }
    expect(asc[0]).toBe(measuredDesc[measuredDesc.length - 1])
  })

  it('keeps an un-measured row at the bottom in BOTH directions', async () => {
    await renderPage()
    // ذوب traded once in this window: its return could not be computed, so it
    // is not the worst performer — it is not on the scale at all.
    expect(itemBySymbol(IRR, 'ذوب').return_pct).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /^Return/ }))
    expect(rowSymbols()[rowSymbols().length - 1]).toBe('ذوب')

    fireEvent.click(screen.getByRole('button', { name: /^Return/ }))
    expect(rowSymbols()[rowSymbols().length - 1]).toBe('ذوب')
  })

  it('marks the sorted column for assistive technology', async () => {
    await renderPage()

    fireEvent.click(screen.getByRole('button', { name: /^Return/ }))
    const header = screen.getByRole('columnheader', { name: /^Return/ })
    expect(header).toHaveAttribute('aria-sort', 'descending')
    fireEvent.click(screen.getByRole('button', { name: /^Return/ }))
    expect(header).toHaveAttribute('aria-sort', 'ascending')
  })
})

// --- the selectors -----------------------------------------------------------

describe('Stocks — the selectors issue the right query', () => {
  it('asks for the endpoint defaults on first paint, with no sector filter', async () => {
    await renderPage()

    expect(paths()[0]).toBe('/stocks/screen?period=1y&numeraire=IRR')
    expect(paths()[0]).not.toContain('sector=')
  })

  it('sends the chosen window', async () => {
    await renderPage()

    fireEvent.click(screen.getByRole('button', { name: '3m' }))
    await waitFor(() => expect(lastPath()).toContain('period=3m'))
    expect(lastPath()).toBe('/stocks/screen?period=3m&numeraire=IRR')
  })

  it('sends the chosen numéraire', async () => {
    await renderPage()

    fireEvent.change(screen.getByLabelText('Measured in'), { target: { value: 'GOLD' } })
    await waitFor(() => expect(lastPath()).toContain('numeraire=GOLD'))
    expect(lastPath()).toBe('/stocks/screen?period=1y&numeraire=GOLD')
  })

  it('sends the sector CODE, which is what the endpoint accepts', async () => {
    await renderPage()

    const select = screen.getByLabelText('Sector')
    // The options are the roster's own vocabulary, echoed by the server.
    for (const sector of IRR.sectors) {
      expect(within(select).getByText(new RegExp(sector.sector_fa))).toBeInTheDocument()
    }
    fireEvent.change(select, { target: { value: '27' } })
    await waitFor(() => expect(lastPath()).toContain('sector=27'))
    expect(lastPath()).toBe('/stocks/screen?period=1y&numeraire=IRR&sector=27')

    fireEvent.change(select, { target: { value: '' } })
    await waitFor(() => expect(lastPath()).not.toContain('sector='))
  })

  it('labels the table with the numéraire the SERVER served, not the pending one', async () => {
    await renderPage(USD)

    expect(screen.getByText(/measured in USD/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Return \(3 months\)/ })).toBeInTheDocument()
  })
})

// --- the absent columns ------------------------------------------------------

describe('Stocks — the metrics this data cannot support', () => {
  it('renders each absent metric as a dash carrying the API’s own reason, never a 0', async () => {
    await renderPage()

    const first = IRR.items[0]
    expect(first.absent_metrics.length).toBeGreaterThan(0)

    for (const entry of first.absent_metrics) {
      // A column, so a reader scanning for it finds the heading.
      expect(screen.getByTestId(`stk-absent-col-${entry.metric}`)).toHaveTextContent(entry.label)

      const cell = screen.getByTestId(`stk-absent-${first.symbol}-${entry.metric}`)
      expect(cell.textContent?.trim()).toBe('—')
      expect(cell.textContent).not.toMatch(/\d/)
      expect(within(cell).getByTitle(new RegExp(escapeRegExp(entry.reason.slice(0, 60))))).toHaveAttribute(
        'data-absent',
        'true'
      )

      // And the reason in full, in the API's words rather than the page's.
      const block = screen.getByTestId(`stk-absent-reason-${entry.metric}`)
      expect(block.textContent).toContain(entry.reason)
      expect(block.textContent).toContain(entry.requires)
    }
  })

  it('names P/E specifically, with the deliberate omission of TSETMC’s estimatedEPS', async () => {
    await renderPage()

    const block = screen.getByTestId('stk-absent-reason-pe_ratio')
    expect(block.textContent).toContain('estimatedEPS')
    expect(block.textContent).not.toMatch(/coming soon/i)
    expect(block.textContent).not.toMatch(/not available\.?$/i)
  })

  it('holds no copy of its own about the boundary, so it cannot drift from the ingest', () => {
    // The obstacles — Codal, XBRL, fipiran, zTitad — belong to the API's
    // absent_metrics. If any of them appear in this file, the page has started
    // asserting something nothing keeps in step with the backend.
    for (const word of ['Codal', 'XBRL', 'fipiran', 'zTitad', 'estimatedEPS']) {
      expect(stocksSource).not.toContain(word)
    }
  })

  it('shows a metric that could not be MEASURED the same way: a dash and the reason', async () => {
    await renderPage()

    const item = itemBySymbol(IRR, 'ذوب')
    const cell = screen.getByTestId('stk-return-ذوب')
    expect(cell.textContent?.trim()).toBe('—')
    expect(cell.textContent).not.toContain('0')
    expect(within(cell).getByTitle(item.return_reason as string)).toBeInTheDocument()

    const vol = screen.getByTestId('stk-volatility-ذوب')
    expect(within(vol).getByTitle(item.volatility_reason as string)).toBeInTheDocument()
    expect(vol.textContent).not.toContain('0.00')
  })
})

// --- the numéraire columns ---------------------------------------------------

describe('Stocks — a price that could not be re-expressed', () => {
  it('shows the numéraire engine’s own reason where a gold price is null', async () => {
    await renderPage(NO_GOLD)

    const item = NO_GOLD.items[0]
    expect(item.price_gold_grams).toBeNull()
    const reason = item.price_gold_grams_reason as string
    expect(reason).toContain('IR_GOLD_18K')

    const cell = screen.getByTestId(`stk-gold-${item.symbol}`)
    expect(cell.textContent?.trim()).toBe('—')
    expect(within(cell).getByTitle(reason)).toHaveAttribute('data-absent', 'true')

    // The dollar column is unaffected: this deployment has USD_IRT.
    const usd = screen.getByTestId(`stk-usd-${item.symbol}`)
    expect(usd.textContent).toContain('$')
  })

  it('prints a fractional-cent share price instead of rounding it to $0.00', async () => {
    await renderPage()

    const item = IRR.items[0]
    const usd = item.price_usd as number
    expect(usd).toBeLessThan(0.01)
    expect(screen.getByTestId(`stk-usd-${item.symbol}`).textContent).not.toBe('$0.00')
  })
})

// --- the adjustment ----------------------------------------------------------

describe('Stocks — every row states which adjustment produced its return', () => {
  it('shows the version and the action count on every screened row', async () => {
    await renderPage()

    for (const item of IRR.items) {
      const cell = screen.getByTestId(`stk-adjustment-${item.symbol}`)
      expect(cell.textContent).toContain(item.adjustment.version)
      expect(cell.textContent).toContain(`${item.adjustment.actions_applied} action`)
    }
    expect(IRR.items[0].adjustment.version).toBe('priceYesterday-chain-v1')
  })

  it('says at the top of the table that the returns are adjusted and the last close is not', async () => {
    await renderPage()

    const note = screen.getByTestId('stk-adjusted-note')
    expect(note.textContent).toContain('adjusted closes')
    expect(note.textContent).toMatch(/Only the last close is\s+raw/)
  })

  it('publishes the server’s measurement basis rather than a sentence of its own', async () => {
    await renderPage()

    fireEvent.click(screen.getByRole('button', { name: /How these are measured/ }))
    const basis = screen.getByTestId('stk-basis')
    expect(basis.textContent).toContain(IRR.price_basis.return_basis)
    expect(basis.textContent).toContain(IRR.price_basis.volatility_basis)
    expect(basis.textContent).toContain(IRR.price_basis.liquidity_basis)
  })
})

// --- the universe ------------------------------------------------------------

describe('Stocks — the roster is 20 and the table shows 19', () => {
  it('renders the excluded symbol with the reason and the measurement behind it', async () => {
    await renderPage()

    const block = screen.getByTestId('stk-excluded')
    expect(block.textContent).toContain(IRR.excluded_note)

    const row = screen.getByTestId('stk-excluded-کچاد')
    expect(row.textContent).toContain('کچاد')
    expect(row.textContent).toContain(IRR.excluded[0].reason)
    // The roster's own note is the evidence: a block trade of 227,000,010
    // shares that set the official close to 5,576 against a market of ~14,090.
    expect(row.textContent).toContain(IRR.excluded[0].notes)
    expect(row.textContent).toContain('disabled')
  })

  it('accounts for the whole universe on screen: items + excluded', async () => {
    await renderPage()

    expect(rowSymbols()).toHaveLength(IRR.count)
    expect(
      screen.getByText(
        new RegExp(`${IRR.count} of ${IRR.count + IRR.excluded_count} roster symbols`)
      )
    ).toBeInTheDocument()
  })

  it('shows the server’s warning that the fundamentals are not computable', async () => {
    await renderPage()

    for (const warning of IRR.warnings) {
      expect(screen.getByText(warning)).toBeInTheDocument()
    }
  })
})

// --- the unit ----------------------------------------------------------------

describe('Stocks — the currency is rial and the app-wide toggle does not touch it', () => {
  it('renders identical digits whichever way the toman/rial toggle is set', async () => {
    const item = IRR.items[0]

    window.localStorage.setItem('igp_unit', 'IRT')
    const first = await renderPage()
    const inToman = screen.getByTestId(`stk-last-${item.symbol}`).textContent
    const turnoverToman = screen.getByTestId(`stk-turnover-${item.symbol}`).textContent

    // One tree at a time: the settings provider reads the unit once, at mount.
    first.unmount()
    apiMock.mockReset()
    window.localStorage.setItem('igp_unit', 'IRR')
    await renderPage()
    expect(screen.getByTestId(`stk-last-${item.symbol}`).textContent).toBe(inToman)
    expect(screen.getByTestId(`stk-turnover-${item.symbol}`).textContent).toBe(turnoverToman)
  })

  it('prints the raw rial close, not that number multiplied by ten', async () => {
    const item = IRR.items[0]
    window.localStorage.setItem('igp_unit', 'IRR')
    await renderPage()

    const cell = screen.getByTestId(`stk-last-${item.symbol}`)
    const close = item.last_close as number
    expect(cell.textContent).toContain(new Intl.NumberFormat('en-US').format(close))
    expect(cell.textContent).not.toContain(new Intl.NumberFormat('en-US').format(close * 10))
  })

  it('states the unit from the response, and that the toggle does not apply', async () => {
    await renderPage()

    const note = screen.getByTestId('stk-currency')
    expect(note.textContent).toContain('IRR')
    expect(note.textContent).toContain(IRR.currency_note)
    expect(note.textContent).toMatch(/toggle does not apply/)
  })

  it('never imports the toman converters that caused the ×10 defect elsewhere', () => {
    // formatToman and convertDisplay apply the app-wide toman→rial ×10 to a
    // CANONICAL TOMAN value. Every amount here is already rial.
    expect(stocksSource).not.toMatch(/\bformatToman\b\s*\(/)
    expect(stocksSource).not.toMatch(/\bconvertDisplay\b\s*\(/)
    expect(stocksSource).not.toMatch(/\bformatCompactToman\b/)
  })
})

// --- the wiring --------------------------------------------------------------

describe('Stocks — the route exists as well as the nav entry', () => {
  it('gives every nav item a route, /stocks included', () => {
    const navTargets = Array.from(appSource.matchAll(/\{ to: '([^']+)'/g)).map((m) => m[1])
    const routePaths = Array.from(appSource.matchAll(/<Route path="([^"]+)"/g)).map((m) => m[1])

    expect(navTargets).toContain('/stocks')
    expect(routePaths).toContain('/stocks')
    for (const target of navTargets) {
      if (target === '/') continue
      expect(routePaths).toContain(target)
    }
  })
})

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

// Both of these were found by adversarial review against the real payload shapes.
describe('Stocks — a null must never render as a measurement', () => {
  it('does not claim "0 shares in 0 trades" for an instrument with no traded session', async () => {
    const payload = JSON.parse(JSON.stringify(IRR)) as StockScreenResponse
    payload.items[0] = {
      ...payload.items[0],
      last_close: null,
      last_volume: null,
      last_trade_count: null,
      last_value: null
    }
    await renderPage(payload)

    const row = screen.getByTestId(`stk-row-${payload.items[0].symbol}`)
    const toggle = within(row).queryByRole('button', { name: /Why\?/ })
    if (toggle) fireEvent.click(toggle)
    // 9,521 of the 77,344 stored bars are halts, and نوری carried 161 pre-listing
    // placeholders, so a roster instrument really can have no traded session.
    expect(row.textContent ?? '').not.toMatch(/0 shares in 0 trades/)
  })

  it('disables a numéraire this deployment cannot back, instead of erasing the page', async () => {
    // NO_GOLD is a captured payload from a deployment with no IR_GOLD_18K series.
    await renderPage(NO_GOLD)

    const menu = screen.getByLabelText(/Measured in/i) as HTMLSelectElement
    const gold = Array.from(menu.options).find((o) => o.value === 'GOLD')!
    expect(gold.disabled).toBe(true)
    expect(gold.title.length).toBeGreaterThan(0)
    expect(Array.from(menu.options).find((o) => o.value === 'IRR')!.disabled).toBe(false)
  })
})
