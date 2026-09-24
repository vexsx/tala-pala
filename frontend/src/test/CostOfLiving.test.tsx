import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { CostOfLivingCard } from '../pages/Markets'
import marketsSource from '../pages/Markets.tsx?raw'
import type { CostOfLivingBlock } from '../api/types'

// Emitted by backend-go/internal/relvalue's own buildCostOfLiving over the REAL
// SCI observations exported from production (293 monthly periods per series,
// 2002-03..2026-07), window 2013-08-01..2026-09-24. Nothing here is authored:
// the numbers, the notes and the period counts are the server's.
import block13y from './fixtures/markets-cost-of-living-13y.json'

const BLOCK = block13y as CostOfLivingBlock

describe('CostOfLivingCard — the basket is not the portfolio', () => {
  it('shows each component with its own change and its change against the basket', () => {
    render(<CostOfLivingCard block={BLOCK} calendar="gregorian" />)

    // Shelter rose 1430% in cash and still FELL 53% against the basket. Both
    // numbers must be on screen: either alone tells the reader the opposite
    // story from the other.
    const shelter = screen.getByTestId('col-growth-SCI_CPI_HOUSING')
    // formatPct does not group thousands on this page, so the raw digits.
    expect(shelter.textContent).toContain('1430')
    const shelterReal = screen.getByTestId('col-real-SCI_CPI_HOUSING')
    expect(shelterReal.textContent).toContain('53')
    expect(shelterReal.textContent).toMatch(/[-−]/)

    const cars = screen.getByTestId('col-real-SCI_CPI_VEHICLES')
    expect(cars.textContent).toContain('81')
    expect(cars.textContent).toContain('+')
  })

  // The single most misleading reading this page could invite.
  it('renders the caveat that shelter is not a house price', () => {
    render(<CostOfLivingCard block={BLOCK} calendar="gregorian" />)
    const body = document.body.textContent ?? ''
    expect(body).toContain('NO house PRICE index')
    expect(body).toMatch(/rent-dominated/i)
    // And the per-row note that says what the shelter index actually measures.
    expect(body).toMatch(/cost of OCCUPYING a home/i)
  })

  it('never shows rent as its own row, and says why it is absent', () => {
    render(<CostOfLivingCard block={BLOCK} calendar="gregorian" />)
    // No ROW for rent...
    expect(screen.queryByTestId('col-growth-SCI_CPI_RENT')).toBeNull()
    expect(screen.queryByTestId('col-real-SCI_CPI_RENT')).toBeNull()
    // ...but the code IS named in the note, which is the point: a reader who
    // wonders where rent went is told it is the shelter row, rather than
    // being left to assume the data is missing.
    expect(document.body.textContent).toContain('SCI_CPI_RENT')
    expect(document.body.textContent).toMatch(/same fact twice/i)
  })

  it('shows a reason instead of a zero when a component cannot be measured', () => {
    const unmeasured: CostOfLivingBlock = {
      ...BLOCK,
      items: [
        {
          ...BLOCK.items[0],
          growth_pct: null,
          real_growth_pct: null,
          from: null,
          to: null,
          periods: 0
        }
      ]
    }
    render(<CostOfLivingCard block={unmeasured} calendar="gregorian" />)
    const cell = screen.getByTestId('col-growth-SCI_CPI_HOUSING')
    // A dash, never a zero: a zero would read as "prices did not move".
    expect(cell.textContent).not.toMatch(/\b0(\.0+)?%/)
    expect(cell.textContent).toContain('\u2014')
    // The reason travels in the title, which is how every other absent metric
    // on this page carries its own — the same affordance, not a new one.
    const marker = cell.querySelector('[data-absent="true"]')
    expect(marker?.getAttribute('title')).toMatch(/never interpolated|fewer than two/i)
    expect(screen.getByText(/not measured/i)).toBeTruthy()
  })

  it('explains what the sign means, because "negative" reads as bad by default', () => {
    render(<CostOfLivingCard block={BLOCK} calendar="gregorian" />)
    const body = document.body.textContent ?? ''
    expect(body).toMatch(/relatively cheaper/i)
    expect(body).toMatch(/not a return/i)
  })

  it('states the headline it is measured against', () => {
    render(<CostOfLivingCard block={BLOCK} calendar="gregorian" />)
    expect(screen.getAllByText(/SCI_CPI_URBAN/).length).toBeGreaterThan(0)
  })
})

// A property of the FILE, not of one render: the cost-of-living rows must not
// be merged into the ranked asset table. The API separates them precisely so
// an index nobody can buy cannot be sorted against gold or win "best
// performer", and a later edit that folds them into `items` would undo that in
// the one place a reader looks.
describe('Markets.tsx — the separation is structural', () => {
  it('renders cost_of_living through its own card and never into items', () => {
    expect(marketsSource).toContain('<CostOfLivingCard')
    // The page must not splice the basket rows into the ranked list.
    expect(marketsSource).not.toMatch(/items.*\.concat\(.*cost_of_living/s)
    expect(marketsSource).not.toMatch(/\[\s*\.\.\.\s*items\s*,\s*\.\.\.\s*data\.cost_of_living/s)
  })
})
