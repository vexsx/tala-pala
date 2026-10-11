import { formatGrouped, tehranParts, type DisplayUnit } from './format'
import type {
  LiquidityAggregatesResponse,
  LiquidityAnnualRow,
  LiquidityBacktest,
  LiquidityBand,
  LiquidityBandStep,
  LiquidityBreak,
  LiquidityDataAge,
  LiquidityDmPValues,
  LiquidityDollarHistoryPoint,
  LiquidityDollarResponse,
  LiquidityLatest,
  LiquidityPoint,
  LiquiditySeries
} from '../api/types'

/**
 * Pure helpers for the money & liquidity page: the Jalali month labels, the
 * unit a CBI figure is shown in, and the rows the charts draw.
 *
 * Nothing here measures money growth, a scenario or a backtest — those arrive
 * computed from the API. What happens here is display: dividing a stock by ten
 * to show it in همت, placing a month on an axis, multiplying a path by the
 * band's own log quantiles to draw its edges, and keeping apart, key by key,
 * what must never be joined into one line — a measured price and a scenario,
 * and base money on CBI's old method and on its revised one.
 */

export const CODE_M2 = 'CBI_LIQUIDITY'
export const CODE_BASE = 'CBI_BASE_MONEY'

// --- Jalali periods -----------------------------------------------------------------

/** The twelve Jalali months, in the transliteration Iranian English reporting uses. */
export const JALALI_MONTHS: ReadonlyArray<{ en: string; fa: string }> = [
  { en: 'Farvardin', fa: 'فروردین' },
  { en: 'Ordibehesht', fa: 'اردیبهشت' },
  { en: 'Khordad', fa: 'خرداد' },
  { en: 'Tir', fa: 'تیر' },
  { en: 'Mordad', fa: 'مرداد' },
  { en: 'Shahrivar', fa: 'شهریور' },
  { en: 'Mehr', fa: 'مهر' },
  { en: 'Aban', fa: 'آبان' },
  { en: 'Azar', fa: 'آذر' },
  { en: 'Dey', fa: 'دی' },
  { en: 'Bahman', fa: 'بهمن' },
  { en: 'Esfand', fa: 'اسفند' }
]

/** 'YYYY-MM' → its year and month; null for anything else. */
export function parsePeriod(period: string): { jy: number; jm: number } | null {
  const m = /^(\d{4})-(\d{2})$/.exec(period)
  if (!m) return null
  const jm = Number(m[2])
  if (jm < 1 || jm > 12) return null
  return { jy: Number(m[1]), jm }
}

/**
 * A Jalali month as one integer (months since 0 Farvardin, year 0), so a chart
 * can place the quarter-end history and the monthly workbooks on one axis with
 * the gaps between quarters as wide as they really are.
 */
export function periodOrdinal(period: string): number | null {
  const p = parsePeriod(period)
  return p ? p.jy * 12 + p.jm - 1 : null
}

export function ordinalPeriod(ordinal: number): string {
  const jy = Math.floor(ordinal / 12)
  const jm = ordinal - jy * 12 + 1
  return `${jy}-${jm < 10 ? '0' : ''}${jm}`
}

/** '1404-12' → 'Esfand 1404'. A label it cannot read is returned as it came. */
export function periodLabel(period: string): string {
  const p = parsePeriod(period)
  return p ? `${JALALI_MONTHS[p.jm - 1].en} ${p.jy}` : period
}

const FA_DIGITS = '۰۱۲۳۴۵۶۷۸۹'

/** '1404-12' → 'اسفند ۱۴۰۴'. */
export function periodLabelFa(period: string): string {
  const p = parsePeriod(period)
  if (!p) return period
  const year = String(p.jy).replace(/\d/g, (d) => FA_DIGITS[Number(d)])
  return `${JALALI_MONTHS[p.jm - 1].fa} ${year}`
}

/**
 * Where to put the ticks on a month axis: every Farvardin (thinned to whole
 * steps of 1, 2, 5 or 10 years) on a long span, every quarter or half-year on
 * a short one, so a tick always sits on a real month boundary.
 */
export function periodTicks(min: number, max: number, maxTicks = 7): number[] {
  if (!(max > min)) return [min]
  const span = max - min
  if (span < 30) {
    const step = span <= 15 ? 3 : 6
    const ticks: number[] = []
    for (let t = Math.ceil(min / step) * step; t <= max; t += step) ticks.push(t)
    return ticks
  }
  const years = span / 12
  const step = [1, 2, 5, 10, 20].find((s) => years / s <= maxTicks) ?? 50
  const ticks: number[] = []
  const firstYear = Math.ceil(min / 12)
  for (let y = Math.ceil(firstYear / step) * step; y * 12 <= max; y += step) ticks.push(y * 12)
  return ticks
}

/**
 * Ticks for a chart of one point a year: the points themselves, thinned to at
 * most `maxTicks` and counted back from the newest so it is always labelled.
 */
export function esfandTicks(xs: number[], maxTicks = 8): number[] {
  const step = Math.max(1, Math.ceil(xs.length / maxTicks))
  return xs.filter((_, i) => (xs.length - 1 - i) % step === 0)
}

/** A tick's text: the Jalali year on a long span, the month on a short one. */
export function periodTickLabel(ordinal: number, span: number): string {
  return span < 30 ? ordinalPeriod(ordinal) : String(Math.floor(ordinal / 12))
}

/** Whole calendar days between two instants, each read as its Tehran date. */
export function tehranDaysBetween(from: string, to: string): number | null {
  const a = new Date(from)
  const b = new Date(to)
  if (Number.isNaN(a.getTime()) || Number.isNaN(b.getTime())) return null
  const pa = tehranParts(a)
  const pb = tehranParts(b)
  return Math.round(
    (Date.UTC(pb.year, pb.month - 1, pb.day) - Date.UTC(pa.year, pa.month - 1, pa.day)) / 86_400_000
  )
}

/** The Jalali month a 'YYYY/MM/DD' date falls in, as a period. */
export function jalaliDatePeriod(date: string): string | null {
  const m = /^(\d{4})\/(\d{2})\/\d{2}$/.exec(date)
  return m ? `${m[1]}-${m[2]}` : null
}

// --- units ---------------------------------------------------------------------------

/**
 * How a CBI stock is shown. CBI publishes هزار میلیارد ریال (10¹² rial) and
 * the API serves exactly that; with the app on toman the page divides by ten
 * and shows همت (هزار میلیارد تومان, 10¹² toman), the unit Iranian reporting
 * uses, and with the app on rial it shows the figure as published. Either
 * way the unit is printed beside every number — never one silently.
 */
export interface MoneyUnit {
  divisor: number
  /** Printed after a figure. */
  label: string
  /** The same, spelled out for a heading. */
  long: string
}

export function moneyUnit(unit: DisplayUnit): MoneyUnit {
  return unit === 'IRR'
    ? { divisor: 1, label: 'thousand billion rial', long: 'thousand billion rial (هزار میلیارد ریال), as CBI publishes it' }
    : { divisor: 10, label: 'همت', long: 'همت — trillion toman (CBI’s thousand billion rial ÷ 10)' }
}

export function toDisplayMoney(valueThousandBillionRial: number, unit: DisplayUnit): number {
  return valueThousandBillionRial / moneyUnit(unit).divisor
}

/** The figure alone, at a precision that suits its size (1350s stocks are fractions of one همت). */
export function formatMoneyNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const abs = Math.abs(value)
  return formatGrouped(value, abs >= 100 ? 0 : abs >= 1 ? 1 : 3)
}

/** A stock in the reader's unit: '15,581 همت' or '155,812 thousand billion rial'. */
export function formatMoney(valueThousandBillionRial: number | null | undefined, unit: DisplayUnit): string {
  if (valueThousandBillionRial === null || valueThousandBillionRial === undefined) return '—'
  return `${formatMoneyNumber(toDisplayMoney(valueThousandBillionRial, unit))} ${moneyUnit(unit).label}`
}

// --- the money series ------------------------------------------------------------------

export function findSeries(resp: LiquidityAggregatesResponse, code: string): LiquiditySeries | undefined {
  return resp.series.find((s) => s.code === code)
}

/** True once at least one liquidity or base-money observation is stored. */
export function hasMoneyData(resp: LiquidityAggregatesResponse): boolean {
  if (!resp.latest) return false
  return [CODE_M2, CODE_BASE].some((c) => (findSeries(resp, c)?.points.length ?? 0) > 0)
}

/**
 * CBI's monthly workbooks have arrived 10 to 137 days after the month they
 * describe, in batches (report_cbi.md, measured over every edition from Tir
 * 1399). A newest month older than the longest of those is late by CBI's own
 * record — either CBI has published nothing newer or it has not been read.
 */
export const MONEY_STALE_AFTER_DAYS = 137

/** The server's verdict when it gives one; otherwise the bound above. */
export function moneyIsStale(latest: LiquidityLatest | null, age: LiquidityDataAge | undefined): boolean {
  if (age && typeof age.stale === 'boolean') return age.stale
  return latest !== null && latest.age_days > MONEY_STALE_AFTER_DAYS
}

/**
 * A row of a month-axis chart: the period, its x position, and one value per
 * line. A key holds one line; a key absent or null at a row is a gap the line
 * does not cross.
 */
export interface SeriesRow {
  x: number
  period: string
  [line: string]: number | string | null | [number, number]
}

/**
 * The line a base-money figure belongs to: one per CBI method, so no stroke
 * ever joins a figure on the old method to one on the revised method — the
 * two are not the same quantity, and a line between them would draw a growth
 * CBI's own footnotes say not to read.
 */
export function methodKey(prefix: string, method: LiquidityPoint['method'] | undefined): string {
  return `${prefix}_${method ?? 'unmarked'}`
}

/** Every key the rows use, in first-seen order — the lines a chart must draw. */
export function lineKeys(rows: SeriesRow[], prefix: string): string[] {
  const keys: string[] = []
  for (const r of rows) {
    for (const k of Object.keys(r)) {
      if (k.startsWith(`${prefix}_`) && !keys.includes(k) && r[k] !== null) keys.push(k)
    }
  }
  return keys
}

/**
 * A value with no neighbour on its own line (base money's single revised
 * month, 1399-12, between old-method months) — a line needs two points, so
 * the chart draws it as a dot instead of losing it.
 */
export function isIsolated(rows: SeriesRow[], index: number, key: string): boolean {
  const v = rows[index]?.[key]
  if (v === null || v === undefined) return false
  const prev = rows[index - 1]?.[key]
  const next = rows[index + 1]?.[key]
  return (prev === null || prev === undefined) && (next === null || next === undefined)
}

/**
 * Liquidity and base money on one month axis, one value each per period,
 * picked by `pick` (a level in the reader's unit, or a growth the API
 * measured). Base money is split by method; nothing is computed or filled.
 */
export function moneyRows(
  resp: LiquidityAggregatesResponse,
  pick: (p: LiquidityPoint) => number | null,
  opts: { fromX?: number } = {}
): SeriesRow[] {
  const byX = new Map<number, SeriesRow>()
  const row = (p: LiquidityPoint): SeriesRow | null => {
    const x = periodOrdinal(p.period)
    if (x === null || (opts.fromX !== undefined && x < opts.fromX)) return null
    let r = byX.get(x)
    if (!r) {
      r = { x, period: p.period }
      byX.set(x, r)
    }
    return r
  }
  for (const p of findSeries(resp, CODE_M2)?.points ?? []) {
    const r = row(p)
    if (r) r.m2 = pick(p)
  }
  for (const p of findSeries(resp, CODE_BASE)?.points ?? []) {
    const r = row(p)
    if (r) r[methodKey('base', p.method)] = pick(p)
  }
  return fillNulls([...byX.values()].sort((a, b) => a.x - b.x))
}

/**
 * The money multiplier (liquidity ÷ base money, as the API measured it), on
 * the method of the base money it divides by, split the same way.
 */
export function multiplierRows(
  resp: LiquidityAggregatesResponse,
  opts: { fromX?: number; esfandOnly?: boolean } = {}
): SeriesRow[] {
  const method = new Map((findSeries(resp, CODE_BASE)?.points ?? []).map((p) => [p.period, p.method]))
  const rows: SeriesRow[] = []
  for (const m of resp.multiplier) {
    const x = periodOrdinal(m.period)
    if (x === null || (opts.fromX !== undefined && x < opts.fromX)) continue
    if (opts.esfandOnly && x % 12 !== 11) continue
    rows.push({ x, period: m.period, [methodKey('mult', method.get(m.period))]: m.value })
  }
  return fillNulls(rows.sort((a, b) => a.x - b.x))
}

/** Every row carries every key, null where its line has no value: a gap, never a join. */
function fillNulls(rows: SeriesRow[]): SeriesRow[] {
  const keys = new Set<string>()
  for (const r of rows) for (const k of Object.keys(r)) keys.add(k)
  for (const r of rows) for (const k of keys) if (r[k] === undefined) r[k] = null
  return rows
}

/** The x a window of `years` whole years ending at the newest row starts from. */
export function windowStart(newestX: number | null, years: number | null): number | undefined {
  if (newestX === null || years === null) return undefined
  return newestX - years * 12 + 1
}

export function newestOrdinal(resp: LiquidityAggregatesResponse): number | null {
  let best: number | null = null
  for (const code of [CODE_M2, CODE_BASE]) {
    for (const p of findSeries(resp, code)?.points ?? []) {
      const x = periodOrdinal(p.period)
      if (x !== null && (best === null || x > best)) best = x
    }
  }
  return best
}

/** The breaks of one series, each with its x. */
export function breaksFor(breaks: LiquidityBreak[], code: string): Array<LiquidityBreak & { x: number }> {
  return breaks
    .filter((b) => b.code === code)
    .map((b) => ({ ...b, x: periodOrdinal(b.from) }))
    .filter((b): b is LiquidityBreak & { x: number } => b.x !== null)
}

/** True when a break of `code` falls after `from` and at or before `to`. */
export function breakBetween(breaks: LiquidityBreak[], code: string, from: string, to: string): boolean {
  const a = periodOrdinal(from)
  const b = periodOrdinal(to)
  if (a === null || b === null) return false
  return breaksFor(breaks, code).some((k) => k.x > a && k.x <= b)
}

// --- the annual view ---------------------------------------------------------------------

export type AnnualMeasure = 'end' | 'avg'

/** One bar group per Jalali year: Esfand over Esfand, or yearly average over yearly average. */
export function annualGrowthRows(
  annual: LiquidityAnnualRow[],
  measure: AnnualMeasure,
  fromYear?: number
): Array<{ year: number; m2: number | null; base: number | null }> {
  return annual
    .filter((a) => fromYear === undefined || a.year >= fromYear)
    .map((a) => {
      const g = measure === 'end' ? a.end_growth_pct : a.avg_growth_pct
      return { year: a.year, m2: g?.[CODE_M2] ?? null, base: g?.[CODE_BASE] ?? null }
    })
}

/**
 * The Esfand stocks of the annual block on the month axis (so the levels
 * chart reads the same in both views), base money split by the method of its
 * Esfand observation.
 */
export function annualLevelRows(resp: LiquidityAggregatesResponse, unit: DisplayUnit, fromYear?: number): SeriesRow[] {
  const method = new Map((findSeries(resp, CODE_BASE)?.points ?? []).map((p) => [p.period, p.method]))
  const rows: SeriesRow[] = []
  for (const a of resp.annual ?? []) {
    if (fromYear !== undefined && a.year < fromYear) continue
    const period = `${a.year}-12`
    const m2 = a.end_value[CODE_M2]
    const base = a.end_value[CODE_BASE]
    rows.push({
      x: a.year * 12 + 11,
      period,
      m2: m2 == null ? null : toDisplayMoney(m2, unit),
      [methodKey('base', method.get(period))]: base == null ? null : toDisplayMoney(base, unit)
    })
  }
  return fillNulls(rows)
}

/** True when an annual growth is missing because the months it compares straddle a method change. */
export function annualGrowthCrossesBreak(
  resp: LiquidityAggregatesResponse,
  row: LiquidityAnnualRow,
  code: string,
  measure: AnnualMeasure
): boolean {
  const g = measure === 'end' ? row.end_growth_pct : row.avg_growth_pct
  if (g && g[code] != null) return false
  const y = row.year
  // The yearly average reads every month of both years; Esfand reads two months.
  const from = measure === 'end' ? `${y - 1}-12` : `${y - 2}-12`
  return breakBetween(resp.breaks, code, from, `${y}-12`)
}

/** Why an annual growth is missing, in words — or null when it is not. */
export function annualGrowthReason(
  resp: LiquidityAggregatesResponse,
  row: LiquidityAnnualRow,
  code: string,
  measure: AnnualMeasure
): string | null {
  const g = measure === 'end' ? row.end_growth_pct : row.avg_growth_pct
  if (g && g[code] != null) return null
  const y = row.year
  if (annualGrowthCrossesBreak(resp, row, code, measure)) {
    return `CBI changed its base-money method inside the months this growth compares; it is not measured across the change`
  }
  if (measure === 'avg') {
    return `a yearly average needs all twelve monthly stocks of ${y} and of ${y - 1}; before Tir 1397 CBI's history is quarter-ends`
  }
  return `no Esfand ${y - 1} figure is stored`
}

/** Why a yearly average is missing, in words — or null when it is not. */
export function annualAverageReason(
  resp: LiquidityAggregatesResponse,
  row: LiquidityAnnualRow,
  code: string
): string | null {
  if (row.avg_value && row.avg_value[code] != null) return null
  const y = row.year
  if (breakBetween(resp.breaks, code, `${y - 1}-12`, `${y}-12`)) {
    return `CBI changed its base-money method during ${y}: a mean across the change would mix two definitions`
  }
  return `a yearly average needs all twelve monthly stocks of ${y}; before Tir 1397 CBI's history is quarter-ends`
}

// --- the data table --------------------------------------------------------------------------

export interface MoneyTableRow {
  period: string
  x: number
  m2: LiquidityPoint | null
  base: LiquidityPoint | null
  multiplier: number | null
}

export function moneyTableRows(resp: LiquidityAggregatesResponse, fromX?: number): MoneyTableRow[] {
  const byPeriod = new Map<string, MoneyTableRow>()
  const get = (period: string): MoneyTableRow | null => {
    const x = periodOrdinal(period)
    if (x === null || (fromX !== undefined && x < fromX)) return null
    let r = byPeriod.get(period)
    if (!r) {
      r = { period, x, m2: null, base: null, multiplier: null }
      byPeriod.set(period, r)
    }
    return r
  }
  for (const p of findSeries(resp, CODE_M2)?.points ?? []) {
    const r = get(p.period)
    if (r) r.m2 = p
  }
  for (const p of findSeries(resp, CODE_BASE)?.points ?? []) {
    const r = get(p.period)
    if (r) r.base = p
  }
  for (const m of resp.multiplier) {
    const r = byPeriod.get(m.period)
    if (r) r.multiplier = m.value
  }
  return [...byPeriod.values()].sort((a, b) => a.x - b.x)
}

export type MoneySortKey =
  | 'period'
  | 'm2'
  | 'm2_mom'
  | 'm2_yoy'
  | 'm2_ytd'
  | 'base'
  | 'base_mom'
  | 'base_yoy'
  | 'base_ytd'
  | 'multiplier'

export function moneySortValue(r: MoneyTableRow, key: MoneySortKey): number | null {
  switch (key) {
    case 'period':
      return r.x
    case 'multiplier':
      return r.multiplier
    default: {
      const [which, field] = key.split('_') as ['m2' | 'base', string | undefined]
      const p = r[which]
      if (!p) return null
      if (!field) return p.value
      return field === 'mom' ? p.mom_pct : field === 'yoy' ? p.yoy_pct : p.ytd_pct
    }
  }
}

export type AnnualSortKey =
  | 'year'
  | 'm2_end'
  | 'm2_end_growth'
  | 'm2_avg'
  | 'm2_avg_growth'
  | 'base_end'
  | 'base_end_growth'
  | 'base_avg'
  | 'base_avg_growth'

export function annualSortValue(r: LiquidityAnnualRow, key: AnnualSortKey): number | null {
  if (key === 'year') return r.year
  const code = key.startsWith('m2') ? CODE_M2 : CODE_BASE
  const field = key.replace(/^(m2|base)_/, '')
  const map =
    field === 'end'
      ? r.end_value
      : field === 'end_growth'
        ? r.end_growth_pct
        : field === 'avg'
          ? r.avg_value
          : r.avg_growth_pct
  return map?.[code] ?? null
}

/** Sort without letting a missing figure pose as the smallest or the largest. */
export function sortNullsLast<T>(items: T[], value: (item: T) => number | null, desc: boolean): T[] {
  return [...items].sort((a, b) => {
    const va = value(a)
    const vb = value(b)
    if (va === null && vb === null) return 0
    if (va === null) return 1
    if (vb === null) return -1
    return desc ? vb - va : va - vb
  })
}

// --- the dollar ------------------------------------------------------------------------------

/** Scenario lines in the app's validated categorical order, each with its own dash. */
export const SCENARIO_STYLES: ReadonlyArray<{ color: string; dash: string }> = [
  { color: 'var(--series-1)', dash: '8 4' },
  { color: 'var(--series-2)', dash: '3 3' },
  { color: 'var(--series-3)', dash: '10 3 2 3' }
]

export function scenarioStyle(index: number): { color: string; dash: string } {
  return SCENARIO_STYLES[index] ?? { color: 'var(--muted)', dash: '2 2' }
}

export const scenarioKey = (key: string): string => `scn_${key}`

/** The band's edges around one path value, from its own log quantiles. */
export function bandEdges(value: number, step: LiquidityBandStep): [number, number] {
  return [value * Math.exp(step.lo), value * Math.exp(step.hi)]
}

/** Which scenario the band wraps: the one the API names, or the reader's pick when it applies to each. */
export function bandTarget(band: LiquidityBand | null, scenarioKeys: string[], picked: string | null): string | null {
  if (!band || scenarioKeys.length === 0) return null
  if (band.applied_to !== 'each') return scenarioKeys.includes(band.applied_to) ? band.applied_to : null
  return picked && scenarioKeys.includes(picked) ? picked : scenarioKeys[0]
}

/**
 * The dollar chart's rows: the measured month-end close, each scenario path,
 * the dollar's own trend and the band, each under its own key.
 *
 * The measured series and the paths never share a key. Each path is joined to
 * the anchor — the last stored close, which is where every path starts by
 * construction — by repeating that close at the anchor's month: the same
 * value at the same month, so nothing is invented and the dashed lines start
 * where the solid one ends. The band starts there too, with zero width: there
 * is no uncertainty about a price already seen. A horizon the band does not
 * cover gets no band, rather than one drawn between its neighbours.
 */
export function dollarRows(
  d: LiquidityDollarResponse,
  opts: { fromX?: number; bandKey: string | null }
): SeriesRow[] {
  const byX = new Map<number, SeriesRow>()
  const at = (period: string): SeriesRow | null => {
    const x = periodOrdinal(period)
    if (x === null || (opts.fromX !== undefined && x < opts.fromX)) return null
    let r = byX.get(x)
    if (!r) {
      r = { x, period }
      byX.set(x, r)
    }
    return r
  }
  for (const h of d.history) {
    const r = at(h.period)
    if (r) r.actual = h.usd_close
  }
  const anchorPeriod = d.anchor ? jalaliDatePeriod(d.anchor.jalali) : null
  const anchorRow = anchorPeriod ? at(anchorPeriod) : null
  const p0 = d.anchor?.close_toman ?? null
  const steps = new Map((d.band?.by_h ?? []).map((s) => [s.h, s]))
  for (const s of d.scenarios) {
    if (s.path.length === 0) continue
    const key = scenarioKey(s.key)
    if (anchorRow && p0 !== null) anchorRow[key] = p0
    for (const p of s.path) {
      const r = at(p.period)
      if (!r) continue
      r[key] = p.value
      if (s.key === opts.bandKey) {
        const step = steps.get(p.h)
        r.band = step ? bandEdges(p.value, step) : null
      }
    }
    if (s.key === opts.bandKey && anchorRow && p0 !== null) anchorRow.band = [p0, p0]
  }
  if (d.trend && d.trend.path.length > 0) {
    if (anchorRow && p0 !== null) anchorRow.trend = p0
    for (const p of d.trend.path) {
      const r = at(p.period)
      if (r) r.trend = p.value
    }
  }
  return fillNulls([...byX.values()].sort((a, b) => a.x - b.x))
}

/** '1-12' → months 1 to 12; '24' → month 24. */
export function parseCoverageKey(key: string): { from: number; to: number } | null {
  const m = /^(\d+)(?:-(\d+))?$/.exec(key.trim())
  if (!m) return null
  const from = Number(m[1])
  const to = m[2] === undefined ? from : Number(m[2])
  return to >= from ? { from, to } : null
}

function rangeText(r: { from: number; to: number }): string {
  return r.from === r.to ? `${r.from} months` : `${r.from}–${r.to} months`
}

/** The measured coverage clauses, shortest horizons first: 'about 84% of the time at 1–12 months'. */
export function coverageClauses(coverage: Record<string, number>): string[] {
  return Object.entries(coverage)
    .map(([k, v]) => ({ r: parseCoverageKey(k), v }))
    .filter((e): e is { r: { from: number; to: number }; v: number } => e.r !== null && Number.isFinite(e.v))
    .sort((a, b) => a.r.from - b.r.from)
    .map((e) => `about ${Math.round(e.v * 100)}% of the time at ${rangeText(e.r)}`)
}

/**
 * What the band is, said with the only probability it has earned: how often a
 * band built this way held the real price in the backtest. Never its nominal
 * quantiles — those promise more than it delivered.
 */
export function coverageSentence(band: LiquidityBand): string {
  const clauses = coverageClauses(band.coverage)
  const since = periodLabel(band.origins_from)
  if (clauses.length === 0) {
    return `No backtest coverage is reported for this band, so it carries no probability.`
  }
  const list = clauses.length === 1 ? clauses[0] : `${clauses.slice(0, -1).join(', ')} and ${clauses[clauses.length - 1]}`
  return `In the backtest from ${since}, a band built this way held the actual price ${list}.`
}

/**
 * True when every measured coverage falls short of the share of outcomes the
 * band's own quantiles would hold — the page then says the price left it more
 * often than its construction suggests, without printing that nominal share.
 */
export function bandUndercovers(band: LiquidityBand): boolean {
  const nominal = band.upper_q - band.lower_q
  const measured = Object.values(band.coverage).filter((v) => Number.isFinite(v))
  return measured.length > 0 && measured.every((v) => v < nominal)
}

/**
 * The coverage beyond twelve months, said beside the 24-month choice — and
 * whether it is lower than within twelve months, read from the numbers.
 */
export function longHorizonNote(band: LiquidityBand | null): string | null {
  if (!band) return null
  const entries = Object.entries(band.coverage)
    .map(([k, v]) => ({ r: parseCoverageKey(k), v }))
    .filter((e): e is { r: { from: number; to: number }; v: number } => e.r !== null && Number.isFinite(e.v))
    .sort((a, b) => a.r.from - b.r.from)
  const short = entries.filter((e) => e.r.to <= 12)
  const long = entries.filter((e) => e.r.to > 12)
  if (long.length === 0) return 'the band’s coverage beyond 12 months was not measured in the backtest.'
  const said = (list: typeof entries) =>
    list.map((e) => `about ${Math.round(e.v * 100)}% of the time at ${rangeText(e.r)}`).join(', ')
  let compare = ''
  if (short.length > 0) {
    const lo = Math.min(...short.map((e) => e.v))
    const hi = Math.max(...short.map((e) => e.v))
    const verb = long.every((e) => e.v < lo) ? 'less often' : long.every((e) => e.v > hi) ? 'more often' : 'about as often'
    compare = ` — ${verb} than within 12 months (${said(short)})`
  }
  return `in the backtest the band held the actual price ${said(long)}${compare}.`
}

/** The dollar ÷ liquidity index where it exists, on the month axis. */
export function indexRows(history: LiquidityDollarHistoryPoint[], fromX?: number): SeriesRow[] {
  const rows: SeriesRow[] = []
  for (const h of history) {
    const x = periodOrdinal(h.period)
    if (x === null || h.usd_per_m2_index === null || (fromX !== undefined && x < fromX)) continue
    rows.push({ x, period: h.period, index: h.usd_per_m2_index })
  }
  return rows
}

/** The month the index equals 100 at, when one does — its base. */
export function indexBasePeriod(history: LiquidityDollarHistoryPoint[]): string | null {
  const base = history.find((h) => h.usd_per_m2_index !== null && Math.abs(h.usd_per_m2_index - 100) < 1e-9)
  return base ? base.period : null
}

/** The lowest and highest index month in the rows. */
export function indexExtremes(rows: SeriesRow[]): { min: SeriesRow; max: SeriesRow } | null {
  if (rows.length === 0) return null
  let min = rows[0]
  let max = rows[0]
  for (const r of rows) {
    if ((r.index as number) < (min.index as number)) min = r
    if ((r.index as number) > (max.index as number)) max = r
  }
  return { min, max }
}

// --- the backtest ----------------------------------------------------------------------------

/** The models a backtest names, in the order they are read: the benchmarks first. */
export const MODEL_LABELS: Record<string, string> = {
  random_walk: 'No change (random walk)',
  dollar_trend: 'The dollar’s own past trend',
  m2_paced: 'Paced by liquidity growth'
}

export function modelLabel(key: string): string {
  return MODEL_LABELS[key] ?? key.replace(/_/g, ' ')
}

/**
 * The p-value maps as model → horizon → p. Design §3 leaves their nesting
 * open; a flat horizon → p map is read as the liquidity-paced model's, the
 * only model the page asks the tests about.
 */
export function normalizeDm(
  values: LiquidityDmPValues | null | undefined
): Record<string, Record<string, number | null>> {
  if (!values) return {}
  const entries = Object.entries(values)
  if (entries.every(([, v]) => v === null || typeof v === 'number')) {
    return { m2_paced: Object.fromEntries(entries) as Record<string, number | null> }
  }
  const out: Record<string, Record<string, number | null>> = {}
  for (const [model, v] of entries) if (v !== null && typeof v === 'object') out[model] = v
  return out
}

/** The horizons the RMSE table covers, shortest first. */
export function backtestHorizons(bt: LiquidityBacktest): string[] {
  const hs = new Set<string>()
  for (const byH of Object.values(bt.rmse)) for (const h of Object.keys(byH)) hs.add(h)
  return [...hs].filter((h) => Number.isFinite(Number(h))).sort((a, b) => Number(a) - Number(b))
}

/** The models in reading order: random walk, the trend, liquidity-paced, then anything else. */
export function backtestModels(bt: LiquidityBacktest): string[] {
  const known = Object.keys(MODEL_LABELS).filter((m) => m in bt.rmse)
  return [...known, ...Object.keys(bt.rmse).filter((m) => !(m in MODEL_LABELS))]
}

/** The model with the smallest error at a horizon; null on a tie or with nothing measured. */
export function bestModel(bt: LiquidityBacktest, h: string): string | null {
  let best: string | null = null
  let bestV = Infinity
  let tie = false
  for (const m of Object.keys(bt.rmse)) {
    const v = bt.rmse[m]?.[h]
    if (v === null || v === undefined) continue
    if (v < bestV) {
      best = m
      bestV = v
      tie = false
    } else if (v === bestV) {
      tie = true
    }
  }
  return tie ? null : best
}

export function formatP(p: number): string {
  if (p < 0.001) return '< 0.001'
  return p < 0.01 ? p.toFixed(3) : p.toFixed(2)
}

function monthsList(hs: string[]): string {
  const n = hs.map(Number)
  if (n.length <= 1) return `${n.join('')} months`
  return `${n.slice(0, -1).join(', ')} and ${n[n.length - 1]} months`
}

/** 'p ≤ 0.002' or 'p 0.16–0.30' over the horizons given. */
function pSpan(ps: number[], significant: boolean): string {
  const lo = Math.min(...ps)
  const hi = Math.max(...ps)
  if (significant) return hi < 0.001 ? 'p < 0.001' : `p ≤ ${formatP(hi)}`
  return lo === hi ? `p = ${formatP(lo)}` : `p ${formatP(lo)}–${formatP(hi)}`
}

/**
 * What the backtest's numbers show, in sentences built from those numbers —
 * so a new backtest that came out differently would read differently. A
 * one-sided Diebold–Mariano p below `alpha` is "measurably" better; anything
 * else is no measurable difference, whatever the RMSE's last digit says.
 */
export function backtestStatements(bt: LiquidityBacktest, alpha = 0.05): string[] {
  const hs = backtestHorizons(bt)
  const vsRw = normalizeDm(bt.dm_vs_random_walk_p).m2_paced ?? {}
  const vsTrend = normalizeDm(bt.dm_vs_trend_p).m2_paced ?? {}
  const out: string[] = []
  const judge = (ps: Record<string, number | null>, against: string) => {
    const tested = hs.filter((h) => typeof ps[h] === 'number')
    if (tested.length === 0) return null
    const yes = tested.filter((h) => (ps[h] as number) < alpha)
    const no = tested.filter((h) => (ps[h] as number) >= alpha)
    const p = (list: string[]) => list.map((h) => ps[h] as number)
    if (no.length === 0) {
      out.push(`Against ${against}: measurably smaller misses at every horizon tested, ${monthsList(yes)} (${pSpan(p(yes), true)}).`)
    } else if (yes.length === 0) {
      out.push(`Against ${against}: no measurable difference at any horizon tested, ${monthsList(no)} (${pSpan(p(no), false)}).`)
    } else {
      out.push(
        `Against ${against}: measurably smaller misses at ${monthsList(yes)} (${pSpan(p(yes), true)}), ` +
          `no measurable difference at ${monthsList(no)} (${pSpan(p(no), false)}).`
      )
    }
    return { yes, no }
  }
  judge(vsRw, 'assuming no change')
  const trend = judge(vsTrend, 'the dollar’s own past trend')
  if (trend) {
    if (trend.yes.length === 0) {
      out.push('In this test, pacing the dollar with liquidity growth added no skill the dollar’s own trend did not already have.')
    } else if (trend.no.length === 0) {
      out.push('In this test, pacing the dollar with liquidity growth added measurable skill over the dollar’s own trend at every horizon tested.')
    } else {
      out.push(
        `In this test, pacing the dollar with liquidity growth added measurable skill over the dollar’s own trend at ${monthsList(
          trend.yes
        )} only.`
      )
    }
  } else {
    out.push('No test against the dollar’s own trend is reported, so the backtest cannot say whether liquidity adds anything to it.')
  }
  return out
}
