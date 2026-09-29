import { parseInterval, type IntervalId } from './intervals'
import { parseChartSymbol, type ChartSymbol } from './symbols'

export type { ChartSymbol } from './symbols'

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
const LOG_SCALE_KEY = `${PREFIX}log_scale`

/**
 * The static tick group: the two symbols with years of stored history. The
 * picker shows them before the registry answers, and they stay when it does
 * not answer at all.
 */
export const CHART_SYMBOLS = ['IR_GOLD_18K', 'XAUUSD'] as const

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

/**
 * The stored symbol when it still has a symbol's shape; the default otherwise.
 * Whether a well-shaped registry code is still served is the API's answer
 * (a 400 the page shows), not something a stale preference can decide here.
 */
export function readSymbol(): ChartSymbol {
  return parseChartSymbol(readRaw(SYMBOL_KEY)) ?? DEFAULT_SYMBOL
}

export function writeSymbol(symbol: ChartSymbol): void {
  writeRaw(SYMBOL_KEY, symbol)
}

/**
 * Null when nothing is stored or the stored value is no longer a timeframe.
 *
 * This is the reader's CHOICE of timeframe. A Tehran symbol forces 1D while it
 * is on screen, and that must never be written back here: visiting TEDPIX from
 * a 4H gold chart would otherwise reset gold to 1D for good.
 */
export function readInterval(): IntervalId | null {
  return parseInterval(readRaw(INTERVAL_KEY))
}

export function writeInterval(interval: IntervalId): void {
  writeRaw(INTERVAL_KEY, interval)
}

/** The price axis in log scale — the only way a 9k → 3.7M index stays readable. */
export function readLogScale(): boolean {
  return readRaw(LOG_SCALE_KEY) === '1'
}

export function writeLogScale(on: boolean): void {
  writeRaw(LOG_SCALE_KEY, on ? '1' : '0')
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
