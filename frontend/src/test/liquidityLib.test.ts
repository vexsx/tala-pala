import { describe, expect, it } from 'vitest'
import {
  CODE_BASE,
  CODE_M2,
  MONEY_STALE_AFTER_DAYS,
  annualFigure,
  annualGrowthRows,
  annualLevelRows,
  annualMethodSpan,
  annualSortValue,
  backtestStatements,
  bandEdges,
  bandTarget,
  bandUndercovers,
  bestModel,
  breakBetween,
  coverageClauses,
  coverageSentence,
  dollarCloseAge,
  dollarRows,
  esfandTicks,
  formatMoney,
  formatP,
  hasMoneyData,
  historyMoneyRunStart,
  indexBasePeriod,
  indexExtremes,
  indexRows,
  isIsolated,
  jalaliDatePeriod,
  lineKeys,
  longHorizonNote,
  methodChanges,
  methodSwitches,
  moneyIsStale,
  moneyRows,
  moneySortValue,
  moneyTableRows,
  moneyUnit,
  monthlyRunStart,
  multiplierRows,
  normalizeDm,
  ordinalPeriod,
  parseCoverageKey,
  parsePeriod,
  periodLabel,
  periodLabelFa,
  periodOrdinal,
  periodTickLabel,
  periodTicks,
  scenarioKey,
  sortNullsLast,
  tehranDaysBetween,
  toDisplayMoney,
  type SeriesRow
} from '../lib/liquidity'
import type { LiquidityAggregatesResponse, LiquidityDollarResponse } from '../api/types'

// HAND-BUILT from the research data, pending a production capture — see
// fixtures/README.md ("Money & liquidity"). Every number is CBI's or TGJU's
// as the research read it; the API's arithmetic is redone by the generator
// there; the prose fields are the generator's own.
import monthlyJson from './fixtures/liquidity-aggregates-monthly.json'
import annualJson from './fixtures/liquidity-aggregates-annual.json'
import emptyJson from './fixtures/liquidity-aggregates-not-ingested.json'
import dollar12Json from './fixtures/liquidity-dollar-12.json'
import dollar24Json from './fixtures/liquidity-dollar-24.json'

const MONTHLY = monthlyJson as LiquidityAggregatesResponse
const ANNUAL = annualJson as LiquidityAggregatesResponse
const EMPTY = emptyJson as LiquidityAggregatesResponse
const D12 = dollar12Json as unknown as LiquidityDollarResponse
const D24 = dollar24Json as unknown as LiquidityDollarResponse

const rowAt = (rows: SeriesRow[], period: string) => rows.find((r) => r.period === period)!

/**
 * The API as Track M builds it lists the base-money change as ONE break at
 * 1399-12 (with the old-method 1400 prints in `old_method_after`) instead of
 * one entry per switch. Everything the page says about the change has to
 * hold for both shapes, so it is read from the points' own methods.
 */
const ONE_BREAK = [
  {
    code: CODE_BASE,
    from: '1399-12',
    note: 'CBI redefined base money from Esfand 1399.',
    old_method_after: Array.from({ length: 11 }, (_, i) => `1400-${String(i + 1).padStart(2, '0')}`)
  }
]
const ANNUAL_ONE_BREAK: LiquidityAggregatesResponse = { ...ANNUAL, breaks: ONE_BREAK }

describe('Jalali periods', () => {
  it('reads, orders and names a month', () => {
    expect(parsePeriod('1404-12')).toEqual({ jy: 1404, jm: 12 })
    expect(parsePeriod('1404-13')).toBeNull()
    expect(parsePeriod('1404/12')).toBeNull()
    expect(periodOrdinal('1404-12')).toBe(1404 * 12 + 11)
    // Esfand is followed by Farvardin of the next year, not month 13.
    expect(ordinalPeriod(periodOrdinal('1404-12')! + 1)).toBe('1405-01')
    expect(ordinalPeriod(periodOrdinal('1405-01')! - 1)).toBe('1404-12')
    for (const p of ['1352-03', '1399-12', '1400-01', '1405-07']) {
      expect(ordinalPeriod(periodOrdinal(p)!)).toBe(p)
    }
    expect(periodLabel('1404-12')).toBe('Esfand 1404')
    expect(periodLabel('1397-04')).toBe('Tir 1397')
    expect(periodLabel('1405-01')).toBe('Farvardin 1405')
    expect(periodLabel('not a month')).toBe('not a month')
    expect(periodLabelFa('1404-12')).toBe('اسفند ۱۴۰۴')
    expect(periodLabelFa('1405-07')).toBe('مهر ۱۴۰۵')
    expect(jalaliDatePeriod('1405/07/18')).toBe('1405-07')
    expect(jalaliDatePeriod('2026-10-10')).toBeNull()
  })

  it('puts ticks on real month boundaries', () => {
    const min = periodOrdinal('1352-03')!
    const max = periodOrdinal('1404-12')!
    const long = periodTicks(min, max)
    expect(long.length).toBeLessThanOrEqual(8)
    // Every tick is a Farvardin, labelled with its year.
    for (const t of long) expect(ordinalPeriod(t).endsWith('-01')).toBe(true)
    expect(periodTickLabel(periodOrdinal('1400-01')!, max - min)).toBe('1400')
    // A short span ticks quarters, labelled with the month.
    const short = periodTicks(periodOrdinal('1405-07')!, periodOrdinal('1406-07')!)
    expect(short.map(ordinalPeriod)).toEqual(['1405-07', '1405-10', '1406-01', '1406-04', '1406-07'])
    expect(periodTickLabel(periodOrdinal('1406-01')!, 12)).toBe('1406-01')
  })

  it('ticks a yearly chart at its own Esfands, always including the newest', () => {
    const esfands = Array.from({ length: 53 }, (_, i) => (1352 + i) * 12 + 11)
    const ticks = esfandTicks(esfands)
    expect(ticks.length).toBeLessThanOrEqual(8)
    expect(ticks[ticks.length - 1]).toBe(1404 * 12 + 11)
    for (const t of ticks) expect(ordinalPeriod(t).endsWith('-12')).toBe(true)
    expect(esfandTicks(esfands.slice(-5))).toEqual(esfands.slice(-5))
  })

  it('counts the days between two Tehran dates', () => {
    // Esfand 1404 ended 2026-03-20; its workbook was saved 2026-06-21 05:41 UTC.
    expect(tehranDaysBetween('2026-03-20', '2026-06-21T05:41:00Z')).toBe(93)
    expect(tehranDaysBetween('2026-03-20', 'garbage')).toBeNull()
  })
})

describe('units', () => {
  it('shows a CBI stock in همت with toman on, and as published with rial on', () => {
    expect(MONTHLY.unit).toBe('thousand_billion_rial')
    expect(moneyUnit('IRT')).toMatchObject({ divisor: 10, label: 'همت' })
    expect(moneyUnit('IRR')).toMatchObject({ divisor: 1, label: 'thousand billion rial' })
    // Esfand 1404 liquidity: 155,812.2 thousand billion rial = 15,581.22 همت.
    expect(toDisplayMoney(155812.2, 'IRT')).toBeCloseTo(15581.22, 6)
    expect(formatMoney(155812.2, 'IRT')).toBe('15,581 همت')
    expect(formatMoney(155812.2, 'IRR')).toBe('155,812 thousand billion rial')
    // The 1350s are fractions of one همت: shown, not rounded to zero.
    expect(formatMoney(0.518, 'IRT')).toBe('0.052 همت')
    expect(formatMoney(null, 'IRT')).toBe('—')
  })
})

describe('the money series', () => {
  it('knows when nothing is stored', () => {
    expect(hasMoneyData(MONTHLY)).toBe(true)
    expect(hasMoneyData(EMPTY)).toBe(false)
  })

  it('reads the dollar close’s age from either shape of the dollar’s data_age', () => {
    expect(dollarCloseAge({ stale: true, age_days: 9 })).toEqual({ stale: true, age_days: 9 })
    // One block per input, as Track M builds it: the dollar's is `usd`.
    const nested = { money: { stale: true, age_days: 205 }, usd: { stale: false, age_days: 1 } }
    expect(dollarCloseAge(nested)).toEqual({ stale: false, age_days: 1 })
    expect(dollarCloseAge(undefined)).toEqual({})
  })

  it('calls the newest month old by the server’s word, or past CBI’s longest lag', () => {
    expect(MONTHLY.latest!.age_days).toBe(205)
    expect(moneyIsStale(MONTHLY.latest, MONTHLY.data_age)).toBe(true)
    // Without a server verdict the page's own bound decides.
    expect(moneyIsStale(MONTHLY.latest, {})).toBe(true)
    expect(moneyIsStale({ ...MONTHLY.latest!, age_days: MONEY_STALE_AFTER_DAYS }, {})).toBe(false)
    expect(moneyIsStale({ ...MONTHLY.latest!, age_days: 60 }, {})).toBe(false)
    // The server's verdict wins either way.
    expect(moneyIsStale({ ...MONTHLY.latest!, age_days: 60 }, { stale: true })).toBe(true)
    expect(moneyIsStale(null, {})).toBe(false)
  })

  it('never puts base money on two methods under one line', () => {
    const rows = moneyRows(MONTHLY, (p) => p.value)
    expect(lineKeys(rows, 'base')).toEqual(['base_old', 'base_revised'])
    for (const r of rows) {
      expect(r.base_old !== null && r.base_revised !== null).toBe(false)
      // Every row carries every key: a gap is null, never a missing key a
      // chart library might bridge.
      expect(r).toHaveProperty('base_old')
      expect(r).toHaveProperty('base_revised')
      expect(r).toHaveProperty('m2')
    }
    // CBI's methods in the stored months: old to 1399-11, revised at 1399-12,
    // old through 1400-11, revised from 1400-12.
    expect(rowAt(rows, '1399-11').base_old).toBe(4361.0)
    expect(rowAt(rows, '1399-12').base_revised).toBe(4506.7)
    expect(rowAt(rows, '1399-12').base_old).toBeNull()
    expect(rowAt(rows, '1400-01').base_old).toBe(4652.5)
    expect(rowAt(rows, '1400-12').base_revised).toBe(5981.1)
    // The lone revised month has no neighbour on its line: drawn as a dot.
    const i = rows.findIndex((r) => r.period === '1399-12')
    expect(isIsolated(rows, i, 'base_revised')).toBe(true)
    expect(isIsolated(rows, i + 1, 'base_old')).toBe(false)
    expect(isIsolated(rows, i, 'm2')).toBe(false)
  })

  it('draws growth as the API measured it, leaving a gap where it did not', () => {
    const rows = moneyRows(MONTHLY, (p) => p.yoy_pct)
    expect(rowAt(rows, '1404-12').m2).toBe(53.269)
    expect(rowAt(rows, '1404-12').base_revised).toBe(61.533)
    // Across the method change the API returns null; nothing fills it.
    expect(rowAt(rows, '1399-12').base_revised).toBeNull()
    expect(rowAt(rows, '1399-12').base_old).toBeNull()
    expect(rowAt(rows, '1401-06').base_revised).toBeNull()
  })

  it('converts levels in the builder and trims to a window', () => {
    const from = periodOrdinal('1395-01')!
    const rows = moneyRows(MONTHLY, (p) => toDisplayMoney(p.value, 'IRT'), { fromX: from })
    expect(rows[0].x).toBeGreaterThanOrEqual(from)
    expect(rowAt(rows, '1404-12').m2).toBeCloseTo(15581.22, 6)
  })

  it('splits the multiplier by the method of the base money it divides by', () => {
    const rows = multiplierRows(MONTHLY)
    expect(rowAt(rows, '1404-12').mult_revised).toBe(7.095)
    expect(rowAt(rows, '1399-11').mult_old).not.toBeNull()
    const i = rows.findIndex((r) => r.period === '1399-12')
    expect(isIsolated(rows, i, 'mult_revised')).toBe(true)
    // Annual: Esfand points only.
    expect(multiplierRows(MONTHLY, { esfandOnly: true }).every((r) => r.period.endsWith('-12'))).toBe(true)
  })

  it('reads every method switch from the points, however the breaks are listed', () => {
    const switches = ['1399-12', '1400-01', '1400-12']
    expect(methodSwitches(MONTHLY).map((s) => s.from)).toEqual(switches)
    expect(methodChanges(MONTHLY).map((s) => s.from)).toEqual(switches)
    // One listed break: the same three switches.
    expect(methodChanges({ ...MONTHLY, breaks: ONE_BREAK }).map((s) => s.from)).toEqual(switches)
    // No break listed at all: the points still say it.
    expect(methodChanges({ ...MONTHLY, breaks: [] }).map((s) => s.from)).toEqual(switches)
    // Liquidity has no method.
    expect(methodSwitches(MONTHLY, CODE_M2)).toEqual([])
    // A month with no recorded method is not a switch.
    const unrecorded = {
      ...MONTHLY,
      breaks: [],
      series: MONTHLY.series.map((s) =>
        s.code === CODE_BASE
          ? { ...s, points: s.points.map((p) => (p.period === '1399-12' ? { ...p, method: null } : p)) }
          : s
      )
    }
    expect(methodSwitches(unrecorded).map((s) => s.from)).toEqual(['1400-12'])
  })

  it('finds where the unbroken monthly run starts, from the data', () => {
    expect(monthlyRunStart(MONTHLY.series[0].points)).toBe('1397-04')
    // As the CBI ingest stores it: monthly from Tir 1398, with Esfand 1397
    // alone among quarter-ends before it.
    const ingest = MONTHLY.series[0].points
      .filter((p) => p.period >= '1398-04' || ['1397-06', '1397-09', '1397-12', '1398-03'].includes(p.period) || p.period < '1397-04')
      .map((p) => (p.period < '1398-04' && p.period !== '1397-12' ? { ...p, frequency: 'Q' as const } : p))
    expect(monthlyRunStart(ingest)).toBe('1398-04')
    // Order does not matter.
    expect(monthlyRunStart([...ingest].reverse())).toBe('1398-04')
    expect(monthlyRunStart([])).toBeNull()
    // The dollar history carries no frequency: the quarter-end Khordad 1397
    // touches Tir 1397, so the unbroken run of months with liquidity starts
    // there — and every month before it is still one of only some.
    expect(historyMoneyRunStart(D12.history)).toBe('1397-03')
    expect(historyMoneyRunStart(D12.history.map((h) => ({ ...h, m2: null })))).toBeNull()
  })

  it('finds a break between two months', () => {
    expect(breakBetween(MONTHLY.breaks, CODE_BASE, '1398-12', '1399-12')).toBe(true)
    expect(breakBetween(MONTHLY.breaks, CODE_BASE, '1401-12', '1402-12')).toBe(false)
    expect(breakBetween(MONTHLY.breaks, CODE_M2, '1398-12', '1399-12')).toBe(false)
  })
})

describe('the annual view', () => {
  const year = (y: number, resp: LiquidityAggregatesResponse = ANNUAL) => resp.annual!.find((a) => a.year === y)!

  it('reads Esfand-over-Esfand and yearly-average growth from the annual block', () => {
    const end = annualGrowthRows(ANNUAL, 'end', 1397)
    expect(end.find((r) => r.year === 1404)).toEqual({ year: 1404, m2: 53.269, base: 61.533 })
    expect(end.find((r) => r.year === 1399)!.base).toBeNull()
    expect(end[0].year).toBe(1397)
    const avg = annualGrowthRows(ANNUAL, 'avg')
    expect(avg.find((r) => r.year === 1404)!.m2).toBe(39.74)
    expect(avg.find((r) => r.year === 1396)!.m2).toBeNull()
  })

  it('reads which years compare two methods from the points, with one break or three', () => {
    for (const resp of [ANNUAL, ANNUAL_ONE_BREAK]) {
      const spans = (field: 'end_growth_pct' | 'avg_growth_pct' | 'avg_value') =>
        resp.annual!.filter((a) => annualMethodSpan(resp, a, CODE_BASE, field) !== null).map((a) => a.year)
      // Esfand 1399 (revised) against Esfand 1398 (old). Esfand 1400 against
      // Esfand 1399 is revised against revised: one definition.
      expect(spans('end_growth_pct')).toEqual([1399])
      // A yearly average reads every month: 1399 and 1400 hold both methods.
      expect(spans('avg_value')).toEqual([1399, 1400])
      expect(spans('avg_growth_pct')).toEqual([1399, 1400, 1401])
      expect(annualMethodSpan(resp, year(1399, resp), CODE_M2, 'end_growth_pct')).toBeNull()
    }
  })

  it('says why an annual figure is missing, the same with one break listed', () => {
    for (const resp of [ANNUAL, ANNUAL_ONE_BREAK]) {
      expect(annualFigure(resp, year(1399, resp), CODE_BASE, 'end_growth_pct').reason).toMatch(/changed its base-money method/)
      expect(annualFigure(resp, year(1400, resp), CODE_BASE, 'avg_growth_pct').reason).toMatch(/changed its base-money method/)
      // 1401 and 1400 both have all twelve months: the gap is the method, not a missing month.
      const y1401 = annualFigure(resp, year(1401, resp), CODE_BASE, 'avg_growth_pct')
      expect(y1401).toEqual({ value: null, reason: expect.stringMatching(/changed its base-money method/) })
      expect(annualFigure(resp, year(1398, resp), CODE_M2, 'avg_growth_pct').reason).toMatch(
        /all twelve monthly stocks of 1398 and of 1397; before Tir 1397 only some months are stored/
      )
      expect(annualFigure(resp, year(1404, resp), CODE_M2, 'end_growth_pct')).toEqual({ value: 53.269, reason: null })
    }
  })

  it('prefers the API’s own reason when it gives one', () => {
    const withReason = {
      ...year(1398),
      null_reason: { [CODE_M2]: { avg_growth_pct: '1397 does not have all twelve months' } }
    }
    expect(annualFigure(ANNUAL, withReason, CODE_M2, 'avg_growth_pct')).toEqual({
      value: null,
      reason: '1397 does not have all twelve months'
    })
  })

  it('takes no yearly average across a method change, even when a response carries one', () => {
    for (const y of [1399, 1400]) {
      // As Track M's annual block does: a mean of all twelve months whatever their method.
      const served = { ...year(y), avg_value: { ...year(y).avg_value!, [CODE_BASE]: 5000 } }
      expect(annualFigure(ANNUAL, served, CODE_BASE, 'avg_value')).toEqual({
        value: null,
        reason: `CBI changed its base-money method during ${y}: a mean across the change would mix two definitions`
      })
      expect(annualFigure(ANNUAL, served, CODE_M2, 'avg_value').value).not.toBeNull()
      // …and it sorts with the missing, as it is shown.
      expect(annualSortValue(ANNUAL, served, 'base_avg')).toBeNull()
    }
    // Nor a growth across the change.
    const growth = { ...year(1399), end_growth_pct: { ...year(1399).end_growth_pct, [CODE_BASE]: 30.052 } }
    expect(annualFigure(ANNUAL, growth, CODE_BASE, 'end_growth_pct').value).toBeNull()
    expect(annualFigure(ANNUAL, year(1396), CODE_M2, 'avg_value').reason).toMatch(/needs all twelve monthly stocks of 1396/)
    expect(annualFigure(ANNUAL, year(1401), CODE_BASE, 'avg_value')).toEqual({ value: 7092.12, reason: null })
  })

  it('says a mean or growth over a month with no recorded method is not taken', () => {
    const unrecorded: LiquidityAggregatesResponse = {
      ...ANNUAL,
      series: ANNUAL.series.map((s) =>
        s.code === CODE_BASE
          ? { ...s, points: s.points.map((p) => (p.period === '1402-05' ? { ...p, method: null } : p)) }
          : s
      )
    }
    const y = unrecorded.annual!.find((a) => a.year === 1402)!
    expect(annualFigure(unrecorded, y, CODE_BASE, 'avg_value')).toEqual({
      value: null,
      reason: 'the months of 1402 are not all on one recorded base-money method, so no mean is taken across them'
    })
  })

  it('places each Esfand stock on the month axis, split by method', () => {
    const rows = annualLevelRows(ANNUAL, 'IRT', 1398)
    expect(rows.every((r) => r.period.endsWith('-12'))).toBe(true)
    expect(rowAt(rows, '1404-12').m2).toBeCloseTo(15581.22, 6)
    expect(rowAt(rows, '1398-12').base_old).toBeCloseTo(352.85, 6)
    expect(rowAt(rows, '1399-12').base_revised).toBeCloseTo(450.67, 6)
  })
})

describe('the data table', () => {
  it('pairs the two series and the multiplier by month', () => {
    const rows = moneyTableRows(MONTHLY)
    const esfand = rows.find((r) => r.period === '1404-12')!
    expect(esfand.m2!.value).toBe(155812.2)
    expect(esfand.base!.value).toBe(21959.3)
    expect(esfand.multiplier).toBe(7.095)
    expect(moneySortValue(esfand, 'm2_ytd')).toBe(53.269)
    expect(moneySortValue(esfand, 'base')).toBe(21959.3)
  })

  it('sorts a missing figure last both ways', () => {
    const rows = moneyTableRows(MONTHLY, periodOrdinal('1399-01')!)
    for (const desc of [true, false]) {
      const sorted = sortNullsLast(rows, (r) => moneySortValue(r, 'base_yoy'), desc)
      expect(moneySortValue(sorted[sorted.length - 1], 'base_yoy')).toBeNull()
      expect(moneySortValue(sorted[0], 'base_yoy')).not.toBeNull()
    }
  })
})

describe('the dollar chart', () => {
  const P0 = D12.anchor!.close_toman

  it('keeps the measured price and every path under separate keys', () => {
    const rows = dollarRows(D12, { bandKey: 'last_12m' })
    const anchor = rowAt(rows, '1405-07')
    // The measured series ends at the anchor month…
    expect(anchor.actual).toBe(265100)
    expect(rows.filter((r) => r.actual !== null).every((r) => r.x <= anchor.x)).toBe(true)
    // …and each path starts there, by repeating the same close: nothing invented.
    for (const s of D12.scenarios) {
      expect(anchor[scenarioKey(s.key)]).toBe(P0)
      expect(rows.filter((r) => r[scenarioKey(s.key)] !== null).every((r) => r.x >= anchor.x)).toBe(true)
    }
    expect(anchor.trend).toBe(P0)
    // No future month carries a measured value.
    expect(rowAt(rows, '1406-07').actual).toBeNull()
    expect(rowAt(rows, '1406-07')[scenarioKey('last_12m')]).toBe(406315)
    expect(rowAt(rows, '1406-07').trend).toBe(370100)
  })

  it('draws the band from the path and its own quantiles, from zero width at the anchor', () => {
    const rows = dollarRows(D12, { bandKey: 'last_12m' })
    expect(rowAt(rows, '1405-07').band).toEqual([P0, P0])
    // The research's band at 12 months for the 53.3% path: 311k–860k.
    const [lo, hi] = rowAt(rows, '1406-07').band as [number, number]
    expect(Math.round(lo / 100) * 100).toBe(310900)
    expect(Math.round(hi / 100) * 100).toBe(860400)
    expect(bandEdges(100, { h: 3, lo: -0.1996, hi: 0.2807 })[0]).toBeCloseTo(100 * Math.exp(-0.1996), 9)
    // Only the chosen path carries it.
    expect(rows.every((r) => r.band === null || r.x >= rowAt(rows, '1405-07').x)).toBe(true)
  })

  it('leaves the band out at a horizon it does not cover, rather than drawing between neighbours', () => {
    const gappy: LiquidityDollarResponse = {
      ...D12,
      band: { ...D12.band!, by_h: D12.band!.by_h.filter((s) => s.h !== 5) }
    }
    const rows = dollarRows(gappy, { bandKey: 'avg_3y' })
    const p5 = D12.scenarios[1].path.find((p) => p.h === 5)!.period
    expect(rowAt(rows, p5).band).toBeNull()
    const p6 = D12.scenarios[1].path.find((p) => p.h === 6)!.period
    expect(rowAt(rows, p6).band).not.toBeNull()
  })

  it('wraps the scenario the API names, or the reader’s pick when it wraps each', () => {
    const keys = D12.scenarios.map((s) => s.key)
    expect(bandTarget(D12.band, keys, null)).toBe('last_12m')
    expect(bandTarget(D12.band, keys, 'slowest_3y')).toBe('slowest_3y')
    const named = { ...D12.band!, applied_to: 'avg_3y' }
    expect(bandTarget(named, keys, 'slowest_3y')).toBe('avg_3y')
    expect(bandTarget({ ...named, applied_to: 'gone' }, keys, null)).toBeNull()
    expect(bandTarget(null, keys, null)).toBeNull()
  })

  it('trims history to the window and keeps every future month', () => {
    const from = periodOrdinal('1402-08')!
    const rows = dollarRows(D24, { fromX: from, bandKey: null })
    expect(rows[0].period).toBe('1402-08')
    expect(rows[rows.length - 1].period).toBe('1407-07')
    expect(rows.some((r) => r.band != null)).toBe(false)
  })
})

describe('the band’s coverage', () => {
  it('is labelled with the measured coverage, never a nominal level', () => {
    expect(parseCoverageKey('1-12')).toEqual({ from: 1, to: 12 })
    expect(parseCoverageKey('24')).toEqual({ from: 24, to: 24 })
    expect(parseCoverageKey('12-1')).toBeNull()
    expect(coverageClauses(D12.band!.coverage)).toEqual([
      'about 84% of the time at 1–12 months',
      'about 70% of the time at 24 months'
    ])
    const s = coverageSentence(D12.band!)
    expect(s).toBe(
      'In the backtest from Farvardin 1394, a band built this way held the actual price about 84% of the time at 1–12 months and about 70% of the time at 24 months.'
    )
    // 0.05 and 0.95 quantiles would read as a "90% band": never said.
    expect(s).not.toMatch(/90|95|confiden|probabil/)
    expect(coverageSentence({ ...D12.band!, coverage: {} })).toMatch(/carries no probability/)
  })

  it('knows when the band held the price less often than its quantiles would', () => {
    // 5%–95% quantiles would hold 90% of outcomes; it held 84% and 70%.
    expect(bandUndercovers(D12.band!)).toBe(true)
    expect(bandUndercovers({ ...D12.band!, coverage: { '1-12': 0.92, '24': 0.7 } })).toBe(false)
    expect(bandUndercovers({ ...D12.band!, coverage: {} })).toBe(false)
  })

  it('says beside the 24-month choice how the longer band did, from the numbers', () => {
    expect(longHorizonNote(D12.band)).toBe(
      'in the backtest the band held the actual price about 70% of the time at 24 months — less often than within 12 months (about 84% of the time at 1–12 months).'
    )
    const better = { ...D12.band!, coverage: { '1-12': 0.84, '24': 0.9 } }
    expect(longHorizonNote(better)).toContain('more often than within 12 months')
    expect(longHorizonNote({ ...D12.band!, coverage: { '1-12': 0.84 } })).toMatch(/not measured/)
    expect(longHorizonNote(null)).toBeNull()
  })
})

describe('the dollar per unit of liquidity', () => {
  it('finds its base and its range', () => {
    expect(indexBasePeriod(D12.history)).toBe('1397-04')
    const rows = indexRows(D12.history)
    expect(rows.every((r) => r.index !== null)).toBe(true)
    // 1405-07 has no liquidity figure: no index, not a zero.
    expect(rows.find((r) => r.period === '1405-07')).toBeUndefined()
    expect(rows[rows.length - 1]).toMatchObject({ period: '1404-12', index: 204.581 })
    const ext = indexExtremes(rows)!
    expect(ext.min.period).toBe('1396-06')
    expect(ext.max.period).toBe('1404-11')
    expect(indexExtremes([])).toBeNull()
  })
})

describe('the backtest', () => {
  const bt = D12.backtest!

  it('reads either nesting of the p-value maps', () => {
    expect(normalizeDm(bt.dm_vs_trend_p)).toEqual({ m2_paced: bt.dm_vs_trend_p.m2_paced })
    expect(normalizeDm({ '3': 0.2, '12': 0.3 })).toEqual({ m2_paced: { '3': 0.2, '12': 0.3 } })
    expect(normalizeDm(null)).toEqual({})
  })

  it('states what the research numbers show', () => {
    expect(backtestStatements(bt)).toEqual([
      'Against assuming no change: measurably smaller misses at every horizon tested, 3, 6, 12 and 24 months (p ≤ 0.002).',
      'Against the dollar’s own past trend: no measurable difference at any horizon tested, 3, 6, 12 and 24 months (p 0.16–0.30).',
      'In this test, pacing the dollar with liquidity growth added no skill the dollar’s own trend did not already have.'
    ])
    for (const h of ['3', '6', '12', '24']) expect(bestModel(bt, h)).toBe('m2_paced')
  })

  it('would say something else if the numbers did', () => {
    const better = {
      ...bt,
      dm_vs_trend_p: { m2_paced: { ...(bt.dm_vs_trend_p.m2_paced as Record<string, number>), '3': 0.01 } }
    }
    const s = backtestStatements(better)
    expect(s[1]).toBe(
      'Against the dollar’s own past trend: measurably smaller misses at 3 months (p ≤ 0.01), no measurable difference at 6, 12 and 24 months (p 0.22–0.30).'
    )
    expect(s[2]).toContain('at 3 months only')
    const flat = { ...bt, dm_vs_random_walk_p: { '3': 0.4, '6': 0.5, '12': 0.6, '24': 0.7 }, dm_vs_trend_p: {} }
    const f = backtestStatements(flat)
    expect(f[0]).toBe(
      'Against assuming no change: no measurable difference at any horizon tested, 3, 6, 12 and 24 months (p 0.40–0.70).'
    )
    expect(f[1]).toMatch(/No test against the dollar’s own trend is reported/)
  })

  it('formats p-values without overstating them', () => {
    expect(formatP(0)).toBe('< 0.001')
    expect(formatP(0.0018)).toBe('0.002')
    expect(formatP(0.3047)).toBe('0.30')
  })
})
