
import { describe, expect, it } from 'vitest'
import { resolvePolicy, TILT_CONFIDENCE_MIN, ROUND_TRIP_COST_PCT, horizonTilt } from '../lib/advice'
import type { DecisionPolicy } from '../lib/advice'
import type { Prediction } from '../api/types'

/**
 * Contract test: the frontend must consume the backend decision policy, not
 * re-derive it. These assertions are pinned to the exact field names and
 * semantics produced by prediction-python/app/core/costs.py::decision_policy.
 * If Python renames or re-scales a field, this fails instead of the UI
 * silently drifting back to its own hard-coded rules.
 */
const SERVER_POLICY: DecisionPolicy = {
  cost_pct: 0.5084,
  cost_basis: 'observed_spread',
  cost_source: 'hamrahgold',
  cost_observed_at: '2026-07-27T09:05:04.427397+00:00',
  cost_age_hours: 0.07,
  cost_reason: 'observed hamrahgold buy/sell spread, 0.1h old',
  buy_threshold_pct: 0.5084,
  sell_threshold_pct: 0.2542,
  min_confidence_pct: 55.0,
  fallback_cost_pct: 2.2,
  policy_version: 1
}

const pred = (pct: number, conf: number): Prediction =>
  ({
    id: 1, horizon: '1d', target_time: '2026-07-28T08:00:00Z',
    point_forecast: 5_000_000, lower_bound: 4_900_000, upper_bound: 5_100_000,
    expected_change_pct: pct, direction: 'up', confidence: conf,
    model_name: 'naive', actual_value: null
  }) as unknown as Prediction

describe('decision policy contract', () => {
  it('uses the server thresholds verbatim, not local constants', () => {
    const r = resolvePolicy(SERVER_POLICY)
    expect(r.costPct).toBe(0.5084)
    expect(r.buyPct).toBe(0.5084)
    expect(r.sellPct).toBe(0.2542)
    expect(r.minConf).toBe(55)
    expect(r.basis).toBe('observed_spread')
    expect(r.costPct).not.toBe(ROUND_TRIP_COST_PCT)
  })

  it('sell bar is half the buy bar (exit leg only) as Python defines it', () => {
    const r = resolvePolicy(SERVER_POLICY)
    expect(r.sellPct).toBeCloseTo(r.buyPct / 2, 6)
  })

  it('tilts honor server thresholds', () => {
    // +0.6% clears the 0.5084% buy bar at 70% confidence
    expect(horizonTilt(pred(0.6, 0.7), 0.5084, SERVER_POLICY)).toBe('favors-buying')
    // -0.3% clears the 0.2542% sell bar
    expect(horizonTilt(pred(-0.3, 0.7), 0.5084, SERVER_POLICY)).toBe('favors-selling')
    // +0.4% is inside the buy bar -> waiting
    expect(horizonTilt(pred(0.4, 0.7), 0.5084, SERVER_POLICY)).toBe('favors-waiting')
    // clears cost but under the confidence bar -> unclear
    expect(horizonTilt(pred(0.6, 0.4), 0.5084, SERVER_POLICY)).toBe('unclear')
  })

  it('fails safe when the policy is missing or malformed', () => {
    for (const bad of [null, undefined, {} as DecisionPolicy,
                       { cost_pct: Number.NaN } as DecisionPolicy]) {
      const r = resolvePolicy(bad as DecisionPolicy | null)
      expect(Number.isFinite(r.costPct)).toBe(true)
      expect(r.costPct).toBeGreaterThan(0)
      expect(r.minConf).toBe(TILT_CONFIDENCE_MIN)
    }
  })

  it('a live spread is used only when the server sent no policy', () => {
    expect(resolvePolicy(null, 0.42).costPct).toBe(0.42)
    expect(resolvePolicy(SERVER_POLICY, 0.42).costPct).toBe(0.5084)
  })
})
