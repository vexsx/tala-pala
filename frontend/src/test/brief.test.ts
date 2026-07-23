import { describe, expect, it } from 'vitest'
import { composeBrief } from '../lib/brief'
import type { CustomForecast, MarketSummary, Prediction } from '../api/types'

const NOW = Date.parse('2026-07-23T09:00:00Z')
const fmt = (v: number) => `${Math.round(v).toLocaleString('en-US')} IRT`

function prediction(overrides: Partial<Prediction>): Prediction {
  return {
    id: 1,
    horizon: '7d',
    predicted_at: '2026-07-23T08:00:00Z',
    target_time: '2026-07-30T08:00:00Z',
    point_forecast: 5_100_000,
    lower_bound: 4_900_000,
    upper_bound: 5_300_000,
    expected_change_pct: 2.0,
    direction: 'up',
    confidence: 0.7,
    model_name: 'ensemble',
    actual_value: null,
    ...overrides
  } as Prediction
}

const summary: MarketSummary = {
  current_18k: {
    value: 5_000_000,
    currency: 'IRT',
    unit: 'gram',
    observed_at: '2026-07-23T08:55:00Z',
    change_24h_pct: 0.8,
    stale: false
  },
  xau_usd: {
    value: 2400,
    currency: 'USD',
    unit: 'ozt',
    observed_at: '2026-07-23T08:55:00Z',
    change_24h_pct: -0.2,
    stale: false
  },
  usd_irt: {
    value: 192_000,
    currency: 'IRT',
    unit: 'usd',
    observed_at: '2026-07-23T08:55:00Z',
    change_24h_pct: 0.1,
    stale: false
  },
  theoretical_18k: 4_900_000,
  premium_pct: 2.0,
  premium_avg_30d: 0.5,
  trading_cost_pct: 0.49,
  last_update: '2026-07-23T08:55:00Z',
  providers: [],
  signal: null
} as unknown as MarketSummary

const custom: CustomForecast = {
  symbol: 'IR_GOLD_18K',
  horizon_days: 7,
  model_name: 'ensemble',
  beats_naive: true,
  point_forecast: 5_100_000,
  lower_bound: 4_900_000,
  upper_bound: 5_300_000,
  last_price: 5_000_000,
  expected_change_pct: 2.0,
  direction: 'up',
  confidence: 0.7,
  regime: 'trending',
  decision_lean: 'buy',
  decision_note: 'Projected gain clears costs.',
  monte_carlo: {
    p_up: 0.64,
    p_gain_over_cost: 0.41,
    p_loss_over_cost: 0.18,
    sim_p05_pct: -3.2,
    sim_median_pct: 1.4,
    sim_p95_pct: 6.1,
    n_paths: 2000
  },
  round_trip_cost_pct: 0.49,
  provider_gap_pct: 0.3,
  warnings: []
}

describe('composeBrief', () => {
  it('writes all four sections from a full payload', () => {
    const sections = composeBrief({
      summary,
      predictions: [prediction({})],
      custom,
      costPct: 0.49,
      fmt,
      now: NOW
    })
    expect(sections.map((s) => s.key)).toEqual([
      'situation',
      'expectations',
      'possibilities',
      'prescription'
    ])
    const text = sections.flatMap((s) => s.paragraphs).join(' ')
    expect(text).toContain('0.49%')          // live cost, not the 1.5% fallback
    expect(text).toContain('64% odds')       // Monte Carlo p_up
    expect(text).toContain('not financial advice')
  })

  it('prescribes buying when the projected move clears the live cost', () => {
    const sections = composeBrief({
      summary,
      predictions: [prediction({ expected_change_pct: 2.0, confidence: 0.7 })],
      custom: null,
      costPct: 0.49,
      fmt,
      now: NOW
    })
    const rx = sections.find((s) => s.key === 'prescription')!
    expect(rx.paragraphs.join(' ')).toContain('favor buying')
  })

  it('prescribes patience when moves are within the cost', () => {
    const sections = composeBrief({
      summary,
      predictions: [prediction({ expected_change_pct: 0.2, confidence: 0.7 })],
      custom: null,
      costPct: 1.5,
      fmt,
      now: NOW
    })
    const rx = sections.find((s) => s.key === 'prescription')!
    expect(rx.paragraphs.join(' ')).toContain('patience')
  })

  it('drops every section when there is no data', () => {
    expect(composeBrief({ summary: null, predictions: [], custom: null, costPct: 1.5, fmt })).toEqual([])
  })
})
