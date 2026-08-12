import { describe, expect, it } from 'vitest'
import {
  CUSTOM_INTERVALS,
  FALLBACK_INTERVAL,
  INTERVALS,
  PRESET_INTERVALS,
  defaultHistorySeconds,
  intervalLabel,
  intervalSeconds,
  isIntraday,
  isSupported,
  parseInterval,
  resolveInterval,
  supportedIntervals,
  type IntervalId
} from '../chart/intervals'
import type { CandleCoverage } from '../api/types'

/** Production shape: 5-minute ticks since 2026-07-20, daily backfill before it. */
function coverage(overrides: Partial<CandleCoverage> = {}): CandleCoverage {
  return {
    base_granularity_seconds: 300,
    intraday_from: '2026-07-20T00:00:00Z',
    history_from: '2022-04-20T00:00:00Z',
    supported_intervals: INTERVALS.map((d) => d.id),
    note: '',
    ...overrides
  }
}

/** The shape IR_GOLD_18K had before 2026-07-20: one observation per day. */
const DAILY_ONLY = coverage({
  base_granularity_seconds: 86_400,
  intraday_from: null,
  supported_intervals: ['1d', '2d', '3d', '1w']
})

describe('interval table', () => {
  it('has unique ids and strictly increasing durations', () => {
    const ids = INTERVALS.map((d) => d.id)
    expect(new Set(ids).size).toBe(ids.length)
    for (let i = 1; i < INTERVALS.length; i++) {
      expect(INTERVALS[i].seconds).toBeGreaterThan(INTERVALS[i - 1].seconds)
    }
  })

  it('splits every interval into exactly one of presets or custom', () => {
    const combined = [...PRESET_INTERVALS, ...CUSTOM_INTERVALS].sort()
    expect(combined).toEqual(INTERVALS.map((d) => d.id).sort())
    expect(PRESET_INTERVALS.filter((id) => CUSTOM_INTERVALS.includes(id))).toEqual([])
  })

  it('offers the six presets the toolbar promises', () => {
    expect(PRESET_INTERVALS).toEqual(['15m', '30m', '1h', '4h', '1d', '1w'])
    expect(PRESET_INTERVALS.map(intervalLabel)).toEqual(['15m', '30m', '1H', '4H', '1D', '1W'])
  })

  it('knows which timeframes are sub-daily', () => {
    expect(isIntraday('12h')).toBe(true)
    expect(isIntraday('1d')).toBe(false)
    expect(isIntraday('1w')).toBe(false)
    expect(intervalSeconds('1w')).toBe(604_800)
  })

  it('asks for a bounded default history span per interval', () => {
    for (const def of INTERVALS) {
      expect(defaultHistorySeconds(def.id)).toBe(def.seconds * def.defaultBars)
      expect(def.defaultBars).toBeGreaterThan(0)
    }
  })
})

describe('parseInterval', () => {
  it('accepts the canonical ids and the legacy aliases', () => {
    expect(parseInterval('15m')).toBe('15m')
    expect(parseInterval('1D')).toBe('1d')
    expect(parseInterval('hourly')).toBe('1h')
    expect(parseInterval('daily')).toBe('1d')
  })

  it('rejects anything else rather than guessing', () => {
    expect(parseInterval('7m')).toBeNull()
    expect(parseInterval('')).toBeNull()
    expect(parseInterval(null)).toBeNull()
    expect(parseInterval(undefined)).toBeNull()
  })
})

describe('isSupported', () => {
  it('allows every timeframe once intraday observations exist', () => {
    for (const def of INTERVALS) {
      expect(isSupported(def.id, coverage())).toEqual({ ok: true })
    }
  })

  it('withholds intraday timeframes when the symbol has one observation a day', () => {
    const result = isSupported('15m', DAILY_ONLY)
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.reason).toContain('15m')
      expect(result.reason).toContain('daily source data')
    }
  })

  it('falls back to the intraday_from reason when granularity is unknown', () => {
    const result = isSupported('15m', coverage({ base_granularity_seconds: null, intraday_from: null }))
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.reason).toMatch(/one per day/i)
  })

  it('still allows daily and above on a daily-only symbol', () => {
    expect(isSupported('1d', DAILY_ONLY)).toEqual({ ok: true })
    expect(isSupported('1w', DAILY_ONLY)).toEqual({ ok: true })
  })

  it('refuses a timeframe finer than the source granularity, and says so', () => {
    const result = isSupported('5m', coverage({ base_granularity_seconds: 900 }))
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.reason).toContain('15-minute')
  })

  it("honours the server's own list when nothing physical rules the timeframe out", () => {
    const result = isSupported('45m', coverage({ supported_intervals: ['5m', '1h', '1d'] }))
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.reason).toContain('not available for the current data source')
  })

  it('is permissive when coverage has not arrived yet', () => {
    expect(isSupported('5m', null)).toEqual({ ok: true })
    expect(isSupported('5m', undefined)).toEqual({ ok: true })
  })

  it('lists only the timeframes a daily-only symbol can serve', () => {
    expect(supportedIntervals(DAILY_ONLY)).toEqual(['1d', '2d', '3d', '1w'])
  })
})

describe('resolveInterval', () => {
  it('keeps a supported timeframe and says nothing', () => {
    expect(resolveInterval('15m', coverage())).toEqual({ interval: '15m', notice: null })
  })

  it('falls back to 1d and explains why when a saved timeframe stops being servable', () => {
    const resolved = resolveInterval('15m', DAILY_ONLY)
    expect(resolved.interval).toBe(FALLBACK_INTERVAL)
    expect(resolved.interval).toBe('1d')
    expect(resolved.notice).toContain('15m')
    expect(resolved.notice).toContain('1D')
    expect(resolved.notice).toContain('daily source data')
  })

  it('defaults to 1d when nothing was saved', () => {
    expect(resolveInterval(null, coverage())).toEqual({ interval: '1d', notice: null })
  })

  it('never substitutes silently — every fallback carries a reason', () => {
    const intraday: IntervalId[] = ['5m', '15m', '1h', '4h', '12h']
    for (const id of intraday) {
      const resolved = resolveInterval(id, DAILY_ONLY)
      expect(resolved.interval).toBe('1d')
      expect(resolved.notice).toBeTruthy()
    }
  })
})
