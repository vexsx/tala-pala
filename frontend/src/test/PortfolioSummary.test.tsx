import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { PortfolioSummaryCards } from '../pages/Portfolio'
import type { PortfolioSummary } from '../api/types'

const summary: PortfolioSummary = {
  total_grams_18k_equivalent: 12.5,
  invested: 90_000_000,
  current_value: 101_500_000,
  unrealized_pnl: 11_500_000,
  pnl_pct: 12.78,
  avg_price: 7_200_000,
  break_even_price: 7_250_000,
  scenarios: [
    { change_pct: -10, value: 91_350_000, pnl: 1_350_000 },
    { change_pct: 10, value: 111_650_000, pnl: 21_650_000 }
  ],
  target_price_for_profit_pct: 7_975_000
}

describe('PortfolioSummaryCards', () => {
  it('renders invested and current value in grouped toman', () => {
    render(<PortfolioSummaryCards s={summary} />)
    // Default display unit is IRT (toman), so values are NOT multiplied by 10.
    expect(screen.getByText('90,000,000 تومان')).toBeInTheDocument()
    expect(screen.getByText('101,500,000 تومان')).toBeInTheDocument()
  })

  it('renders the PnL with a signed percent and positive color class', () => {
    render(<PortfolioSummaryCards s={summary} />)
    const pnlPct = screen.getByText('+12.78%')
    expect(pnlPct).toBeInTheDocument()
    expect(pnlPct.className).toContain('pos')
    expect(screen.getByText('11,500,000 تومان')).toBeInTheDocument()
  })

  it('renders holdings in 18k-equivalent grams and break-even price', () => {
    render(<PortfolioSummaryCards s={summary} />)
    expect(screen.getByText('12.5 g')).toBeInTheDocument()
    expect(screen.getByText('7,250,000 تومان')).toBeInTheDocument()
  })
})
