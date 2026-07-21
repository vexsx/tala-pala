import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ActionPlanner } from '../components/ActionPlanner'
import type { PortfolioSummary, Prediction, PriceHistoryItem } from '../api/types'

const NOW = Date.parse('2026-07-20T00:00:00Z')

function pred(overrides: Partial<Prediction> = {}): Prediction {
  return {
    id: 1,
    horizon: '7d',
    predicted_at: '2026-07-19T10:00:00Z',
    target_time: '2026-07-27T10:00:00Z',
    point_forecast: 8_200_000,
    lower_bound: 8_050_000,
    upper_bound: 8_350_000,
    expected_change_pct: 2.5,
    direction: 'up',
    confidence: 0.72,
    model_name: 'test-model',
    actual_value: null,
    ...overrides
  }
}

function hist(observed_at: string, value: number): PriceHistoryItem {
  return { observed_at, value, source: 'test' }
}

const predictions = [
  pred({
    id: 1,
    horizon: '1d',
    target_time: '2026-07-21T10:00:00Z',
    point_forecast: 8_050_000,
    lower_bound: 7_990_000,
    upper_bound: 8_110_000,
    expected_change_pct: 0.6
  }),
  pred({ id: 2, horizon: '7d' })
]

const history = [
  hist('2026-07-17T00:00:00Z', 7_950_000),
  hist('2026-07-18T00:00:00Z', 7_980_000),
  hist('2026-07-19T00:00:00Z', 8_000_000)
]

const portfolio: PortfolioSummary = {
  total_grams_18k_equivalent: 12.5,
  invested: 90_000_000,
  current_value: 101_500_000,
  unrealized_pnl: 11_500_000,
  pnl_pct: 12.78,
  avg_price: 7_200_000,
  break_even_price: 7_250_000,
  scenarios: [],
  target_price_for_profit_pct: 7_975_000
}

function renderPlanner(props: Partial<Parameters<typeof ActionPlanner>[0]> = {}) {
  return render(
    <ActionPlanner
      predictions={predictions}
      history={history}
      currentPrice={8_000_000}
      now={NOW}
      {...props}
    />
  )
}

describe('ActionPlanner', () => {
  it('renders one outcome row per future horizon with net-of-cost math', () => {
    renderPlanner()
    const card = screen.getByTestId('action-planner')
    expect(card).toHaveTextContent('Tomorrow')
    expect(card).toHaveTextContent('7 days')
    // 7d: +2.50% expected − 1.5% cost = +1.00% net; 1d: 0.60 − 1.5 = −0.90%.
    expect(card).toHaveTextContent('+1.00%')
    expect(card).toHaveTextContent('-0.90%')
    // change vs today for the 7d row: (8.2M − 8.0M) / 8.0M = +2.50%
    expect(card).toHaveTextContent('+2.50%')
  })

  it('marks the likely best buy and sell windows', () => {
    renderPlanner()
    const card = screen.getByTestId('action-planner')
    // Both forecasts sit above today's price → buy window is today.
    expect(card).toHaveTextContent(/likely best buy window: today/i)
    // Highest forecast is the 7d horizon → sell window at its target date.
    expect(card).toHaveTextContent(/likely best sell window: .*7 days/i)
  })

  it('renders tilt badges from the cost-aware heuristic', () => {
    renderPlanner()
    const card = screen.getByTestId('action-planner')
    expect(card).toHaveTextContent('favors buying') // 7d clears cost with confidence
    expect(card).toHaveTextContent('favors waiting') // 1d move within cost
  })

  it('shows the projected portfolio value at the sell window when holdings exist', () => {
    renderPlanner({ portfolio })
    const card = screen.getByTestId('action-planner')
    expect(card).toHaveTextContent('You hold 12.5 g')
    expect(card).toHaveTextContent(/est\. p\/l/i)
  })

  it('renders context facts only when present', () => {
    renderPlanner({ corrXau20: 0.62, fundsFlowPct: 3.2 })
    const card = screen.getByTestId('action-planner')
    expect(card).toHaveTextContent(/retail money is flowing into gold funds/i)
    expect(card).toHaveTextContent('0.62')
    expect(card).not.toHaveTextContent('USD/IRT · 7d')
  })

  it('shows an empty state without future forecasts and always the disclaimer', () => {
    renderPlanner({ predictions: [] })
    const card = screen.getByTestId('action-planner')
    expect(card).toHaveTextContent(/no future forecasts yet/i)
    expect(card).toHaveTextContent(/not financial advice/i)
  })
})
