import { describe, expect, it } from 'vitest'
import {
  bestWindows,
  changeOverDays,
  defaultAdvisorHorizon,
  horizonTilt,
  latestByHorizon,
  parseAdvisorSelection,
  planRows,
  serializeAdvisorSelection,
  ROUND_TRIP_COST_PCT,
  type Tilt
} from '../lib/advice'
import { normalizePrediction } from '../lib/forecastChart'
import type { Prediction, PriceHistoryItem } from '../api/types'

const NOW = Date.parse('2026-07-20T00:00:00Z')

function pred(overrides: Partial<Prediction> = {}): Prediction {
  return {
    id: 1,
    horizon: '7d',
    predicted_at: '2026-07-19T10:00:00Z',
    target_time: '2026-07-27T10:00:00Z',
    point_forecast: 8_144_000,
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

describe('horizonTilt', () => {
  // [expected_change_pct, confidence, data_fresh, expected tilt]
  const cases: Array<[number, number, boolean | undefined, Tilt]> = [
    [2.0, 0.72, undefined, 'favors-buying'], // clears cost with confidence
    [1.51, 0.55, undefined, 'favors-buying'], // boundary: conf exactly 55
    [2.0, 0.5, undefined, 'unclear'], // clears cost but low confidence
    [1.5, 0.9, undefined, 'favors-waiting'], // exactly at cost → not a buy
    [0.5, 0.9, undefined, 'favors-waiting'], // small move, within cost
    [-1.0, 0.7, undefined, 'favors-selling'], // drop beyond half the cost
    [-0.7, 0.7, undefined, 'favors-waiting'], // drop within half the cost
    [-1.0, 0.4, undefined, 'favors-waiting'], // drop but low confidence, |pct| ≤ cost
    [-2.5, 0.9, undefined, 'favors-selling'],
    [-2.5, 0.4, undefined, 'unclear'], // big drop, low confidence, |pct| > cost
    [2.0, 0.9, false, 'no-call'], // stale data always silences the call
    [Number.NaN, 0.9, undefined, 'unclear']
  ]

  it.each(cases)('pct=%s conf=%s fresh=%s → %s', (pct, conf, fresh, expected) => {
    const p = pred({
      expected_change_pct: pct,
      confidence: conf,
      data_fresh: fresh
    })
    expect(horizonTilt(p, ROUND_TRIP_COST_PCT)).toBe(expected)
  })
})

describe('normalizePrediction (base_value derivation)', () => {
  it('derives base_value from point_forecast and expected_change_pct', () => {
    const p = normalizePrediction(
      pred({ point_forecast: 8_100_000, expected_change_pct: 1.25 })
    )
    expect(p.base_value).toBeCloseTo(8_000_000, 5)
  })

  it('derives base_value for negative expected changes', () => {
    const p = normalizePrediction(
      pred({ point_forecast: 7_920_000, expected_change_pct: -1 })
    )
    expect(p.base_value).toBeCloseTo(8_000_000, 5)
  })

  it('keeps an existing base_value untouched', () => {
    const p = normalizePrediction(
      pred({ base_value: 7_777_777, point_forecast: 8_100_000, expected_change_pct: 1.25 })
    )
    expect(p.base_value).toBe(7_777_777)
  })

  it('skips the derivation at expected_change_pct === -100 (division by zero)', () => {
    const p = normalizePrediction(
      pred({ point_forecast: 0, expected_change_pct: -100 })
    )
    expect(p.base_value).toBeUndefined()
  })

  it('fills predicted_value from point_forecast and created_at from predicted_at', () => {
    const p = normalizePrediction(pred({ point_forecast: 8_100_000 }))
    expect(p.predicted_value).toBe(8_100_000)
    expect(p.created_at).toBe('2026-07-19T10:00:00Z')
  })

  it('lets the derived base drive direction hit/miss checks', () => {
    // Live-API row: no base_value. Actual finished above the derived base → 'up' hit.
    const p = normalizePrediction(
      pred({ point_forecast: 8_100_000, expected_change_pct: 1.25, actual_value: 8_050_000 })
    )
    expect(p.base_value).toBeDefined()
    expect(p.actual_value! > p.base_value!).toBe(true)
  })
})

describe('planRows', () => {
  const preds = [
    pred({
      id: 1,
      horizon: '1d',
      target_time: '2026-07-21T00:00:00Z',
      point_forecast: 8_100_000,
      expected_change_pct: 1.25,
      lower_bound: 8_020_000,
      upper_bound: 8_180_000
    }),
    pred({
      id: 2,
      horizon: '7d',
      target_time: '2026-07-27T00:00:00Z',
      point_forecast: 8_200_000,
      expected_change_pct: 2.5,
      lower_bound: 8_050_000,
      upper_bound: 8_350_000
    }),
    pred({
      id: 3,
      horizon: 'eod',
      target_time: '2026-07-19T17:00:00Z', // already in the past
      point_forecast: 8_010_000,
      expected_change_pct: 0.1
    })
  ]

  it('keeps only future horizons, sorted by target time', () => {
    const rows = planRows(preds, 8_000_000, 1.5, NOW)
    expect(rows.map((r) => r.horizon)).toEqual(['1d', '7d'])
  })

  it('computes change vs today and net-of-cost percentages', () => {
    const rows = planRows(preds, 8_000_000, 1.5, NOW)
    expect(rows[0].changeVsTodayPct).toBeCloseTo(1.25, 6)
    expect(rows[0].netPct).toBeCloseTo(1.25 - 1.5, 6)
    expect(rows[1].changeVsTodayPct).toBeCloseTo(2.5, 6)
    expect(rows[1].netPct).toBeCloseTo(1.0, 6)
  })

  it('leaves changeVsTodayPct null without a current price', () => {
    const rows = planRows(preds, null, 1.5, NOW)
    expect(rows[0].changeVsTodayPct).toBeNull()
    expect(rows[0].netPct).toBeCloseTo(-0.25, 6)
  })

  it('dedupes to the latest prediction per horizon', () => {
    const rows = planRows(
      [
        pred({
          id: 1,
          horizon: '1d',
          predicted_at: '2026-07-19T08:00:00Z',
          target_time: '2026-07-21T00:00:00Z',
          point_forecast: 8_000_001
        }),
        pred({
          id: 2,
          horizon: '1d',
          predicted_at: '2026-07-19T12:00:00Z',
          target_time: '2026-07-21T00:00:00Z',
          point_forecast: 8_111_111
        })
      ],
      null,
      1.5,
      NOW
    )
    expect(rows).toHaveLength(1)
    expect(rows[0].forecast).toBe(8_111_111)
  })
})

describe('bestWindows', () => {
  const rows = planRows(
    [
      pred({
        id: 1,
        horizon: '1d',
        target_time: '2026-07-21T00:00:00Z',
        point_forecast: 8_100_000
      }),
      pred({
        id: 2,
        horizon: '7d',
        target_time: '2026-07-27T00:00:00Z',
        point_forecast: 8_200_000
      })
    ],
    null,
    1.5,
    NOW
  )

  it('picks lowest forecast as buy window and highest as sell window', () => {
    const w = bestWindows(rows, 8_150_000)
    expect(w).not.toBeNull()
    expect(w!.buy).toEqual({ when: 'horizon', row: rows[0] })
    expect(w!.sell).toEqual({ when: 'horizon', row: rows[1] })
  })

  it('says buy today when every forecast is above the current price', () => {
    const w = bestWindows(rows, 8_000_000)
    expect(w!.buy).toEqual({ when: 'today' })
    expect(w!.sell).toEqual({ when: 'horizon', row: rows[1] })
  })

  it('says sell today when every forecast is below the current price', () => {
    const w = bestWindows(rows, 9_000_000)
    expect(w!.sell).toEqual({ when: 'today' })
    expect(w!.buy).toEqual({ when: 'horizon', row: rows[0] })
  })

  it('ignores stale rows and returns null when none are fresh', () => {
    const stale = planRows(
      [
        pred({
          id: 1,
          horizon: '1d',
          target_time: '2026-07-21T00:00:00Z',
          data_fresh: false
        })
      ],
      null,
      1.5,
      NOW
    )
    expect(bestWindows(stale, 8_000_000)).toBeNull()
  })
})

describe('advisor selection helpers', () => {
  it('round-trips standard and custom selections', () => {
    expect(parseAdvisorSelection('7d')).toEqual({ kind: 'std', horizon: '7d' })
    expect(parseAdvisorSelection('custom:14')).toEqual({ kind: 'custom', days: 14 })
    expect(serializeAdvisorSelection({ kind: 'std', horizon: '3d' })).toBe('3d')
    expect(serializeAdvisorSelection({ kind: 'custom', days: 30 })).toBe('custom:30')
  })

  it('rejects junk values', () => {
    expect(parseAdvisorSelection(null)).toBeNull()
    expect(parseAdvisorSelection('')).toBeNull()
    expect(parseAdvisorSelection('2w')).toBeNull()
    expect(parseAdvisorSelection('custom:0')).toBeNull()
    expect(parseAdvisorSelection('custom:91')).toBeNull()
    expect(parseAdvisorSelection('custom:abc')).toBeNull()
  })

  it('defaults to 7d when available, else the longest horizon', () => {
    expect(defaultAdvisorHorizon(['1h', '7d', '30d'])).toBe('7d')
    expect(defaultAdvisorHorizon(['1h', '3d'])).toBe('3d')
    expect(defaultAdvisorHorizon([])).toBeNull()
  })
})

describe('latestByHorizon', () => {
  it('returns horizons in canonical order regardless of input order', () => {
    const out = latestByHorizon([
      pred({ id: 1, horizon: '30d' }),
      pred({ id: 2, horizon: '1h' }),
      pred({ id: 3, horizon: '3d' })
    ])
    expect(out.map((p) => p.horizon)).toEqual(['1h', '3d', '30d'])
  })
})

describe('changeOverDays', () => {
  const hist = (observed_at: string, value: number): PriceHistoryItem => ({
    observed_at,
    value,
    source: 'test'
  })

  it('computes the percent change over the window', () => {
    const items = [
      hist('2026-07-10T00:00:00Z', 8_000_000),
      hist('2026-07-13T00:00:00Z', 8_100_000),
      hist('2026-07-20T00:00:00Z', 8_400_000)
    ]
    // Reference is the latest observation ≥ 7 days before the last one (07-13).
    expect(changeOverDays(items, 7)).toBeCloseTo((300_000 / 8_100_000) * 100, 6)
  })

  it('returns null with fewer than two points', () => {
    expect(changeOverDays([hist('2026-07-20T00:00:00Z', 8_000_000)], 7)).toBeNull()
    expect(changeOverDays([], 7)).toBeNull()
  })
})
