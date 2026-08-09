import { describe, expect, it } from 'vitest'
import { shortTermConsensus, verdictSentence } from '../components/ShortTermDesk'
import type { PlanRow } from '../lib/advice'
import type { Tilt } from '../lib/advice'

const row = (horizon: string, tilt: Tilt, fresh = true): PlanRow =>
  ({
    horizon,
    targetTime: '2026-08-10T00:00:00Z',
    t: Date.parse('2026-08-10T00:00:00Z'),
    forecast: 1_000_000,
    lower: 950_000,
    upper: 1_050_000,
    expectedChangePct: 1,
    changeVsTodayPct: 1,
    netPct: 0.5,
    tilt,
    tiltReason: '',
    dataFresh: fresh,
    warnings: []
  }) as unknown as PlanRow

describe('short-term consensus', () => {
  it('reports a split short end as MIXED rather than picking a side', () => {
    const c = shortTermConsensus([
      row('1h', 'favors-buying'),
      row('1d', 'favors-selling')
    ])
    expect(c.verdict).toBe('mixed')
    expect(verdictSentence(c, 0.5)).toContain('disagrees with itself')
  })

  it('leans buy only when nothing leans the other way', () => {
    expect(
      shortTermConsensus([row('1h', 'favors-buying'), row('1d', 'favors-waiting')]).verdict
    ).toBe('buy')
  })

  it('leans sell symmetrically', () => {
    expect(
      shortTermConsensus([row('1h', 'favors-selling'), row('1d', 'favors-waiting')]).verdict
    ).toBe('sell')
  })

  it('says WAIT when every move is inside the cost bar', () => {
    const c = shortTermConsensus([row('1h', 'favors-waiting'), row('1d', 'favors-waiting')])
    expect(c.verdict).toBe('wait')
    expect(verdictSentence(c, 0.51)).toContain('0.51% round-trip cost')
  })

  it('makes NO CALL when every short horizon ran on stale inputs', () => {
    const c = shortTermConsensus([
      row('1h', 'no-call', false),
      row('1d', 'no-call', false)
    ])
    expect(c.verdict).toBe('no-call')
    expect(verdictSentence(c, 0.5)).toContain('stale')
  })

  it('is MIXED when moves clear the cost but confidence does not', () => {
    expect(shortTermConsensus([row('1h', 'unclear'), row('1d', 'unclear')]).verdict).toBe('mixed')
  })

  it('makes no call with no rows at all', () => {
    expect(shortTermConsensus([]).verdict).toBe('no-call')
  })

  it('always names the cost bar it judged against', () => {
    for (const tilt of ['favors-waiting', 'unclear'] as Tilt[]) {
      const c = shortTermConsensus([row('1h', tilt)])
      expect(verdictSentence(c, 0.49)).toMatch(/0\.49%/)
    }
  })
})
