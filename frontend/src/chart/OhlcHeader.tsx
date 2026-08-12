import type { ChartCandle } from '../api/types'
import { useSettings } from '../lib/settings'
import {
  convertDisplay,
  formatDateTime,
  formatGrouped,
  formatPct,
  formatUsd,
  pctClass,
  type DisplayUnit
} from '../lib/format'
import { intervalLabel, type IntervalId } from './intervals'
import { indexOfTime, isSingleObservation } from './useCandles'

/**
 * One price formatter for the header and the chart's price axis, so a number
 * read off the scale and the same number read off the header can never disagree
 * about units. XAUUSD is quoted in dollars; everything else is toman/rial and
 * follows the site-wide IRT/IRR toggle.
 */
export function formatChartPrice(value: number, symbol: string, unit: DisplayUnit): string {
  if (symbol === 'XAUUSD') return formatUsd(value)
  return formatGrouped(Math.round(convertDisplay(value, unit)))
}

export interface OhlcHeaderProps {
  symbol: string
  interval: IntervalId
  /** The crosshair candle; null falls back to the latest bucket. */
  hovered: ChartCandle | null
  candles: ChartCandle[]
  unit: DisplayUnit
}

export function OhlcHeader({ symbol, interval, hovered, candles, unit }: OhlcHeaderProps) {
  const { calendar } = useSettings()

  if (candles.length === 0) {
    return (
      <div className="ohlc-head">
        <span className="ohlc-symbol">{symbol}</span>
        <span className="ohlc-interval">{intervalLabel(interval)}</span>
      </div>
    )
  }

  const index = hovered ? indexOfTime(candles, hovered.t) : candles.length - 1
  const bar = index >= 0 ? candles[index] : candles[candles.length - 1]
  const prev = index > 0 ? candles[index - 1] : null
  const tracking = hovered !== null && index >= 0

  // A bar's change is measured against the previous close, which is what a
  // trader reads; with no previous bar loaded, open-to-close is the honest
  // substitute rather than a blank.
  const basis = prev ? prev.close : bar.open
  const changePct = basis !== 0 ? ((bar.close - basis) / basis) * 100 : null
  const single = isSingleObservation(bar)
  const price = (value: number) => formatChartPrice(value, symbol, unit)

  return (
    <div className="ohlc-head">
      <span className="ohlc-symbol">{symbol}</span>
      <span className="ohlc-interval">{intervalLabel(interval)}</span>
      <span className="ohlc-time muted small">
        {formatDateTime(bar.open_time ?? new Date(bar.t * 1000), calendar)}
      </span>
      {!tracking && <span className="ohlc-latest muted small">latest</span>}

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
      <span className="ohlc-cell">
        <span className="ohlc-key muted">C</span>
        <span className="num mono">{price(bar.close)}</span>
      </span>
      <span className={`num mono ${pctClass(changePct)}`}>{formatPct(changePct)}</span>

      {single && (
        <span className="badge badge-off" title="One observation in this bucket — no traded range">
          1 obs
        </span>
      )}
    </div>
  )
}
