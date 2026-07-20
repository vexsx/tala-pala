import { describe, expect, it } from 'vitest'
import {
  convertDisplay,
  formatCompact,
  formatJalaliDate,
  formatPct,
  formatToman,
  gregorianToJalali,
  pctClass
} from '../lib/format'

describe('toman / rial formatting', () => {
  it('groups thousands with en digits', () => {
    expect(formatToman(8_120_000, 'IRT', false)).toBe('8,120,000')
  })

  it('appends the Persian toman label', () => {
    expect(formatToman(8_120_000, 'IRT')).toBe('8,120,000 تومان')
  })

  it('rial display multiplies by 10 (display-only)', () => {
    expect(convertDisplay(8_120_000, 'IRR')).toBe(81_200_000)
    expect(formatToman(8_120_000, 'IRR', false)).toBe('81,200,000')
    expect(formatToman(8_120_000, 'IRR')).toBe('81,200,000 ریال')
  })

  it('toman display is the identity conversion', () => {
    expect(convertDisplay(8_120_000, 'IRT')).toBe(8_120_000)
  })
})

describe('jalali conversion', () => {
  it('converts Nowruz: 2024-03-20 -> 1403-01-01', () => {
    expect(gregorianToJalali(2024, 3, 20)).toEqual({ jy: 1403, jm: 1, jd: 1 })
  })

  it('formats an ISO instant to a Jalali date in Asia/Tehran', () => {
    // 12:00 UTC is 15:30 in Tehran, still 2024-03-20 locally.
    expect(formatJalaliDate('2024-03-20T12:00:00Z')).toBe('1403/01/01')
  })

  it('round-trips another known date: 2025-09-23 -> 1404-07-01', () => {
    expect(gregorianToJalali(2025, 9, 23)).toEqual({ jy: 1404, jm: 7, jd: 1 })
  })
})

describe('percent formatting', () => {
  it('adds an explicit plus sign for gains', () => {
    expect(formatPct(1.234)).toBe('+1.23%')
  })

  it('keeps the minus sign for losses', () => {
    expect(formatPct(-2.5)).toBe('-2.50%')
  })

  it('renders a dash for missing values', () => {
    expect(formatPct(null)).toBe('—')
    expect(formatPct(undefined)).toBe('—')
  })

  it('maps sign to a color class', () => {
    expect(pctClass(3)).toBe('pos')
    expect(pctClass(-3)).toBe('neg')
    expect(pctClass(0)).toBe('flat')
    expect(pctClass(null)).toBe('flat')
  })
})

describe('compact numbers', () => {
  it('abbreviates millions', () => {
    expect(formatCompact(8_120_000)).toBe('8.1M')
  })
  it('abbreviates thousands', () => {
    expect(formatCompact(2_500)).toBe('2.5k')
  })
})
