import { describe, expect, it } from 'vitest'
import {
  CBI_LAG_DAYS,
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
  dollarMonthlyFrom,
  dollarRows,
  esfandTicks,
  formatMoney,
  formatMultiplier,
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
  measurablyBest,
  methodChanges,
  methodSwitches,
  moneyIsStale,
  moneyStaleAfterDays,
  moneyRows,
  moneyView,
  moneySortValue,
  moneyTableRows,
  moneyUnit,
  monthlyRunStart,
  multiplierRows,
  normalizeDm,
  ordinalPeriod,
  parseCoverageKey,
  parsePeriod,
  pathBand,
  periodLabel,
  periodLabelFa,
  periodOrdinal,
  periodTickLabel,
  periodTicks,
  publicationLag,
  quarterEndsOnly,
  rmseAgainstTrend,
  scenarioKey,
  seriesMonthlyFrom,
  sortNullsLast,
  tehranDaysBetween,
  toDisplayMoney,
  yoyRunStart,
  type SeriesRow
} from '../lib/liquidity'
import type { LiquidityAggregatesResponse, LiquidityDollarResponse, LiquidityPoint } from '../api/types'

// CAPTURED from the routes themselves, on the local rehearsal of real data
// (fixtures/README.md, "Money & liquidity"): CBI's 48 monthly workbooks and
// its tsdview quarter-ends, and USD_IRT's TGJU history plus one BitMax close,
// served by internal/liquidity on 2026-10-11. Every number below is what the
// API answered there; nothing is hand-built.
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
 * The same capture with some base-money months restated on CBI's revised
 * method, as a deployment that ingested more editions would hold them: the
 * Esfand 1402 workbook restates Esfand 1400 on the revised method (the
 * rehearsal never ingested it), and the research read Esfand 1399 revised
 * too. Only `method` moves; it is a stated transformation for the layouts
 * the page must also handle, not a fixture.
 */
function withMethods(resp: LiquidityAggregatesResponse, methods: Record<string, LiquidityPoint['method']>) {
  return {
    ...resp,
    series: resp.series.map((s) =>
      s.code === CODE_BASE
        ? { ...s, points: s.points.map((p) => (p.period in methods ? { ...p, method: methods[p.period] } : p)) }
        : s
    )
  }
}
const RESTATED_1400 = withMethods(MONTHLY, { '1400-12': 'revised' })
const RESEARCH_LAYOUT = withMethods(ANNUAL, { '1399-12': 'revised', '1400-12': 'revised' })

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
    expect(periodLabel('1398-04')).toBe('Tir 1398')
    expect(periodLabel('1405-01')).toBe('Farvardin 1405')
    expect(periodLabel('not a month')).toBe('not a month')
    expect(periodLabelFa('1404-12')).toBe('اسفند ۱۴۰۴')
    expect(periodLabelFa('1405-07')).toBe('مهر ۱۴۰۵')
    expect(jalaliDatePeriod(D12.anchor!.jalali)).toBe('1405-07')
    expect(jalaliDatePeriod('2026-10-11')).toBeNull()
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
    expect(MONTHLY.latest!.published_at).toBe('2026-06-21T05:41:31Z')
    expect(tehranDaysBetween('2026-03-20', MONTHLY.latest!.published_at!)).toBe(93)
    expect(tehranDaysBetween('2026-03-20', 'garbage')).toBeNull()
  })
})

describe('units', () => {
  it('shows a CBI stock in همت with toman on, and as published with rial on', () => {
    expect(MONTHLY.unit).toBe('thousand_billion_rial')
    expect(moneyUnit('IRT')).toMatchObject({ divisor: 10, label: 'همت' })
    expect(moneyUnit('IRR')).toMatchObject({ divisor: 1, label: 'thousand billion rial' })
    // Esfand 1404 liquidity: 155,812.2 thousand billion rial = 15,581.22 همت.
    expect(MONTHLY.latest!.period).toBe('1404-12')
    expect(MONTHLY.series[0].points[MONTHLY.series[0].points.length - 1].value).toBe(155812.2)
    expect(toDisplayMoney(155812.2, 'IRT')).toBeCloseTo(15581.22, 6)
    expect(formatMoney(155812.2, 'IRT')).toBe('15,581 همت')
    expect(formatMoney(155812.2, 'IRR')).toBe('155,812 thousand billion rial')
    // The 1350s are fractions of one همت: shown, not rounded to zero.
    expect(formatMoney(0.518, 'IRT')).toBe('0.052 همت')
    expect(formatMoney(null, 'IRT')).toBe('—')
  })

  it('prints the multiplier at CBI’s three decimals, trailing zeros kept', () => {
    expect(formatMultiplier(7.095499)).toBe('7.095')
    expect(formatMultiplier(7.72)).toBe('7.720')
    expect(formatMultiplier(8.1)).toBe('8.100')
    expect(formatMultiplier(null)).toBe('—')
  })
})

describe('how old the money figures are', () => {
  it('knows when nothing is stored', () => {
    expect(hasMoneyData(MONTHLY)).toBe(true)
    expect(hasMoneyData(EMPTY)).toBe(false)
  })

  it('reads the dollar close’s age from either shape of the dollar’s data_age', () => {
    expect(dollarCloseAge({ stale: true, age_days: 9 })).toEqual({ stale: true, age_days: 9 })
    // One block per input, as the API also sends: the dollar's is `usd`.
    expect(dollarCloseAge(D12.data_age)).toBe(D12.data_age.usd)
    expect(dollarCloseAge(D12.data_age).age_days).toBe(0)
    expect(dollarCloseAge(undefined)).toEqual({})
  })

  it('states the lag the API measured, and its bound, not figures of its own', () => {
    // The API's: 21 to 164 days, stale past 195 (the longest lag plus a month).
    expect(MONTHLY.data_age.publication_lag_days).toEqual({ min: 21, max: 164 })
    expect(MONTHLY.data_age.stale_after_days).toBe(195)
    expect(publicationLag(MONTHLY.data_age)).toEqual({ min: 21, max: 164 })
    expect(moneyStaleAfterDays(MONTHLY.data_age)).toBe(195)
    // The fallbacks are the same measured figures.
    expect(publicationLag({})).toEqual(CBI_LAG_DAYS)
    expect(CBI_LAG_DAYS).toEqual({ min: 21, max: 164 })
    expect(MONEY_STALE_AFTER_DAYS).toBe(195)
    // A different measurement from the API is the one stated.
    expect(publicationLag({ publication_lag_days: { min: 30, max: 200 } })).toEqual({ min: 30, max: 200 })
    expect(moneyStaleAfterDays({ stale_after_days: 231 })).toBe(231)
  })

  it('calls the newest month old by the server’s word, or past the measured bound', () => {
    expect(MONTHLY.latest!.age_days).toBe(205)
    expect(moneyIsStale(MONTHLY.latest, MONTHLY.data_age)).toBe(true)
    // Without a server verdict the bound decides: 1403-11 was 194 days old
    // when 1403-12 arrived 164 days late, and that was not late.
    expect(moneyIsStale(MONTHLY.latest, {})).toBe(true)
    expect(moneyIsStale({ ...MONTHLY.latest!, age_days: 194 }, {})).toBe(false)
    expect(moneyIsStale({ ...MONTHLY.latest!, age_days: 140 }, { stale_after_days: 195 })).toBe(false)
    expect(moneyIsStale({ ...MONTHLY.latest!, age_days: 196 }, { stale_after_days: 195 })).toBe(true)
    // The server's verdict wins either way.
    expect(moneyIsStale({ ...MONTHLY.latest!, age_days: 60 }, { stale: true })).toBe(true)
    expect(moneyIsStale(null, {})).toBe(false)
  })
})

describe('the money series', () => {
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
    // In the stored months base money is on the old method through 1401-11
    // and on the revised one from 1401-12, the first month an edition stored
    // here restates.
    expect(rowAt(rows, '1401-11').base_old).toBe(8086.4)
    expect(rowAt(rows, '1401-12').base_revised).toBe(8548)
    expect(rowAt(rows, '1401-12').base_old).toBeNull()
    expect(rowAt(rows, '1404-12').base_revised).toBe(21959.3)
    const i = rows.findIndex((r) => r.period === '1401-12')
    expect(isIsolated(rows, i, 'base_revised')).toBe(false)
    expect(isIsolated(rows, i, 'm2')).toBe(false)
  })

  it('draws a lone month on its own method as a dot', () => {
    // Esfand 1400 restated on the revised method between old-method months.
    const rows = moneyRows(RESTATED_1400, (p) => p.value)
    const i = rows.findIndex((r) => r.period === '1400-12')
    expect(rows[i].base_revised).toBe(6039.7)
    expect(isIsolated(rows, i, 'base_revised')).toBe(true)
    expect(isIsolated(rows, i + 1, 'base_old')).toBe(false)
  })

  it('draws growth as the API measured it, leaving a gap where it did not', () => {
    const rows = moneyRows(MONTHLY, (p) => p.yoy_pct)
    expect(rowAt(rows, '1404-12').m2).toBe(53.268706)
    expect(rowAt(rows, '1404-12').base_revised).toBe(61.533143)
    // Across the method change the API returns null; nothing fills it.
    expect(rowAt(rows, '1401-12').base_revised).toBeNull()
    expect(rowAt(rows, '1401-12').base_old).toBeNull()
    expect(rowAt(rows, '1402-06').base_revised).toBeNull()
    // Before a year after the monthly record starts only quarter-ends compare.
    expect(rowAt(rows, '1398-12').m2).toBe(31.295509)
    expect(rowAt(rows, '1399-02').m2).toBeNull()
    expect(rowAt(rows, '1399-04').m2).not.toBeNull()
  })

  it('converts levels in the builder and trims to a window', () => {
    const from = periodOrdinal('1395-01')!
    const rows = moneyRows(MONTHLY, (p) => toDisplayMoney(p.value, 'IRT'), { fromX: from })
    expect(rows[0].period).toBe('1395-03')
    expect(rowAt(rows, '1404-12').m2).toBeCloseTo(15581.22, 6)
  })

  it('splits the multiplier by the method of the base money it divides by', () => {
    const rows = multiplierRows(MONTHLY)
    expect(rowAt(rows, '1404-12').mult_revised).toBe(7.095499)
    expect(rowAt(rows, '1401-11').mult_old).toBe(7.547784)
    expect(rowAt(rows, '1401-12').mult_revised).toBe(7.414226)
    // Annual: Esfand points only.
    expect(multiplierRows(MONTHLY, { esfandOnly: true }).every((r) => r.period.endsWith('-12'))).toBe(true)
  })

  it('marks the method switches the stored months show, not the footnote’s date', () => {
    // The API dates the redefinition from 1399-12, but every stored month
    // from 1399-12 to 1401-11 is an old-method print: the only switch is at
    // 1401-12. A marker at 1399-12 would say the line breaks where it runs on.
    expect(MONTHLY.breaks[0]).toMatchObject({ from: '1399-12', first_revised: '1401-12' })
    expect(MONTHLY.breaks[0].old_method_after).toHaveLength(24)
    expect(methodSwitches(MONTHLY).map((s) => s.from)).toEqual(['1401-12'])
    expect(methodChanges(MONTHLY).map((s) => s.from)).toEqual(['1401-12'])
    expect(methodChanges({ ...MONTHLY, breaks: [] }).map((s) => s.from)).toEqual(['1401-12'])
    // Every switch of a layout with more of them.
    expect(methodChanges(RESTATED_1400).map((s) => s.from)).toEqual(['1400-12', '1401-01', '1401-12'])
    // Liquidity has no method.
    expect(methodSwitches(MONTHLY, CODE_M2)).toEqual([])
    // Only when no month records a method are the listed breaks all there is.
    const unrecorded = withMethods(
      MONTHLY,
      Object.fromEntries(MONTHLY.series[1].points.map((p) => [p.period, null]))
    )
    expect(methodSwitches(unrecorded)).toEqual([])
    expect(methodChanges(unrecorded).map((s) => s.from)).toEqual(['1399-12'])
    // A month with no recorded method is not a switch.
    expect(methodSwitches(withMethods(MONTHLY, { '1401-12': null })).map((s) => s.from)).toEqual(['1402-01'])
  })

  it('names one month for where the monthly record starts', () => {
    // The API's monthly_from, the same month its null reasons name.
    expect(MONTHLY.series.map((s) => s.monthly_from)).toEqual(['1398-04', '1398-04', '1398-04', '1398-04'])
    expect(seriesMonthlyFrom(MONTHLY)).toBe('1398-04')
    expect(MONTHLY.series[0].points.find((p) => p.period === '1398-05')!.null_reason!.yoy_pct).toBe(
      '1397-05 is not stored, so there is no year-on-year comparison (before 1398-04 only quarter-end months are stored)'
    )
    // Read from the points, the run starts at the same month: tsdview's
    // quarter-end 1398-03 sits right before it but is not monthly.
    expect(monthlyRunStart(MONTHLY.series[0].points)).toBe('1398-04')
    expect(seriesMonthlyFrom({ ...MONTHLY, series: MONTHLY.series.map((s) => ({ ...s, monthly_from: undefined })) })).toBe(
      '1398-04'
    )
    expect(monthlyRunStart([...MONTHLY.series[0].points].reverse())).toBe('1398-04')
    expect(monthlyRunStart([])).toBeNull()
    // The dollar response names it too. Read from its history alone, the
    // quarter-end 1398-03 touches 1398-04 and would give another month — the
    // API's month is the one stated.
    expect(dollarMonthlyFrom(D12)).toBe('1398-04')
    expect(historyMoneyRunStart(D12.history)).toBe('1398-03')
    expect(dollarMonthlyFrom({ ...D12, money_as_of: { ...D12.money_as_of!, monthly_from: undefined } })).toBe('1398-03')
    expect(historyMoneyRunStart(D12.history.map((h) => ({ ...h, m2: null })))).toBeNull()
    // Year-on-year runs unbroken a year later.
    expect(yoyRunStart('1398-04')).toBe('1399-04')
    expect(yoyRunStart(null)).toBeNull()
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
    expect(ANNUAL.freq).toBe('annual')
    const end = annualGrowthRows(ANNUAL, 'end', 1397)
    expect(end.find((r) => r.year === 1404)).toEqual({ year: 1404, m2: 53.268706, base: 61.533143 })
    expect(end.find((r) => r.year === 1401)!.base).toBeNull()
    expect(end[0].year).toBe(1397)
    const avg = annualGrowthRows(ANNUAL, 'avg')
    expect(avg.find((r) => r.year === 1404)!.m2).toBe(39.740178)
    expect(avg.find((r) => r.year === 1396)!.m2).toBeNull()
  })

  it('reads which years compare two methods from the points', () => {
    const spans = (resp: LiquidityAggregatesResponse, field: 'end_growth_pct' | 'avg_growth_pct' | 'avg_value') =>
      resp.annual!.filter((a) => annualMethodSpan(resp, a, CODE_BASE, field) !== null).map((a) => a.year)
    // Esfand 1401 (revised) against Esfand 1400 (old).
    expect(spans(ANNUAL, 'end_growth_pct')).toEqual([1401])
    // A yearly average reads every month: only 1401 holds both methods.
    expect(spans(ANNUAL, 'avg_value')).toEqual([1401])
    expect(spans(ANNUAL, 'avg_growth_pct')).toEqual([1401, 1402])
    expect(annualMethodSpan(ANNUAL, year(1401), CODE_M2, 'end_growth_pct')).toBeNull()
    // With Esfand 1399 and 1400 restated, each Esfand from 1399 on is revised:
    // Esfand over Esfand compares one definition, whatever lies between, and
    // only 1399 (against old Esfand 1398) crosses the change.
    expect(spans(RESEARCH_LAYOUT, 'end_growth_pct')).toEqual([1399])
    expect(spans(RESEARCH_LAYOUT, 'avg_value')).toEqual([1399, 1400, 1401])
  })

  it('says why an annual figure is missing', () => {
    // The API's own reason, wherever it gives one.
    expect(annualFigure(ANNUAL, year(1401), CODE_BASE, 'end_growth_pct')).toEqual({
      value: null,
      reason:
        '1401-12 is on the revised base-money method and 1400-12 on the old one (CBI redefined base money from 1399-12); growth across the two methods is not computed'
    })
    expect(annualFigure(ANNUAL, year(1402), CODE_BASE, 'avg_growth_pct').reason).toBe(
      'the months of 1401 and 1402 are not all on one recorded base-money method; growth across methods is not computed'
    )
    // Where it gives none, the page's, naming the API's monthly start.
    expect(annualFigure(ANNUAL, year(1398), CODE_M2, 'avg_growth_pct').reason).toBe(
      'a yearly average needs all twelve monthly stocks of 1398 and of 1397; before Tir 1398 only some months are stored (CBI’s quarter-ends)'
    )
    expect(annualFigure(ANNUAL, year(1404), CODE_M2, 'end_growth_pct')).toEqual({ value: 53.268706, reason: null })
    // Without the API's reasons, the page's reading of the points.
    const bare = { ...year(1401), null_reason: undefined }
    expect(annualFigure(ANNUAL, bare, CODE_BASE, 'end_growth_pct').reason).toMatch(/changed its base-money method/)
  })

  it('takes no yearly average across a method change, even when a response carries one', () => {
    const served = { ...year(1401), avg_value: { ...year(1401).avg_value!, [CODE_BASE]: 7000 }, null_reason: undefined }
    expect(annualFigure(ANNUAL, served, CODE_BASE, 'avg_value')).toEqual({
      value: null,
      reason: 'CBI changed its base-money method during 1401: a mean across the change would mix two definitions'
    })
    expect(annualFigure(ANNUAL, served, CODE_M2, 'avg_value').value).toBe(55822.275)
    // …and it sorts with the missing, as it is shown.
    expect(annualSortValue(ANNUAL, served, 'base_avg')).toBeNull()
    // Nor a growth across the change.
    const growth = { ...year(1401), end_growth_pct: { ...year(1401).end_growth_pct, [CODE_BASE]: 41.5 } }
    expect(annualFigure(ANNUAL, growth, CODE_BASE, 'end_growth_pct').value).toBeNull()
    expect(annualFigure(ANNUAL, year(1396), CODE_M2, 'avg_value').reason).toBe(
      "4 of 12 months are stored; an average of fewer is not the year's average"
    )
    // A year on one method has its average.
    expect(annualFigure(ANNUAL, year(1402), CODE_BASE, 'avg_value')).toEqual({ value: 9729.25, reason: null })
  })

  it('says a mean or growth over a month with no recorded method is not taken', () => {
    const unrecorded = withMethods(ANNUAL, { '1403-05': null })
    const y = unrecorded.annual!.find((a) => a.year === 1403)!
    expect(annualFigure(unrecorded, y, CODE_BASE, 'avg_value')).toEqual({
      value: null,
      reason: 'the months of 1403 are not all on one recorded base-money method, so no mean is taken across them'
    })
  })

  it('names a year that holds only quarter-ends', () => {
    // 1395..1397: tsdview's quarter-ends and the oldest workbook's Esfand 1397.
    for (const y of [1395, 1396, 1397]) expect(quarterEndsOnly(ANNUAL, y)).toBe(true)
    // 1398: ten months, not only quarter-ends; 1404: complete.
    expect(year(1398).months_present).toBe(10)
    expect(quarterEndsOnly(ANNUAL, 1398)).toBe(false)
    expect(quarterEndsOnly(ANNUAL, 1404)).toBe(false)
  })

  it('places each Esfand stock on the month axis, split by method', () => {
    const rows = annualLevelRows(ANNUAL, 'IRT', 1398)
    expect(rows.every((r) => r.period.endsWith('-12'))).toBe(true)
    expect(rowAt(rows, '1404-12').m2).toBeCloseTo(15581.22, 6)
    expect(rowAt(rows, '1398-12').base_old).toBeCloseTo(352.85, 6)
    expect(rowAt(rows, '1400-12').base_old).toBeCloseTo(603.97, 6)
    expect(rowAt(rows, '1401-12').base_revised).toBeCloseTo(854.8, 6)
  })
})

describe('which view the money section can show', () => {
  it('reads an answer only as the view it answers', () => {
    expect(moneyView('monthly', MONTHLY, false)).toBe('ready')
    expect(moneyView('annual', ANNUAL, false)).toBe('ready')
    // The render right after the toggle: annual asked, the monthly answer in
    // hand, the request not yet marked loading. Not a missing annual block.
    expect(MONTHLY.freq).toBe('monthly')
    expect(moneyView('annual', MONTHLY, false)).toBe('loading')
    expect(moneyView('annual', ANNUAL, true)).toBe('loading')
    expect(moneyView('annual', null, false)).toBe('loading')
    // An annual answer without its yearly block is the server's fault.
    const { annual: _dropped, ...noYears } = ANNUAL
    expect(moneyView('annual', noYears, false)).toBe('missing')
  })
})

describe('the data table', () => {
  it('pairs the two series and the multiplier by month', () => {
    const rows = moneyTableRows(MONTHLY)
    const esfand = rows.find((r) => r.period === '1404-12')!
    expect(esfand.m2!.value).toBe(155812.2)
    expect(esfand.base!.value).toBe(21959.3)
    expect(esfand.multiplier).toBe(7.095499)
    expect(moneySortValue(esfand, 'm2_ytd')).toBe(53.268706)
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
    expect(P0).toBe(264630)
    const rows = dollarRows(D12, { bandKey: 'last_12m' })
    const anchor = rowAt(rows, '1405-07')
    // The measured series ends at the anchor month, whose latest close the anchor is…
    expect(anchor.actual).toBe(264630)
    expect(rows.filter((r) => r.actual !== null).every((r) => r.x <= anchor.x)).toBe(true)
    // …and each path starts there, by repeating the same close: nothing invented.
    for (const s of D12.scenarios) {
      expect(anchor[scenarioKey(s.key)]).toBe(P0)
      expect(rows.filter((r) => r[scenarioKey(s.key)] !== null).every((r) => r.x >= anchor.x)).toBe(true)
    }
    expect(anchor.trend).toBe(P0)
    // No future month carries a measured value.
    expect(rowAt(rows, '1406-07').actual).toBeNull()
    expect(rowAt(rows, '1406-07')[scenarioKey('last_12m')]).toBe(405594.98)
    expect(rowAt(rows, '1406-07').trend).toBe(369408.49)
  })

  it('draws the band the API computed, from zero width at the anchor', () => {
    const rows = dollarRows(D12, { bandKey: 'last_12m' })
    expect(rowAt(rows, '1405-07').band).toEqual([P0, P0])
    // The API's own edges at 12 months for the 53.3% path: 311k–860k.
    const p12 = D12.scenarios[0].path.find((p) => p.h === 12)!
    expect(rowAt(rows, '1406-07').band).toEqual([p12.lo, p12.hi])
    expect([Math.round(p12.lo!), Math.round(p12.hi!)]).toEqual([311301, 860294])
    // Without them, the path × exp(the band's own quantile).
    const step = D12.band!.by_h.find((s) => s.h === 12)!
    expect(pathBand({ h: 12, value: p12.value }, step)).toEqual(bandEdges(p12.value, step))
    expect(pathBand({ h: 12, value: 1 }, undefined)).toBeNull()
    expect(bandEdges(100, { h: 3, lo: -0.1996, hi: 0.2809 })[0]).toBeCloseTo(100 * Math.exp(-0.1996), 9)
    // Only the chosen path carries it.
    expect(rows.every((r) => r.band === null || r.x >= rowAt(rows, '1405-07').x)).toBe(true)
  })

  it('leaves the band out at a horizon it does not cover, rather than drawing between neighbours', () => {
    // The API leaves no gap; this one is made by hand, and the edges the
    // path carries are dropped so only the band's steps can supply them.
    const gappy: LiquidityDollarResponse = {
      ...D12,
      scenarios: D12.scenarios.map((s) => ({ ...s, path: s.path.map((p) => ({ ...p, lo: null, hi: null })) })),
      band: { ...D12.band!, by_h: D12.band!.by_h.filter((s) => s.h !== 5) }
    }
    const rows = dollarRows(gappy, { bandKey: 'three_year' })
    const p5 = D12.scenarios[1].path.find((p) => p.h === 5)!.period
    expect(rowAt(rows, p5).band).toBeNull()
    const p6 = D12.scenarios[1].path.find((p) => p.h === 6)!.period
    expect(rowAt(rows, p6).band).not.toBeNull()
  })

  it('wraps the scenario the API names, or the reader’s pick when it wraps each', () => {
    const keys = D12.scenarios.map((s) => s.key)
    expect(keys).toEqual(['last_12m', 'three_year', 'slowest_three_year'])
    expect(D12.band!.applied_to).toBe('each')
    expect(bandTarget(D12.band, keys, null)).toBe('last_12m')
    expect(bandTarget(D12.band, keys, 'slowest_three_year')).toBe('slowest_three_year')
    const named = { ...D12.band!, applied_to: 'three_year' }
    expect(bandTarget(named, keys, 'slowest_three_year')).toBe('three_year')
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
      'about 80% of the time at 1–12 months',
      'about 86% of the time at 13–24 months',
      'about 90% of the time at 24 months'
    ])
    const s = coverageSentence(D12.band!)
    expect(s).toBe(
      'In the backtest from Farvardin 1394, a band built this way held the actual price about 80% of the time at 1–12 months, about 86% of the time at 13–24 months and about 90% of the time at 24 months.'
    )
    expect(s).not.toMatch(/95|confiden|probabil/)
    expect(coverageSentence({ ...D12.band!, coverage: {} })).toMatch(/carries no probability/)
  })

  it('knows when the band held the price less often than its quantiles would', () => {
    // 5%–95% quantiles would hold 90% of outcomes; it held 80%, 86% and 89.9%.
    expect(bandUndercovers(D12.band!)).toBe(true)
    expect(bandUndercovers({ ...D12.band!, coverage: { '1-12': 0.92, '24': 0.7 } })).toBe(false)
    expect(bandUndercovers({ ...D12.band!, coverage: {} })).toBe(false)
  })

  it('states the long-horizon coverage with what it rests on, and no comparison', () => {
    expect(D12.band!.coverage_n).toEqual({ '1-12': 1368, '13-24': 1080, '24': 79 })
    expect(D12.band!.coverage_independent).toEqual({ '24': 3 })
    const note = longHorizonNote(D12.band)
    expect(note).toBe(
      'in the backtest the band held the actual price about 86% of the time at 13–24 months and about 90% of the time at 24 months (79 overlapping origins, about 3 independent outcomes). So few independent outcomes cannot say whether it holds more or less often that far out than within a year.'
    )
    // Whichever way the numbers fall, no "more often" or "less often" than within a year.
    for (const v of [0.5, 0.99]) {
      const n = longHorizonNote({ ...D12.band!, coverage: { ...D12.band!.coverage, '24': v } })!
      expect(n).not.toMatch(/(more|less) often than within/)
      expect(n).toContain(`about ${Math.round(v * 100)}% of the time at 24 months`)
    }
    // Without the sample sizes, the rates alone.
    expect(longHorizonNote({ ...D12.band!, coverage_independent: undefined })).toBe(
      'in the backtest the band held the actual price about 86% of the time at 13–24 months and about 90% of the time at 24 months.'
    )
    expect(longHorizonNote({ ...D12.band!, coverage: { '1-12': 0.8 } })).toMatch(/not measured/)
    expect(longHorizonNote(null)).toBeNull()
  })
})

describe('the dollar per unit of liquidity', () => {
  it('finds its base and its range', () => {
    expect(indexBasePeriod(D12.history)).toBe('1390-12')
    expect(D12.history.find((h) => h.period === '1390-12')!.usd_per_m2_index).toBe(100)
    const rows = indexRows(D12.history)
    expect(rows.every((r) => r.index !== null)).toBe(true)
    // 1405-07 has no liquidity figure: no index, not a zero.
    expect(rows.find((r) => r.period === '1405-07')).toBeUndefined()
    expect(rows[rows.length - 1]).toMatchObject({ period: '1404-12', index: 194.7834 })
    const ext = indexExtremes(rows)!
    expect(ext.min).toMatchObject({ period: '1396-06', index: 52.2307 })
    expect(ext.max).toMatchObject({ period: '1404-11', index: 199.2338 })
    expect(indexExtremes([])).toBeNull()
  })
})

describe('the backtest', () => {
  const bt = D12.backtest!

  it('reads either nesting of the p-value maps', () => {
    // The API's maps are flat, horizon → p, for the liquidity-paced path.
    expect(normalizeDm(bt.dm_vs_trend_p)).toEqual({ m2_paced: bt.dm_vs_trend_p })
    expect(normalizeDm({ m2_paced: { '3': 0.2, '12': 0.3 } })).toEqual({ m2_paced: { '3': 0.2, '12': 0.3 } })
    expect(normalizeDm(null)).toEqual({})
  })

  it('states what the numbers show, a failure to reject as exactly that', () => {
    expect(backtestStatements(bt)).toEqual([
      'Against assuming no change: measurably smaller misses at every horizon tested, 3, 6, 12 and 24 months (p ≤ 0.002).',
      'Against the dollar’s own past trend: no measurable difference at any horizon tested, 3, 6, 12 and 24 months (p 0.19–0.30).',
      'In this test, pacing the dollar with liquidity growth was not measurably better than the dollar’s own trend: its typical miss was 0.8–6.0% smaller, a difference the test could not tell from chance.'
    ])
    expect(rmseAgainstTrend(bt, ['3', '6', '12', '24'])).toBe('0.8–6.0% smaller')
    expect(rmseAgainstTrend({ ...bt, rmse: { ...bt.rmse, m2_paced: { '3': 0.17, '24': 0.4183 } } }, ['3', '24'])).toBe(
      'between 6.0% smaller and 5.4% larger'
    )
    expect(rmseAgainstTrend({ ...bt, rmse: {} }, ['3'])).toBeNull()
    for (const h of ['3', '6', '12', '24']) expect(bestModel(bt, h)).toBe('m2_paced')
  })

  it('emphasises a model only where it was measurably the smallest', () => {
    // The liquidity-paced RMSE is the smallest at every horizon, but not
    // measurably smaller than the trend's: nothing is emphasised.
    for (const h of ['3', '6', '12', '24']) expect(measurablyBest(bt, h)).toBeNull()
    const sharper = { ...bt, dm_vs_trend_p: { '3': 0.01, '6': 0.3, '12': 0.03, '24': 0.04 } }
    expect(['3', '6', '12', '24'].map((h) => measurablyBest(sharper, h))).toEqual(['m2_paced', null, 'm2_paced', 'm2_paced'])
    // The trend smallest: measurably so only if it beat both, the liquidity
    // path by the reverse of its one-sided test.
    const trendBest = { ...bt, rmse: { ...bt.rmse, m2_paced: { ...bt.rmse.m2_paced, '24': 0.5 } } }
    expect(measurablyBest(trendBest, '24')).toBeNull()
    expect(measurablyBest({ ...trendBest, dm_vs_trend_p: { ...bt.dm_vs_trend_p, '24': 0.99 } }, '24')).toBe('dollar_trend')
  })

  it('would say something else if the numbers did', () => {
    const better = { ...bt, dm_vs_trend_p: { ...(bt.dm_vs_trend_p as Record<string, number>), '3': 0.01 } }
    const s = backtestStatements(better)
    expect(s[1]).toBe(
      'Against the dollar’s own past trend: measurably smaller misses at 3 months (p ≤ 0.01), no measurable difference at 6, 12 and 24 months (p 0.23–0.30).'
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
