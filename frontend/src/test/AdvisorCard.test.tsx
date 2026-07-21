import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { AdvisorCard } from '../components/AdvisorCard'
import type {
  CustomForecast,
  PortfolioSummary,
  Prediction,
  SignalLevel,
  SignalSummary
} from '../api/types'

// The custom-timeframe mode fetches /predictions/custom on demand; mock the
// transport so chip tests stay hermetic. Standard-mode tests never call it.
vi.mock('../api/client', () => ({
  api: vi.fn(() => new Promise(() => undefined)),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : 'Unexpected error')
}))
import { api } from '../api/client'

const apiMock = api as unknown as Mock

beforeEach(() => {
  window.localStorage.clear()
  apiMock.mockReset()
  apiMock.mockImplementation(() => new Promise(() => undefined))
})

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

  it('treats a missing signal as no-recommendation', () => {
    renderCard({ signal: null })
    expect(screen.getByTestId('advisor-card')).toHaveTextContent(/insufficient fresh data/i)
  })

  it('still recommends when data_fresh is missing (only false silences it)', () => {
    renderCard({ signal: sig('buy', { data_fresh: undefined }) })
    const card = screen.getByTestId('advisor-card')
    expect(card).not.toHaveTextContent(/insufficient fresh data/i)
    expect(card).toHaveTextContent(/conditions currently favor accumulating/i)
  })

  it('shows the last-session note when the signal carries a market-closed note', () => {
    renderCard({
      signal: sig('buy', { notes: ['prices from last session (market closed)'] })
    })
    const card = screen.getByTestId('advisor-card')
    expect(card).toHaveTextContent(/conditions currently favor accumulating/i)
    expect(card).toHaveTextContent(/assessment based on last session.s closing prices/i)
  })

  it('detects the market-closed note when it arrives among risks', () => {
    renderCard({
      signal: sig('buy', { risks: ['prices from last session (market closed)'] })
    })
    expect(screen.getByTestId('advisor-card')).toHaveTextContent(
      /assessment based on last session.s closing prices/i
    )
  })

  it('omits the last-session note when the market is open', () => {
    renderCard()
    expect(screen.getByTestId('advisor-card')).not.toHaveTextContent(
      /assessment based on last session/i
    )
  })
})

describe('AdvisorCard timeframe selector', () => {
  const customResult: CustomForecast = {
    symbol: 'IR_GOLD_18K',
    horizon_days: 14,
    model_name: 'gbm-fast',
    beats_naive: true,
    point_forecast: 8_300_000,
    lower_bound: 8_100_000,
    upper_bound: 8_500_000,
    last_price: 8_120_000,
    expected_change_pct: 2.2,
    direction: 'up',
    confidence: 0.61,
    regime: 'trending',
    decision_lean: 'buy',
    decision_note: 'Expected move clears assumed costs.',
    monte_carlo: {
      p_up: 0.64,
      p_gain_over_cost: 0.41,
      p_loss_over_cost: 0.18,
      sim_p05_pct: -2.1,
      sim_median_pct: 1.9,
      sim_p95_pct: 6.2,
      n_paths: 2000
    },
    round_trip_cost_pct: 1.5,
    provider_gap_pct: 0.4,
    warnings: [],
    ephemeral: true
  }

  it('renders one chip per available horizon plus Custom', () => {
    renderCard({
      predictions: [
        pred({ id: 1, horizon: '1h', target_time: '2026-07-20T11:00:00Z' }),
        pred({ id: 2, horizon: '7d' })
      ]
    })
    expect(screen.getByRole('button', { name: '1 hour' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '7 days' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Custom…' })).toBeInTheDocument()
    // Horizons without a latest prediction get no chip.
    expect(screen.queryByRole('button', { name: '30 days' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Tomorrow' })).not.toBeInTheDocument()
  })

  it('defaults to 7d and shows the selected-timeframe detail with a tilt sentence', () => {
    renderCard({
      predictions: [pred({ expected_change_pct: 1.8, confidence: 0.72 })]
    })
    expect(screen.getByRole('button', { name: '7 days' })).toHaveAttribute('aria-pressed', 'true')
    const detail = screen.getByTestId('advisor-timeframe-detail')
    expect(detail).toHaveTextContent(/selected timeframe · 7 days/i)
    expect(detail).toHaveTextContent(/models project \+1\.80%/i)
    expect(detail).toHaveTextContent(/conditions modestly favor buying/i)
    expect(detail).toHaveTextContent('favors buying')
  })

  it('honors a persisted standard selection from localStorage', () => {
    window.localStorage.setItem('igp_advisor_horizon', '3d')
    renderCard({
      predictions: [
        pred({ id: 1, horizon: '3d', target_time: '2026-07-23T10:00:00Z' }),
        pred({ id: 2, horizon: '7d' })
      ]
    })
    expect(screen.getByRole('button', { name: '3 days' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByTestId('advisor-timeframe-detail')).toHaveTextContent(
      /selected timeframe · 3 days/i
    )
  })

  it('falls back to the default when the persisted horizon has no prediction', () => {
    window.localStorage.setItem('igp_advisor_horizon', '30d')
    renderCard({ predictions: [pred()] })
    expect(screen.getByRole('button', { name: '7 days' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('persists a chip click to localStorage and activates the chip', () => {
    renderCard({
      predictions: [
        pred({ id: 1, horizon: '3d', target_time: '2026-07-23T10:00:00Z' }),
        pred({ id: 2, horizon: '7d' })
      ]
    })
    fireEvent.click(screen.getByRole('button', { name: '3 days' }))
    expect(window.localStorage.getItem('igp_advisor_horizon')).toBe('3d')
    expect(screen.getByRole('button', { name: '3 days' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByTestId('advisor-timeframe-detail')).toHaveTextContent(
      /selected timeframe · 3 days/i
    )
  })

  it('renders the server decision engine output in custom mode', async () => {
    window.localStorage.setItem('igp_advisor_horizon', 'custom:14')
    apiMock.mockResolvedValue(customResult)
    renderCard()
    expect(await screen.findAllByText('Buy lean')).not.toHaveLength(0)
    const detail = screen.getByTestId('advisor-timeframe-detail')
    expect(detail).toHaveTextContent(/custom, 14 days ahead/i)
    expect(detail).toHaveTextContent('Expected move clears assumed costs.')
    expect(detail).toHaveTextContent(/model decision engine/i)
    expect(detail).toHaveTextContent('64% up')
    expect(detail).toHaveTextContent('41%')
    expect(detail).toHaveTextContent(/1\.5% round-trip/i)
    expect(apiMock).toHaveBeenCalledWith('/predictions/custom?days=14', expect.anything())
  })

  it('shows a loading state while the custom forecast computes', () => {
    window.localStorage.setItem('igp_advisor_horizon', 'custom:30')
    renderCard()
    expect(screen.getByTestId('advisor-timeframe-detail')).toHaveTextContent(
      /computing custom forecast/i
    )
  })
})
