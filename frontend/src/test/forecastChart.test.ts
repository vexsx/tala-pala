import { describe, expect, it } from 'vitest'
import { buildForecastChartData } from '../lib/forecastChart'
import type { Prediction, PriceHistoryItem } from '../api/types'

function hist(observed_at: string, value: number): PriceHistoryItem {
  return { observed_at, value, source: 'test' }
}

function pred(overrides: Partial<Prediction> & Pick<Prediction, 'target_time'>): Prediction {
  return {
    id: 1,
    horizon: '1d',
    created_at: '2026-07-19T10:00:00Z',
    base_value: 8_000_000,
    predicted_value: 8_100_000,
    lower_bound: 8_000_000,
    upper_bound: 8_200_000,
    expected_change_pct: 1.25,
    direction: 'up',
    confidence: 0.7,
    model_name: 'test-model',
    actual_value: null,
    ...overrides
  }
}

describe('buildForecastChartData', () => {
  it('sorts unsorted history ascending by time', () => {
    const out = buildForecastChartData(
      [
        hist('2026-07-18T00:00:00Z', 8_050_000),
        hist('2026-07-16T00:00:00Z', 8_000_000),
        hist('2026-07-17T00:00:00Z', 8_020_000)
      ],
      []
    )
    expect(out.map((p) => p.actual)).toEqual([8_000_000, 8_020_000, 8_050_000])
    expect(out.map((p) => p.t)).toEqual(out.map((p) => p.t).slice().sort((a, b) => a - b))
    // no forecast keys when there are no predictions
    expect(out.every((p) => p.forecast === undefined && p.band === undefined)).toBe(true)
  })

  it('bridges the last actual into the forecast series so the lines connect', () => {
    const out = buildForecastChartData(
      [hist('2026-07-18T00:00:00Z', 8_000_000), hist('2026-07-19T00:00:00Z', 8_050_000)],
      [
        pred({
          target_time: '2026-07-20T00:00:00Z',
          predicted_value: 8_150_000,
          lower_bound: 8_060_000,
          upper_bound: 8_240_000
        })
      ]
    )
    expect(out).toHaveLength(3)
    const bridge = out[1]
    expect(bridge.actual).toBe(8_050_000)
    expect(bridge.forecast).toBe(8_050_000)
    expect(bridge.band).toEqual([8_050_000, 8_050_000])
    const fc = out[2]
    expect(fc.actual).toBeUndefined()
    expect(fc.forecast).toBe(8_150_000)
    expect(fc.band).toEqual([8_060_000, 8_240_000])
  })

  it('merges predictions sharing a target_time into a min/max band', () => {
    const out = buildForecastChartData(
      [hist('2026-07-19T00:00:00Z', 8_000_000)],
      [
        pred({
          target_time: '2026-07-20T00:00:00Z',
          predicted_value: 8_100_000,
          lower_bound: 8_050_000,
          upper_bound: 8_150_000
        }),
        pred({
          id: 2,
          horizon: '3d',
          target_time: '2026-07-20T00:00:00Z',
          predicted_value: 8_200_000,
          lower_bound: 8_020_000,
          upper_bound: 8_300_000
        })
      ]
    )
    // bridge + one merged forecast point
    expect(out).toHaveLength(2)
    const fc = out[1]
    expect(fc.forecast).toBe(8_150_000) // mean of the two point forecasts
    expect(fc.band).toEqual([8_020_000, 8_300_000]) // min(lower)..max(upper)
  })

  it('drops predictions whose target is not after the last actual', () => {
    const out = buildForecastChartData(
      [hist('2026-07-19T00:00:00Z', 8_000_000)],
      [pred({ target_time: '2026-07-18T00:00:00Z' })]
    )
    expect(out).toHaveLength(1)
    expect(out[0].forecast).toBeUndefined()
  })

  it('prefers the live-API point_forecast field over predicted_value', () => {
    const out = buildForecastChartData(
      [hist('2026-07-19T00:00:00Z', 8_000_000)],
      [
        pred({
          target_time: '2026-07-20T00:00:00Z',
          predicted_value: 1,
          point_forecast: 8_120_000,
          lower_bound: 8_050_000,
          upper_bound: 8_200_000
        })
      ]
    )
    expect(out[out.length - 1].forecast).toBe(8_120_000)
  })

  it('plots forecast-only points (no bridge) when there is no history', () => {
    const out = buildForecastChartData(
      [],
      [
        pred({ target_time: '2026-07-21T00:00:00Z', predicted_value: 8_200_000 }),
        pred({ id: 2, target_time: '2026-07-20T00:00:00Z', predicted_value: 8_100_000 })
      ]
    )
    expect(out).toHaveLength(2)
    expect(out[0].forecast).toBe(8_100_000) // sorted ascending
    expect(out[1].forecast).toBe(8_200_000)
    expect(out.every((p) => p.actual === undefined)).toBe(true)
  })
})
