import { toJalaali } from 'jalaali-js'

export type DisplayUnit = 'IRT' | 'IRR'
export type CalendarMode = 'jalali' | 'gregorian'

// ---------- Numbers / currency ----------

export function formatGrouped(value: number, maxFraction = 0): string {
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: maxFraction }).format(value)
}

/** Display-only conversion: canonical IRT, rial view multiplies by 10. */
export function convertDisplay(valueIrt: number, unit: DisplayUnit): number {
  return unit === 'IRR' ? valueIrt * 10 : valueIrt
}

export function currencyLabel(unit: DisplayUnit): string {
  return unit === 'IRR' ? 'ریال' : 'تومان'
}

export function currencyCode(unit: DisplayUnit): string {
  return unit === 'IRR' ? 'IRR' : 'IRT'
}

/** e.g. formatToman(8120000, 'IRT') -> "8,120,000 تومان" */
export function formatToman(valueIrt: number, unit: DisplayUnit = 'IRT', withLabel = true): string {
  const grouped = formatGrouped(Math.round(convertDisplay(valueIrt, unit)))
  return withLabel ? `${grouped} ${currencyLabel(unit)}` : grouped
}

export function formatUsd(value: number, maxFraction = 2): string {
  return `$${new Intl.NumberFormat('en-US', {
    minimumFractionDigits: maxFraction > 0 ? 2 : 0,
    maximumFractionDigits: maxFraction
  }).format(value)}`
}

export function formatPct(
  value: number | null | undefined,
  opts: { sign?: boolean; digits?: number } = {}
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const digits = opts.digits ?? 2
  const sign = opts.sign !== false && value > 0 ? '+' : ''
  return `${sign}${value.toFixed(digits)}%`
}

/** CSS class for a signed number: 'pos' | 'neg' | 'flat'. */
export function pctClass(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value) || Math.abs(value) < 0.005) {
    return 'flat'
  }
  return value > 0 ? 'pos' : 'neg'
}

/** Compact toman/rial amount honoring the display-unit toggle (axis ticks). */
export function formatCompactToman(valueIrt: number, unit: DisplayUnit = 'IRT'): string {
  return formatCompact(convertDisplay(valueIrt, unit))
}

export function formatCompact(value: number): string {
  const abs = Math.abs(value)
  if (abs >= 1e9) return `${(value / 1e9).toFixed(abs >= 1e10 ? 0 : 1)}B`
  if (abs >= 1e6) return `${(value / 1e6).toFixed(abs >= 1e7 ? 0 : 1)}M`
  if (abs >= 1e3) return `${(value / 1e3).toFixed(abs >= 1e4 ? 0 : 1)}k`
  return String(Math.round(value * 100) / 100)
}

/** Normalizes a confidence that may be 0..1 or 0..100 into percent. */
export function confidencePct(confidence: number | null | undefined): number | null {
  if (confidence === null || confidence === undefined || Number.isNaN(confidence)) return null
  return confidence <= 1 ? confidence * 100 : confidence
}

// ---------- Dates (Asia/Tehran + Jalali) ----------

export interface DateParts {
  year: number
  month: number
  day: number
  hour: number
  minute: number
}

const pad2 = (n: number): string => String(n).padStart(2, '0')

/** Wall-clock parts of an instant in Asia/Tehran. */
export function tehranParts(input: string | Date): DateParts {
  const date = typeof input === 'string' ? new Date(input) : input
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Tehran',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false
  })
  const parts: Record<string, string> = {}
  for (const p of fmt.formatToParts(date)) parts[p.type] = p.value
  const hour = Number(parts.hour)
  return {
    year: Number(parts.year),
    month: Number(parts.month),
    day: Number(parts.day),
    hour: hour === 24 ? 0 : hour,
    minute: Number(parts.minute)
  }
}

/** Pure Gregorian -> Jalali conversion (month/day are 1-based). */
export function gregorianToJalali(gy: number, gm: number, gd: number): { jy: number; jm: number; jd: number } {
  const { jy, jm, jd } = toJalaali(gy, gm, gd)
  return { jy, jm, jd }
}

/** e.g. '2024-03-20T12:00:00Z' -> '1403/01/01' (Tehran wall clock). */
export function formatJalaliDate(input: string | Date): string {
  const p = tehranParts(input)
  const j = toJalaali(p.year, p.month, p.day)
  return `${j.jy}/${pad2(j.jm)}/${pad2(j.jd)}`
}

export function formatGregorianDate(input: string | Date): string {
  const p = tehranParts(input)
  return `${p.year}-${pad2(p.month)}-${pad2(p.day)}`
}

export function formatDate(input: string | Date | null | undefined, calendar: CalendarMode): string {
  if (!input) return '—'
  const d = typeof input === 'string' ? new Date(input) : input
  if (Number.isNaN(d.getTime())) return '—'
  return calendar === 'jalali' ? formatJalaliDate(d) : formatGregorianDate(d)
}

export function formatTime(input: string | Date): string {
  const p = tehranParts(input)
  return `${pad2(p.hour)}:${pad2(p.minute)}`
}

export function formatDateTime(input: string | Date | null | undefined, calendar: CalendarMode): string {
  if (!input) return '—'
  const d = typeof input === 'string' ? new Date(input) : input
  if (Number.isNaN(d.getTime())) return '—'
  return `${formatDate(d, calendar)} ${formatTime(d)}`
}

/** Short axis label, e.g. jalali '01/15' or gregorian '03/20'. */
export function shortDate(input: string | Date, calendar: CalendarMode): string {
  const p = tehranParts(input)
  if (calendar === 'jalali') {
    const j = toJalaali(p.year, p.month, p.day)
    return `${pad2(j.jm)}/${pad2(j.jd)}`
  }
  return `${pad2(p.month)}/${pad2(p.day)}`
}

export function relativeTime(input: string | Date | null | undefined): string {
  if (!input) return 'never'
  const t = (typeof input === 'string' ? new Date(input) : input).getTime()
  if (Number.isNaN(t)) return '—'
  const diffSec = Math.round((Date.now() - t) / 1000)
  if (diffSec < 0) return 'just now'
  if (diffSec < 60) return `${diffSec}s ago`
  const min = Math.floor(diffSec / 60)
  if (min < 60) return `${min}m ago`
  const hours = Math.floor(min / 60)
  if (hours < 48) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}
