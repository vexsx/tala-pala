import { parseInterval, type IntervalId } from './intervals'

/**
 * Chart preferences, persisted the same guarded way as lib/settings.tsx: every
 * read is validated and every access is wrapped, because localStorage throws in
 * private-mode Safari and is shadowed by an unusable global under Node.
 *
 * Only choices live here. Candle arrays are never stored — they are large,
 * they go stale in seconds, and a cached bar is a lie the next morning.
 */
const PREFIX = 'igp_chart_'

const SYMBOL_KEY = `${PREFIX}symbol`
const INTERVAL_KEY = `${PREFIX}interval`
const INDICATORS_KEY = `${PREFIX}indicators`

/** The only two symbols with enough history to chart. */
export const CHART_SYMBOLS = ['IR_GOLD_18K', 'XAUUSD'] as const

export type ChartSymbol = (typeof CHART_SYMBOLS)[number]

export const DEFAULT_SYMBOL: ChartSymbol = 'IR_GOLD_18K'

function readRaw(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

function writeRaw(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    // localStorage unavailable — preferences are a nicety, not a requirement
  }
}

export function readSymbol(): ChartSymbol {
  const raw = readRaw(SYMBOL_KEY)
  return (CHART_SYMBOLS as readonly string[]).includes(raw ?? '')
    ? (raw as ChartSymbol)
    : DEFAULT_SYMBOL
}

export function writeSymbol(symbol: ChartSymbol): void {
  writeRaw(SYMBOL_KEY, symbol)
}

/** Null when nothing is stored or the stored value is no longer a timeframe. */
export function readInterval(): IntervalId | null {
  return parseInterval(readRaw(INTERVAL_KEY))
}

export function writeInterval(interval: IntervalId): void {
  writeRaw(INTERVAL_KEY, interval)
}

/**
 * The indicator set is stored here rather than in the indicators module so the
 * chart has exactly one persistence surface. Ids are opaque to this file: it
 * only guarantees an array of non-empty strings comes back.
 */
export function readIndicators(): string[] {
  const raw = readRaw(INDICATORS_KEY)
  if (!raw) return []
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter((v): v is string => typeof v === 'string' && v.length > 0)
  } catch {
    return []
  }
}

export function writeIndicators(ids: string[]): void {
  writeRaw(INDICATORS_KEY, JSON.stringify(ids))
}
