import type { CandleCoverage, InstrumentItem } from '../api/types'
import type { IntervalId } from './intervals'
import type { IndicatorKind, OverlayToggles } from './indicators/registry'

/**
 * The chart's symbol vocabulary, mirrored from
 * backend-go/internal/prices/chart_symbols.go.
 *
 * Three shapes, three sources behind one endpoint:
 *   - a registry code (IR_GOLD_18K, XAUUSD, IR_SILVER_999 …) — ticks from
 *     `prices`, bucketed at any timeframe the data supports;
 *   - IDX:<insCode> — a TSETMC index, one settled close per session;
 *   - EQ:<insCode> — a roster share, one adjusted bar per session, in rials.
 *
 * A Tehran symbol carries the exchange's numeric key rather than a Persian
 * name: a stored drawing or preference survives a renamed index, and two
 * spellings of one letter can never point at two charts.
 */

/** Any symbol the chart can be asked for. Validated by parseChartSymbol. */
export type ChartSymbol = string

export type SymbolKind = 'ticks' | 'tse_index' | 'tse_equity'

/** How a price on this symbol's axis is written. */
export type PriceQuote = 'IRT' | 'USD' | 'IRR' | 'points' | 'index' | 'pct'

const TEHRAN_RE = /^(IDX|EQ):([0-9]{6,20})$/
/** The shape of a registry code — the backend's tickSymbolRE. */
const TICK_RE = /^[A-Z][A-Z0-9_]{1,63}$/

/**
 * The five headline indices the backend names in code
 * (backend-go/internal/bourse/bourse.go). They are offered before the index
 * list arrives, so TEDPIX is one click away on a cold page.
 */
export const TSE_INDEX = {
  TEDPIX: 'IDX:32097828799138957',
  EQUAL_WEIGHTED: 'IDX:67130298613737946',
  TEPIX: 'IDX:5798407779416661',
  EQUAL_WEIGHTED_PRICE: 'IDX:8384385859414435',
  IFX: 'IDX:43685683301327984'
} as const

/** The only symbols the models forecast (backend predictions.ForecastSymbols). */
export const FORECAST_SYMBOLS: readonly string[] = ['IR_GOLD_18K', 'XAUUSD']

/** Normalize and validate a symbol from storage, a URL or a <select>. */
export function parseChartSymbol(raw: string | null | undefined): ChartSymbol | null {
  if (typeof raw !== 'string') return null
  const s = raw.trim().toUpperCase()
  if (TEHRAN_RE.test(s) || TICK_RE.test(s)) return s
  return null
}

export function symbolKind(symbol: ChartSymbol): SymbolKind {
  if (symbol.startsWith('IDX:')) return 'tse_index'
  if (symbol.startsWith('EQ:')) return 'tse_equity'
  return 'ticks'
}

export function isTehranSymbol(symbol: ChartSymbol): boolean {
  return symbolKind(symbol) !== 'ticks'
}

/** The TSETMC insCode inside a Tehran symbol, or null. */
export function tehranCode(symbol: ChartSymbol): string | null {
  const m = TEHRAN_RE.exec(symbol)
  return m ? m[2] : null
}

/**
 * The timeframes a symbol can EVER be served at, or null when the answer is
 * the data's coverage (tick symbols). A Tehran series is one settled session
 * per day and the API refuses every other interval for it.
 */
export function symbolIntervals(symbol: ChartSymbol): IntervalId[] | null {
  return isTehranSymbol(symbol) ? ['1d'] : null
}

/**
 * The coverage a Tehran symbol is known to have before its first response
 * arrives, so the timeframe strip is not briefly permissive — the same object
 * the API answers with.
 */
export const TSE_COVERAGE: CandleCoverage = {
  base_granularity_seconds: 86_400,
  intraday_from: null,
  history_from: null,
  supported_intervals: ['1d'],
  note: 'One settled TSETMC session per day; only the daily timeframe exists.'
}

// ---------------------------------------------------------------------------
// Units
// ---------------------------------------------------------------------------

/**
 * Quote currencies of the canonical symbols (migration 0024). Registry codes
 * added later arrive through registerRegistryQuotes; anything unknown is
 * toman, the house unit of `prices`.
 */
const STATIC_QUOTES: Record<string, PriceQuote> = {
  XAUUSD: 'USD',
  XAGUSD: 'USD',
  BRENT_OIL: 'USD',
  DXY: 'index',
  US10Y: 'pct',
  IR_GOLD_FUND_FLOW: 'pct'
}

const registryQuotes = new Map<string, PriceQuote>()

/**
 * Record the quote currency of every registry row the catalog loaded, so a
 * newly registered USD instrument is written in dollars without a code change.
 */
export function registerRegistryQuotes(items: InstrumentItem[]): void {
  for (const item of items) {
    const q = item.quote_currency
    if (q === 'USD' || q === 'IRT') registryQuotes.set(item.code, q)
  }
}

export function symbolQuote(symbol: ChartSymbol): PriceQuote {
  const kind = symbolKind(symbol)
  if (kind === 'tse_index') return 'points'
  if (kind === 'tse_equity') return 'IRR'
  return registryQuotes.get(symbol) ?? STATIC_QUOTES[symbol] ?? 'IRT'
}

/** Whether the site-wide toman/rial toggle means anything for this symbol. */
export function followsTomanToggle(symbol: ChartSymbol): boolean {
  return symbolQuote(symbol) === 'IRT'
}

// ---------------------------------------------------------------------------
// What can be drawn on which series
// ---------------------------------------------------------------------------

/**
 * Whether something can be drawn, and if not, why — `reason` is the sentence,
 * `short` the few words a crowded row has room for.
 */
export type Support = { ok: true } | { ok: false; reason: string; short: string }

const OK: Support = { ok: true }

/**
 * Whether a chart-wide overlay may be drawn over this symbol, and if not, why.
 *
 *  - The forecast exists for 18k gold and XAU/USD only; drawing gold's toman
 *    forecast on anything else was a bug, not a feature.
 *  - News markers are gold and macro headlines. Placed on a Tehran index they
 *    would read as the causes of its moves, which nothing here has shown.
 *  - The trend-alignment read is served for the two gold symbols only.
 */
export function overlaySupport(symbol: ChartSymbol, key: keyof OverlayToggles): Support {
  if (key === 'forecast') {
    return FORECAST_SYMBOLS.includes(symbol)
      ? OK
      : {
          ok: false,
          reason: 'No forecast is produced for this symbol — the models forecast 18k gold and XAU/USD only.',
          short: 'not forecast'
        }
  }
  if (key === 'events') {
    return isTehranSymbol(symbol)
      ? {
          ok: false,
          reason:
            'News markers are gold and macro headlines; on a Tehran market chart they would read as causes of its moves.',
          short: 'gold and macro news only'
        }
      : OK
  }
  return symbol === 'IR_GOLD_18K' || symbol === 'XAUUSD'
    ? OK
    : {
        ok: false,
        reason: 'The trend-alignment read is served for 18k gold and XAU/USD only.',
        short: 'gold only'
      }
}

const NEEDS_RANGE: IndicatorKind[] = ['supertrend', 'psar', 'ichimoku', 'pivots']

/**
 * Whether an indicator can be computed on a series with these price fields.
 * A close-only series — a Tehran index, or a registry series of one settled
 * close per session (silver 999, the Bahar coin, a fund with no live quote) —
 * is served without a high and a low, and SuperTrend, PSAR, Ichimoku and the
 * pivots are built from exactly those, so they are refused with the reason,
 * never approximated from the close.
 */
export function indicatorSupport(kind: IndicatorKind, priceFields: string[] | null | undefined): Support {
  if (!priceFields || (priceFields.includes('high') && priceFields.includes('low'))) return OK
  if (!NEEDS_RANGE.includes(kind)) return OK
  return {
    ok: false,
    reason:
      'Needs each session’s high and low, and this series is served as one closing value per session, without either.',
    short: 'needs a high and a low'
  }
}

/** Line for a close-only series, candles otherwise. */
export function seriesModeFor(
  symbol: ChartSymbol,
  priceFields: string[] | null | undefined
): 'candles' | 'line' {
  if (priceFields) return priceFields.includes('open') ? 'candles' : 'line'
  return symbolKind(symbol) === 'tse_index' ? 'line' : 'candles'
}
