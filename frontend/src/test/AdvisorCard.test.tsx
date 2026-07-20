import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { AdvisorCard } from '../components/AdvisorCard'
import type {
  PortfolioSummary,
  Prediction,
  SignalLevel,
  SignalSummary
} from '../api/types'

function sig(level: SignalLevel, extra: Partial<SignalSummary> = {}): SignalSummary {
  return {
    signal: level,
    score: 62,
    confidence: 0.68,
    explanation: 'Model-based assessment.',
    supporting: ['Momentum over the last 10 days is positive.'],
    conflicting: ['Local premium is rich versus its 30-day norm.'],
    risks: ['Volatility is elevated.'],
    invalidation: 'price closes below SMA20 (~8,000,000 IRT) or data goes stale.',
    review_at: '2026-07-20T18:00:00Z',
    data_fresh: true,
    ...extra
  }
}

function pred(overrides: Partial<Prediction> = {}): Prediction {
  return {
    id: 1,
    horizon: '7d',
    created_at: '2026-07-20T10:00:00Z',
    target_time: '2026-07-27T10:00:00Z',
    base_value: 8_000_000,
    predicted_value: 8_144_000,
    lower_bound: 8_000_000,
    upper_bound: 8_280_000,
    expected_change_pct: 1.8,
    direction: 'up',
    confidence: 0.72,
    model_name: 'test-model',
    actual_value: null,
    ...overrides
  }
}

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

function renderCard(props: Partial<Parameters<typeof AdvisorCard>[0]> = {}) {
  return render(
    <AdvisorCard
      signal={sig('buy')}
      predictions={[pred()]}
      portfolio={null}
      currentPrice={8_120_000}
      premiumPct={4.2}
      {...props}
    />
  )
}

describe('AdvisorCard', () => {
  it.each(['strong_buy', 'buy'] as SignalLevel[])(
    'renders an accumulate headline with horizon and confidence for %s',
    (level) => {
      renderCard({ signal: sig(level) })
      const card = screen.getByTestId('advisor-card')
      expect(card).toHaveTextContent(/conditions currently favor accumulating/i)
      expect(card).toHaveTextContent('a rise of 1.80%')
      expect(card).toHaveTextContent('7 days')
      expect(card).toHaveTextContent('72% confidence')
      expect(screen.getByTestId('signal-badge').className).toContain(`sig-${level}`)
    }
  )

  it('renders a neutral headline for hold', () => {
    renderCard({ signal: sig('hold') })
    expect(screen.getByTestId('advisor-card')).toHaveTextContent(
      /do not clearly favor buying or selling — waiting is reasonable/i
    )
  })

  it.each(['sell', 'strong_sell'] as SignalLevel[])(
    'renders a reduce-exposure headline for %s',
    (level) => {
      renderCard({
        signal: sig(level),
        predictions: [pred({ direction: 'down', expected_change_pct: -1.2 })]
      })
      const card = screen.getByTestId('advisor-card')
      expect(card).toHaveTextContent(/conditions currently favor reducing exposure/i)
      expect(card).toHaveTextContent('a decline of 1.20%')
    }
  )

  it('shows the why / but factor lists and the invalidation condition', () => {
    renderCard()
    expect(screen.getByText('Momentum over the last 10 days is positive.')).toBeInTheDocument()
    expect(screen.getByText('Local premium is rich versus its 30-day norm.')).toBeInTheDocument()
    expect(screen.getByText('Volatility is elevated.')).toBeInTheDocument()
    expect(screen.getByTestId('advisor-card')).toHaveTextContent(/this view becomes invalid if/i)
    expect(screen.getByTestId('advisor-card')).toHaveTextContent(/price closes below SMA20/i)
  })

  it('labels moderate confidence and always shows the disclaimer', () => {
    renderCard()
    const card = screen.getByTestId('advisor-card')
    expect(card).toHaveTextContent(/confidence is moderate/i)
    expect(card).toHaveTextContent('Decision support only — not financial advice.')
  })

  it('labels low and high confidence bands', () => {
    const { unmount } = renderCard({ signal: sig('buy', { confidence: 0.3 }) })
    expect(screen.getByTestId('advisor-card')).toHaveTextContent(/low — treat as noise/i)
    unmount()
    renderCard({ signal: sig('buy', { confidence: 0.85 }) })
    expect(screen.getByTestId('advisor-card')).toHaveTextContent(/high \(by historical hit-rate\)/i)
  })

  it('shows portfolio context when holdings exist', () => {
    renderCard({ portfolio })
    const card = screen.getByTestId('advisor-card')
    expect(card).toHaveTextContent('You hold 12.5 g')
    expect(card).toHaveTextContent(/vs\s+your break-even/i)
    // buy-ish signal → premium mentioned as an entry cost
    expect(card).toHaveTextContent(/premium over global parity/i)
  })

  it('notes the realizable gain when signal is sell-ish and pnl is positive', () => {
    renderCard({
      signal: sig('sell'),
      predictions: [pred({ direction: 'down', expected_change_pct: -1.2 })],
      portfolio
    })
    expect(screen.getByTestId('advisor-card')).toHaveTextContent(/would realize roughly/i)
  })

  it('renders the subdued no-recommendation state when data is stale', () => {
    renderCard({ signal: sig('hold', { data_fresh: false }) })
    const card = screen.getByTestId('advisor-card')
    expect(card).toHaveTextContent(/insufficient fresh data — no recommendation/i)
    expect(card).toHaveTextContent('Decision support only — not financial advice.')
    expect(screen.getByTestId('signal-badge')).toHaveTextContent('No signal')
  })

  it('treats a missing signal (or missing data_fresh flag) as stale', () => {
    const { unmount } = renderCard({ signal: null })
    expect(screen.getByTestId('advisor-card')).toHaveTextContent(/insufficient fresh data/i)
    unmount()
    renderCard({ signal: sig('buy', { data_fresh: undefined }) })
    expect(screen.getByTestId('advisor-card')).toHaveTextContent(/insufficient fresh data/i)
  })
})
