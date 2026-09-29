import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import MoneyFlow, { HEAT_CLASSES, heatClass, sectorLabel, signedPp, signedToman, sortSectors } from '../pages/MoneyFlow'
import { SettingsProvider } from '../lib/settings'
import moneyFlowSource from '../pages/MoneyFlow.tsx?raw'
import bourseSource from '../pages/Bourse.tsx?raw'
import type { SectorFlowSharesResponse, SectorFlowsResponse } from '../api/types'

// HAND-BUILT, pending a production capture — see fixtures/README.md. The
// inputs (a 291-share market over 125 Tehran sessions with the 2026 closure
// left out, one whole-market day file missing, a capital increase on ذوب, a
// halted share, a delisted one, فولاد's block and second boards) are invented;
// every field of the payloads was emitted by the Go code that serves the route
// (buildSectorFlows / buildSectorShares), so the shape is the server's own.
import flowsJson from './fixtures/bourse-sector-flows.json'
import metals5Json from './fixtures/bourse-sector-flows-27-w5.json'
import rosterOnlyJson from './fixtures/bourse-sector-flows-roster-only.json'

const FLOWS = flowsJson as SectorFlowsResponse
const METALS_5 = metals5Json as SectorFlowSharesResponse
const ROSTER_ONLY = rosterOnlyJson as SectorFlowsResponse

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, api: vi.fn() }
})
const { api } = await import('../api/client')
const apiMock = api as unknown as Mock

function routeTo(main: SectorFlowsResponse) {
  return (path: string): Promise<unknown> => {
    if (path === '/bourse/sector-flows') return Promise.resolve(main)
    if (path === '/bourse/sector-flows/27?window=5') return Promise.resolve(METALS_5)
    return Promise.reject(new Error(`unexpected path ${path}`))
  }
}

async function renderPage(main: SectorFlowsResponse = FLOWS, ready = 'mf-sector-table') {
  apiMock.mockImplementation(routeTo(main))
  const result = render(
    <MemoryRouter>
      <SettingsProvider>
        <MoneyFlow />
      </SettingsProvider>
    </MemoryRouter>
  )
  await screen.findByTestId(ready)
  return result
}

function sectorRowOrder(): string[] {
  const table = screen.getByTestId('mf-sector-table')
  return within(table)
    .getAllByRole('row')
    .map((r) => r.getAttribute('data-testid'))
    .filter((id): id is string => id !== null && id.startsWith('mf-sector-'))
    .map((id) => id.replace('mf-sector-', ''))
}

beforeEach(() => {
  apiMock.mockReset()
  window.localStorage.setItem('igp_calendar', 'gregorian')
})

// ---------------------------------------------------------------------------
// The helpers
// ---------------------------------------------------------------------------

describe('the diverging scale', () => {
  it('bins net flow at fixed thresholds with a neutral middle', () => {
    expect(heatClass(0.99)).toBe('mf-h-0')
    expect(heatClass(-0.99)).toBe('mf-h-0')
    expect(heatClass(1)).toBe('mf-h-p1')
    expect(heatClass(-3)).toBe('mf-h-n2')
    expect(heatClass(5.99)).toBe('mf-h-p2')
    expect(heatClass(6)).toBe('mf-h-p3')
    expect(heatClass(-40)).toBe('mf-h-n3')
    expect(heatClass(null)).toBe('mf-h-na')
    // Seven classes, one gray between the two hues.
    expect(HEAT_CLASSES.map((c) => c.cls)).toEqual(['mf-h-n3', 'mf-h-n2', 'mf-h-n1', 'mf-h-0', 'mf-h-p1', 'mf-h-p2', 'mf-h-p3'])
  })

  it('states amounts signed, with their unit', () => {
    expect(signedToman(1.2e12)).toBe('+1.2T toman')
    expect(signedToman(-921545500297.9662)).toBe('-922B toman')
    expect(signedToman(null)).toBe('—')
    expect(signedPp(0.4212)).toBe('+0.42 pp')
    expect(signedPp(-1.5)).toBe('-1.50 pp')
    expect(sectorLabel({ sector_code: '84', name_en: '' })).toBe('Sector 84')
  })

  it('never lets a missing figure sort as the smallest or the largest', () => {
    const sectors = FLOWS.sectors.slice(0, 3).map((s, i) => ({
      ...s,
      windows: { ...s.windows, '5': { ...s.windows['5'], index_return_pct: i === 1 ? null : s.windows['5'].index_return_pct } }
    }))
    for (const desc of [true, false]) {
      const sorted = sortSectors(sectors, '5', 'index', desc)
      expect(sorted[sorted.length - 1].windows['5'].index_return_pct).toBeNull()
    }
  })
})

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

describe('the market over a window', () => {
  it('opens on five sessions and states the market with its unit', async () => {
    await renderPage()
    expect(screen.getByRole('button', { name: '5 sessions' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByTestId('mf-tile-value').textContent).toContain('35.2T toman')
    const net = screen.getByTestId('mf-tile-net').textContent ?? ''
    expect(net).toContain('-926B toman')
    expect(net).toContain('-2.6% of traded value')
    // The other side of the same trades, stated beside it.
    expect(net).toContain('+926B toman')
    expect(screen.getByTestId('mf-window-span').textContent).toContain('2026-09-22 → 2026-09-28')
    expect(screen.getByTestId('mf-window-span').textContent).toContain('2026-09-15 → 2026-09-21')
  })

  it('switches every figure with the window chips', async () => {
    await renderPage()
    fireEvent.click(screen.getByRole('button', { name: '20 sessions' }))
    expect(screen.getByRole('button', { name: '20 sessions' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByTestId('mf-tile-value').textContent).toContain('151T toman')
    expect(screen.getByTestId('mf-tile-net').textContent).toContain('+716B toman')
    expect(screen.getByTestId('mf-window-span').textContent).toContain('2026-09-01 → 2026-09-28')
    const inflow = screen.getByTestId('mf-top-inflow')
    expect(inflow.textContent).toContain('20 sessions')
    // فولاد's three boards are one company in the twenty-session list.
    expect(within(inflow).getByText('main + block + secondary')).toBeInTheDocument()
  })

  it('shows the check tiers behind the figures', async () => {
    await renderPage()
    const tiers = screen.getByTestId('mf-tile-tiers').textContent ?? ''
    expect(tiers).toContain('1,280 of 1,284')
    expect(tiers).toContain('1,280 checked against a session value')
    expect(tiers).toContain('0 identities only')
    expect(tiers).toContain('3 excluded')
    expect(tiers).toContain('1 no trade')
    fireEvent.click(screen.getByRole('button', { name: '20 sessions' }))
    const t20 = screen.getByTestId('mf-tile-tiers').textContent ?? ''
    // The missing day file inside twenty sessions: rows checked on the
    // identities only, and said so.
    expect(t20).toContain('5,115 of 5,129')
    expect(t20).toContain('246 identities only')
    // Per sector too.
    const metals = FLOWS.sectors.find((s) => s.sector_code === '27')!.windows['20']
    const row = screen.getByTestId('mf-sector-27')
    expect(row.textContent).toContain(`${metals.bar_checked.toLocaleString('en-US')} · ${metals.identity_only} · ${metals.excluded}`)
  })

  it('disables a window the store cannot fill, with the reason', async () => {
    const reason = '30 market session(s) are stored and this window needs 60; a shorter window is not substituted for the one asked for'
    const short: SectorFlowsResponse = {
      ...FLOWS,
      sessions: { ...FLOWS.sessions, '60': { ...FLOWS.sessions['60'], available: false, reason } }
    }
    await renderPage(short)
    const chip = screen.getByRole('button', { name: '60 sessions' })
    expect(chip).toBeDisabled()
    expect(chip).toHaveAttribute('title', reason)
  })

  it('flags a newest session that looks partly ingested', async () => {
    const partial: SectorFlowsResponse = {
      ...FLOWS,
      coverage: { ...FLOWS.coverage, newest_partial: true, newest_traded_shares: 150, median_traded_prev20: 257 }
    }
    await renderPage(partial)
    expect(screen.getByTestId('mf-partial').textContent).toContain('partly ingested')
  })

  it("repeats the API's data-age warning", async () => {
    const stale: SectorFlowsResponse = {
      ...FLOWS,
      data_age: { ...FLOWS.data_age, stale: true, age_days: 15, warning: 'This market data is 15 days old.' }
    }
    await renderPage(stale)
    expect(screen.getByTestId('bx-data-age').textContent).toContain('This market data is 15 days old.')
  })
})

describe('the sector table', () => {
  it('sorts by traded value first, then by any column, nulls last', async () => {
    await renderPage()
    const byValue = [...FLOWS.sectors]
      .sort((a, b) => (b.windows['5'].total_value_toman ?? 0) - (a.windows['5'].total_value_toman ?? 0))
      .map((s) => s.sector_code)
    expect(sectorRowOrder()).toEqual(byValue)

    const table = screen.getByTestId('mf-sector-table')
    fireEvent.click(within(table).getByRole('columnheader', { name: /Net individual/ }))
    const byNet = [...FLOWS.sectors]
      .sort((a, b) => (b.windows['5'].net_individual_toman ?? 0) - (a.windows['5'].net_individual_toman ?? 0))
      .map((s) => s.sector_code)
    expect(sectorRowOrder()).toEqual(byNet)
    expect(within(table).getByRole('columnheader', { name: /Net individual/ })).toHaveAttribute('aria-sort', 'descending')
    fireEvent.click(within(table).getByRole('columnheader', { name: /Net individual/ }))
    expect(sectorRowOrder()).toEqual([...byNet].reverse())

    // Wholesale trade has no sector index: it sorts last both ways.
    fireEvent.click(within(table).getByRole('columnheader', { name: /Sector index/ }))
    expect(sectorRowOrder().slice(-1)[0]).toBe('46')
    fireEvent.click(within(table).getByRole('columnheader', { name: /Sector index/ }))
    expect(sectorRowOrder().slice(-1)[0]).toBe('46')
  })

  it('draws net flow as a diverging bar and states the rotation in points', async () => {
    await renderPage()
    const metals = FLOWS.sectors.find((s) => s.sector_code === '27')!.windows['5']
    const row = screen.getByTestId('mf-sector-27')
    expect(within(row).getByRole('img', { name: /individuals bought more/ })).toBeInTheDocument()
    expect(row.textContent).toContain(signedToman(metals.net_individual_toman))
    expect(row.textContent).toContain(signedPp(metals.value_share_change_pp))
    expect(row.textContent).toContain(`${metals.value_share_pct!.toFixed(1)}%`)
    // The value-share line: one hue, the first and last block in its label.
    expect(within(row).getByRole('img', { name: /Share of traded value by five-session block/ })).toBeInTheDocument()
  })

  it("opens a sector's shares and links only the roster", async () => {
    await renderPage()
    const row = screen.getByTestId('mf-sector-27')
    fireEvent.click(within(row).getByRole('button', { name: 'Basic metals' }))
    const shares = await screen.findByTestId('mf-shares-27')
    expect(apiMock).toHaveBeenCalledWith('/bourse/sector-flows/27?window=5', expect.anything())
    const links = within(shares).getAllByRole('link')
    const roster = METALS_5.items.filter((it) => it.in_roster)
    expect(links).toHaveLength(roster.length)
    for (const it of roster) {
      expect(within(shares).getByRole('link', { name: it.symbol })).toHaveAttribute(
        'href',
        `/stocks/${encodeURIComponent(it.roster_symbol!)}`
      )
    }
    // The block board is a row of its own, plain text, with nothing traded.
    const block = within(shares).getByTestId('mf-share-9000000000000002')
    expect(within(block).queryByRole('link')).toBeNull()
    expect(block.textContent).toContain('block')
  })
})

describe('the top lists', () => {
  it('link a company only when it is on the roster', async () => {
    await renderPage()
    for (const [id, side] of [
      ['mf-top-inflow', FLOWS.top['5'].inflow],
      ['mf-top-outflow', FLOWS.top['5'].outflow]
    ] as const) {
      const card = screen.getByTestId(id)
      expect(within(card).getAllByRole('row')).toHaveLength(side.length + 1)
      for (const c of side) {
        const row = within(card).getByTestId(`mf-company-${c.ins_code}`)
        const link = within(row).queryByRole('link')
        if (c.in_roster) {
          expect(link).toHaveAttribute('href', `/stocks/${encodeURIComponent(c.roster_symbol!)}`)
        } else {
          expect(link).toBeNull()
        }
      }
    }
    expect(FLOWS.top['5'].inflow.some((c) => !c.in_roster)).toBe(true)
  })

  it('explains an empty price change instead of inventing one', async () => {
    await renderPage()
    fireEvent.click(screen.getByRole('button', { name: '20 sessions' }))
    const missing = FLOWS.top['20'].inflow.find((c) => c.price_change_pct === null)!
    const row = screen.getByTestId(`mf-company-${missing.ins_code}`)
    const cell = within(row).getByTitle(missing.price_change_reason!)
    expect(cell.textContent).toBe('—')
  })
})

describe('the heatmap', () => {
  it('has a legend with a neutral middle, and a titled cell per block', async () => {
    await renderPage()
    const legend = screen.getByTestId('mf-heat-legend')
    for (const label of ['≤ −6%', 'within ±1%', '≥ +6%', 'individuals sold more', 'individuals bought more']) {
      expect(legend.textContent).toContain(label)
    }
    const heat = screen.getByTestId('mf-heatmap')
    const market = within(heat).getByText('Whole market').closest('tr')!
    const cells = market.querySelectorAll('td.mf-heat-cell')
    expect(cells).toHaveLength(FLOWS.blocks.length)
    // The newest block, its class from the same thresholds, its figure as text.
    const newest = FLOWS.market_blocks[FLOWS.market_blocks.length - 1]
    const last = cells[cells.length - 1]
    expect(last.className).toContain(heatClass(newest.net_individual_pct_of_value))
    expect(last.textContent).toBe(newest.net_individual_pct_of_value!.toFixed(1))
    expect(last.getAttribute('title')).toContain('Whole market, 2026-09-22 → 2026-09-28')
    expect(last.getAttribute('title')).toContain(`of ${'35.2T toman'} traded`)
    // A sector's cell also states its share of the market; the market's does not.
    expect(last.getAttribute('title')).not.toContain("of the market's traded value")
    const metals = FLOWS.sectors.find((s) => s.sector_code === '27')!
    const metalsRow = within(heat).getByText('Basic metals').closest('tr')!
    const metalsCells = metalsRow.querySelectorAll('td.mf-heat-cell')
    expect(metalsCells).toHaveLength(FLOWS.blocks.length)
    expect(metalsCells[metalsCells.length - 1].getAttribute('title')).toContain(
      `${metals.blocks[metals.blocks.length - 1].value_share_pct!.toFixed(1)}% of the market's traded value`
    )
    // Readable as a table: the sectors are its rows, the blocks its columns.
    expect(within(heat).getAllByRole('columnheader')).toHaveLength(FLOWS.blocks.length + 1)
  })
})

describe('what the page says the flow is', () => {
  it('explains net individual flow in plain words', async () => {
    await renderPage()
    const text = screen.getByTestId('mf-explainer').textContent ?? ''
    expect(text).toContain('It is not new money entering the market.')
    expect(text).toContain('institutions are net sellers of it by the same amount')
    expect(text).toContain('It is not a forecast.')
    expect(text).toContain('to within 0.1%')
    expect(text).toContain('to within 1%')
    // The API's own notes travel with it.
    expect(text).toContain(FLOWS.notes[0])
  })

  it('uses no signal or advice language', () => {
    expect(moneyFlowSource).not.toMatch(/\b(buy signal|sell signal|opportunit|strong buy|recommend)/i)
  })

  it("is linked from the roster's flow table on the Tehran market page", () => {
    const card = bourseSource.slice(bourseSource.indexOf('function FlowsCard'), bourseSource.indexOf('// --- the page'))
    expect(card).toContain('the roster, not the market')
    expect(card).toContain('<Link to="/money-flow">Money flow</Link>')
  })

  it('draws the value-share lines in one hue', () => {
    const spark = moneyFlowSource.slice(moneyFlowSource.indexOf('function ShareSpark'), moneyFlowSource.indexOf('/** The four tiers'))
    expect(spark).toContain('var(--series-1)')
    expect(spark).not.toMatch(/--series-[2-9]|--pos|--neg/)
  })
})

describe('before the market-wide flows exist', () => {
  it('says so and sums nothing, rather than passing the roster off as the market', async () => {
    await renderPage(ROSTER_ONLY, 'mf-not-ingested')
    const card = screen.getByTestId('mf-not-ingested')
    expect(card.textContent).toContain('Market-wide flows have not been ingested yet')
    expect(card.textContent).toContain('the roster, not the market')
    expect(card.textContent).toContain('The newest stored date, 2026-09-28, carries 19')
    expect(within(card).getByRole('link', { name: 'Tehran market' })).toHaveAttribute('href', '/bourse')
    expect(screen.queryByTestId('mf-sector-table')).toBeNull()
    expect(screen.queryByTestId('mf-tiles')).toBeNull()
    expect(screen.queryByTestId('mf-heatmap')).toBeNull()
    expect(screen.getByTestId('mf-explainer').textContent).toContain('Market-wide flows not ingested yet')
    await waitFor(() => expect(apiMock).toHaveBeenCalledTimes(1))
  })
})
