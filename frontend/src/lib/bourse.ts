import { toGregorian, toJalaali } from 'jalaali-js'
import { tehranParts, type CalendarMode } from './format'
import type { BourseHistoryPoint, BourseIndexItem } from '../api/types'

/**
 * Pure helpers for the Tehran market page. Nothing here computes a return, a
 * drawdown or a dispersion — those arrive from the API, measured once in Go —
 * so this file is the calendar, the shading, the formatting and the sort.
 */

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n)
}

/**
 * 1 Farvardin of the current Jalali year, as a Gregorian ISO date, in Tehran.
 *
 * The Tehran market states its year-to-date from the last close of the
 * previous Jalali year, so the page asks the API for the return "since" this
 * day and the API measures it from the value in force then. The calendar
 * lives here, in the display layer, because it IS display: the server keeps
 * dates Gregorian and UTC.
 */
export function jalaliYearStart(now: Date): { iso: string; jy: number } {
  const p = tehranParts(now)
  const { jy } = toJalaali(p.year, p.month, p.day)
  const g = toGregorian(jy, 1, 1)
  return { iso: `${g.gy}-${pad2(g.gm)}-${pad2(g.gd)}`, jy }
}

/**
 * Runs of sessions whose value repeats the previous one exactly — a closed
 * market, or a sector index nothing in traded — at least `minRun` long.
 *
 * Shaded on the chart so a flat stretch reads as "the market was shut" and not
 * as "the market was calm": TEDPIX stood still for 50 sessions from 2026-02-25.
 * `from` is the last real session before the run, so the band covers the whole
 * flat segment the line draws.
 */
export function closureRuns(
  points: BourseHistoryPoint[],
  minRun = 5
): Array<{ from: string; to: string; sessions: number }> {
  const runs: Array<{ from: string; to: string; sessions: number }> = []
  let start = -1
  for (let i = 0; i <= points.length; i++) {
    const unchanged = i < points.length && points[i].unchanged === true
    if (unchanged && start < 0) start = i
    if (!unchanged && start >= 0) {
      const n = i - start
      if (n >= minRun) {
        runs.push({ from: points[Math.max(0, start - 1)].date, to: points[i - 1].date, sessions: n })
      }
      start = -1
    }
  }
  return runs
}

/**
 * A toman amount at the scale this market is spoken about in. A trillion toman
 * is one همت (هزار میلیارد تومان), the unit Iranian reporting uses for market
 * value and daily trade value, so the suffix is kept in the text beside it.
 */
export function formatTomanScaled(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const abs = Math.abs(value)
  const fmt = (v: number, d: number) =>
    new Intl.NumberFormat('en-US', { maximumFractionDigits: d, minimumFractionDigits: 0 }).format(v)
  if (abs >= 1e12) return `${fmt(value / 1e12, abs >= 1e14 ? 0 : 1)}T toman`
  if (abs >= 1e9) return `${fmt(value / 1e9, abs >= 1e11 ? 0 : 1)}B toman`
  if (abs >= 1e6) return `${fmt(value / 1e6, abs >= 1e8 ? 0 : 1)}M toman`
  return `${fmt(value, 0)} toman`
}

/** Dollars at the same scales. */
export function formatUsdScaled(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const abs = Math.abs(value)
  const sign = value < 0 ? '-' : ''
  const fmt = (v: number, d: number) =>
    new Intl.NumberFormat('en-US', { maximumFractionDigits: d, minimumFractionDigits: 0 }).format(v)
  if (abs >= 1e9) return `${sign}$${fmt(abs / 1e9, 1)}B`
  if (abs >= 1e6) return `${sign}$${fmt(abs / 1e6, 1)}M`
  return `${sign}$${fmt(abs, 0)}`
}

/** An index level: grouped, one decimal below 1,000, none above. */
export function formatIndexLevel(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return new Intl.NumberFormat('en-US', {
    maximumFractionDigits: Math.abs(value) >= 1000 ? 0 : 1
  }).format(value)
}

/**
 * Axis ticks that suit the span: a year on a multi-year chart, month/day on a
 * short one — in the reader's calendar either way.
 */
export function axisDate(iso: string, calendar: CalendarMode, spanDays: number): string {
  const p = tehranParts(new Date(iso))
  if (calendar === 'jalali') {
    const j = toJalaali(p.year, p.month, p.day)
    // The full year, never "04/08": two-digit Jalali years read as a day and
    // a month to anyone who has not been told otherwise.
    return spanDays > 730 ? String(j.jy) : `${j.jy}/${pad2(j.jm)}`
  }
  return spanDays > 730 ? String(p.year) : `${p.year}-${pad2(p.month)}`
}

export function spanDaysOf(points: Array<{ date: string }>): number {
  if (points.length < 2) return 0
  const a = Date.parse(points[0].date)
  const b = Date.parse(points[points.length - 1].date)
  return Math.round((b - a) / 86400000)
}

export type IndexSortKey =
  | 'order'
  | 'name'
  | 'change_1d_pct'
  | '1w'
  | '1m'
  | '3m'
  | '1y'
  | 'since'
  | 'from_ath'
  | 'dispersion'

/** The figure a sort key reads; null sorts last whichever way. */
export function sortValue(item: BourseIndexItem, key: IndexSortKey): number | string | null {
  switch (key) {
    case 'order':
      return item.display_order
    case 'name':
      return item.name_en
    case 'change_1d_pct':
      return item.change_1d_pct
    case 'since':
      return item.since_pct ?? null
    case 'from_ath':
      return item.from_all_time_high_pct
    case 'dispersion':
      return item.dispersion_1y_pct
    default:
      return item.returns[key] ?? null
  }
}

/**
 * Sort without letting a missing figure pose as the smallest or largest one:
 * an index whose 3-year return cannot be measured is not the worst performer.
 */
export function sortIndices(
  items: BourseIndexItem[],
  key: IndexSortKey,
  desc: boolean
): BourseIndexItem[] {
  return [...items].sort((a, b) => {
    const va = sortValue(a, key)
    const vb = sortValue(b, key)
    if (va === null && vb === null) return a.display_order - b.display_order
    if (va === null) return 1
    if (vb === null) return -1
    if (typeof va === 'string' || typeof vb === 'string') {
      const c = String(va).localeCompare(String(vb))
      return desc ? -c : c
    }
    return desc ? vb - va : va - vb
  })
}

/** The four breadth buckets as percentage widths that always sum to 100. */
export function breadthWidths(b: {
  down_over_2: number
  down_under_2: number
  up_under_2: number
  up_over_2: number
}): [number, number, number, number] {
  const total = b.down_over_2 + b.down_under_2 + b.up_under_2 + b.up_over_2
  if (total === 0) return [0, 0, 0, 0]
  return [
    (b.down_over_2 / total) * 100,
    (b.down_under_2 / total) * 100,
    (b.up_under_2 / total) * 100,
    (b.up_over_2 / total) * 100
  ]
}
