import type { ChartCandle } from '../api/types'
import { useSettings } from '../lib/settings'
import { formatIndexLevel } from '../lib/bourse'
import {
  convertDisplay,
  formatDate,
  formatDateTime,
  formatGrouped,
  formatPct,
  formatUsd,
  pctClass,
  type DisplayUnit
} from '../lib/format'
import { intervalLabel, intervalSeconds, type IntervalId } from './intervals'
import { symbolKind, symbolQuote } from './symbols'
import { indexOfTime, isHalted, isSingleObservation } from './useCandles'

/**
 * Rials as the exchange quotes them: whole rials, and two decimals below 100 —
 * back-adjusted prices early in a long history can be single-digit rials, and
 * rounding those would draw a staircase.
 */
function formatRial(value: number): string {
  return Math.abs(value) < 100 ? formatGrouped(value, 2) : formatGrouped(Math.round(value))
}

/**
 * One price formatter for the header, the chart's price axis, the legend, the
 * panes and the drawing labels, so a number read off the scale and the same
 * number read off the header can never disagree about units.
 *
 *  - A Tehran index is in index POINTS: not money, and the toman/rial toggle
 *    means nothing for it.
 *  - A Tehran share is in RIALS, as TSETMC quotes it: the toggle's ×10 would
 *    make it ten times wrong.
 *  - A registry code quoted in dollars is written in dollars.
 *  - Everything else in `prices` is toman and follows the site-wide IRT/IRR
 *    toggle.
 */
export function formatChartPrice(value: number, symbol: string, unit: DisplayUnit): string {
  switch (symbolQuote(symbol)) {
    case 'points':
      return formatIndexLevel(value)
    case 'IRR':
      return formatRial(value)
    case 'USD':
      return formatUsd(value)
    case 'index':
      return formatGrouped(value, 2)
    case 'pct':
      return `${formatGrouped(value, 3)}%`
    default:
      return formatGrouped(Math.round(convertDisplay(value, unit)))
  }
}

/** What the header says the numbers are in, for a series off the toggle. */
function unitCaption(symbol: string): string | null {
  switch (symbolKind(symbol)) {
    case 'tse_index':
      return 'index points'
    case 'tse_equity':
      return 'rials, adjusted'
    default:
      return null
  }
}

export interface OhlcHeaderProps {
  symbol: string
  /** What to call the symbol; the raw IDX:/EQ: key is not a name. */
  label?: string
  interval: IntervalId
  /** The crosshair candle; null falls back to the latest bucket. */
  hovered: ChartCandle | null
  candles: ChartCandle[]
  unit: DisplayUnit
  /** The response's price_fields; ['close'] is a close-only series. */
  priceFields?: string[] | null
}

export function OhlcHeader({
  symbol,
  label,
  interval,
  hovered,
  candles,
  unit,
  priceFields
}: OhlcHeaderProps) {
  const { calendar } = useSettings()
  const name = label ?? symbol
  const caption = unitCaption(symbol)

  if (candles.length === 0) {
    return (
      <div className="ohlc-head">
        <span className="ohlc-symbol">{name}</span>
        <span className="ohlc-interval">{intervalLabel(interval)}</span>
        {caption && <span className="ohlc-unit muted small">{caption}</span>}
      </div>
    )
  }

  const index = hovered ? indexOfTime(candles, hovered.t) : candles.length - 1
  const bar = index >= 0 ? candles[index] : candles[candles.length - 1]
  const prev = index > 0 ? candles[index - 1] : null
  const tracking = hovered !== null && index >= 0

  // A bar's change is measured against the previous close, which is what a
  // trader reads; with no previous bar loaded, open-to-close is the honest
  // substitute rather than a blank — and a close-only series has no open, so
  // it has no substitute either.
  const basis = prev ? prev.close : bar.open
  const changePct = basis !== null && basis !== 0 ? ((bar.close - basis) / basis) * 100 : null
  const closeOnly = Array.isArray(priceFields) && !priceFields.includes('open')
  // A close-only series has no range by definition, so "one observation, no
  // range" says nothing about any one bar of it.
  const single = !closeOnly && isSingleObservation(bar)
  const halted = isHalted(bar)
  // A bucket of a day or longer is a date: the Tehran rendering of its UTC
  // start ("03:30") is a time nobody traded at.
  const dated = caption !== null || intervalSeconds(interval) >= 86_400
  const equity = symbolKind(symbol) === 'tse_equity'
  const price = (value: number | null) => (value === null ? '—' : formatChartPrice(value, symbol, unit))

  return (
    <div className="ohlc-head">
      <span className="ohlc-symbol">{name}</span>
      <span className="ohlc-interval">{intervalLabel(interval)}</span>
      {caption && <span className="ohlc-unit muted small">{caption}</span>}
      <span className="ohlc-time muted small">
        {/* A Tehran session, and any bucket of a day or more, is a DATE; a
            clock time on it would be the Tehran rendering of UTC midnight,
            which no one traded at. */}
        {dated
          ? formatDate(bar.open_time ?? new Date(bar.t * 1000), calendar)
          : formatDateTime(bar.open_time ?? new Date(bar.t * 1000), calendar)}
      </span>
      {!tracking && <span className="ohlc-latest muted small">latest</span>}

      {!closeOnly && (
        <>
          <span className="ohlc-cell">
            <span className="ohlc-key muted">O</span>
            <span className="num mono">{price(bar.open)}</span>
          </span>
          <span className="ohlc-cell">
            <span className="ohlc-key muted">H</span>
            <span className="num mono">{price(bar.high)}</span>
          </span>
          <span className="ohlc-cell">
            <span className="ohlc-key muted">L</span>
            <span className="num mono">{price(bar.low)}</span>
          </span>
        </>
      )}
      <span className="ohlc-cell">
        {equity ? (
          <span className="ohlc-key muted" title="The official closing price (قیمت پایانی)">
            Final
          </span>
        ) : (
          <span className="ohlc-key muted">C</span>
        )}
        <span className="num mono">{price(bar.close)}</span>
      </span>
      {equity && typeof bar.last_trade === 'number' && (
        <span className="ohlc-cell">
          <span className="ohlc-key muted">Last</span>
          <span className="num mono">{price(bar.last_trade)}</span>
        </span>
      )}
      <span className={`num mono ${pctClass(changePct)}`}>{formatPct(changePct)}</span>

      {single && (
        <span className="badge badge-off" title="One observation in this bucket — no traded range">
          1 obs
        </span>
      )}
      {bar.unchanged === true && (
        <span
          className="badge badge-off"
          title="This close repeats the previous session exactly: the market was closed, or nothing in the index traded."
        >
          no session
        </span>
      )}
      {bar.rescaled === true && (
        <span
          className="badge badge-off"
          title="TSETMC stored this close off by a power of ten; it is shown corrected."
        >
          corrected
        </span>
      )}
      {halted && (
        <span
          className="badge badge-warn"
          title="Nothing traded this session; the price shown is TSETMC's carried reference, not a trade."
        >
          halted — no trade
        </span>
      )}
      {bar.close_outside_range === true && (
        <span
          className="badge badge-off"
          title="The official closing price is volume-weighted over the session and here sits outside the traded high–low."
        >
          final outside range
        </span>
      )}
    </div>
  )
}
