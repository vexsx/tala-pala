import { useApi } from '../hooks/useApi'
import type {
  TrendAlignmentResponse,
  TrendAlignmentState,
  TrendState,
  TrendSymbol,
  TrendTimeframe,
  TrendTimeframeKey
} from '../api/types'
import { useSettings } from '../lib/settings'
import {
  currencyCode,
  formatDateTime,
  formatToman,
  formatUsd,
  type CalendarMode,
  type DisplayUnit
} from '../lib/format'
import ErrorMessage from './ErrorMessage'
import EmptyState from './EmptyState'

/**
 * Trend alignment — the 1D / 4H / 1H price-versus-moving-average read.
 *
 * This file is a DISPLAY of the server's indicator and nothing else. Prices,
 * the three moving averages, candle times and freshness are rendered exactly as
 * GET /market/trend-alignment returned them: there is no moving-average
 * arithmetic anywhere below, and src/test/TrendAlignment.test.tsx pins that so
 * a "small helper" can never quietly turn into a second, disagreeing engine.
 *
 * The indicator is situational awareness for the desk. It touches no model
 * input, no model selection, no confidence, no interval and no buy/sell
 * decision — nothing here may ever be wired into those.
 *
 * Two views share one contract:
 *   <TrendAlignmentCard />  — compact, for the Overview.
 *   <TrendAlignmentTable /> — full row detail, for Technical analysis.
 */

const PATH = '/market/trend-alignment?symbol='

/** Slowest first: the way a desk reads confirmation down the timeframes. */
const TIMEFRAME_ORDER: TrendTimeframeKey[] = ['1d', '4h', '1h']

const TIMEFRAME_LABEL: Record<TrendTimeframeKey, string> = {
  '1d': '1D',
  '4h': '4H',
  '1h': '1H'
}

/**
 * Every state carries a distinct glyph AND a word, so the card still reads
 * correctly in greyscale, in a colour-blind palette, or read aloud.
 */
const TREND_GLYPH: Record<TrendState, string> = {
  bullish: '▲',
  bearish: '▼',
  neutral: '●',
  unavailable: '—'
}

const TREND_LABEL: Record<TrendState, string> = {
  bullish: 'BULLISH',
  bearish: 'BEARISH',
  neutral: 'NEUTRAL',
  unavailable: 'UNAVAILABLE'
}

const ALIGNMENT_GLYPH: Record<TrendAlignmentState, string> = {
  full_bullish: '▲',
  full_bearish: '▼',
  not_aligned: '◆'
}

const ALIGNMENT_LABEL: Record<TrendAlignmentState, string> = {
  full_bullish: 'FULL BULLISH',
  full_bearish: 'FULL BEARISH',
  not_aligned: 'NOT ALIGNED'
}

const ALIGNMENT_BADGE: Record<TrendAlignmentState, string> = {
  full_bullish: 'badge-ok',
  full_bearish: 'badge-bad',
  not_aligned: 'badge-off'
}

const SYMBOL_TITLE: Record<string, string> = {
  IR_GOLD_18K: '18k gold',
  XAUUSD: 'XAU/USD'
}

const SKELETON_ROWS = ['a', 'b', 'c']

export interface TrendAlignmentProps {
  /** Only IR_GOLD_18K and XAUUSD are served; anything else is a 400. */
  symbol?: TrendSymbol
}

interface Reading {
  loading: boolean
  error: string | null
  reload: () => void
  data: TrendAlignmentResponse | null
  neverEvaluated: boolean
}

/**
 * True when the API says the symbol has no stored state yet. Reported rather
 * than papered over: an un-run indicator is a different fact from a neutral
 * one, and inventing "not aligned" numbers for it would be a lie.
 */
export function isNeverEvaluated(data: TrendAlignmentResponse | null): boolean {
  if (data === null) return false
  const note = (data.note ?? '').toLowerCase()
  if (note.includes('never_evaluated') || note.includes('never evaluated')) return true
  // Belt and braces: a payload with no calculation timestamp has never run.
  return !data.calculated_at
}

function useReading(symbol: TrendSymbol): Reading {
  const res = useApi<TrendAlignmentResponse>(`${PATH}${encodeURIComponent(symbol)}`, [symbol])
  return {
    // The skeleton is first-load only; a later refresh must not blank the rows.
    loading: res.loading && res.data === null,
    error: res.error,
    reload: res.reload,
    data: res.data,
    neverEvaluated: isNeverEvaluated(res.data)
  }
}

function timeframeOf(
  data: TrendAlignmentResponse | null,
  key: TrendTimeframeKey
): TrendTimeframe | null {
  return data?.timeframes?.[key] ?? null
}

function trendOf(data: TrendAlignmentResponse | null, key: TrendTimeframeKey): TrendState {
  return timeframeOf(data, key)?.trend ?? 'unavailable'
}

function symbolTitle(symbol: string): string {
  return SYMBOL_TITLE[symbol] ?? symbol
}

/**
 * Price formatter for the symbol on screen: local gold honours the IRT/IRR
 * toggle, the global ounce is quoted in dollars. Never invents a value — a
 * missing number stays an em dash.
 */
function priceFormatter(symbol: string, unit: DisplayUnit): (value: number | null) => string {
  if (symbol === 'XAUUSD') return (value) => (value === null ? '—' : formatUsd(value))
  return (value) => (value === null ? '—' : formatToman(value, unit, false))
}

function unitNote(symbol: string, unit: DisplayUnit): string {
  return symbol === 'XAUUSD'
    ? 'Prices in USD per troy ounce.'
    : `Prices in ${currencyCode(unit)} per gram.`
}

/** Column heading taken from the server's own config, never hard-coded. */
function maColumn(data: TrendAlignmentResponse, key: 'fast' | 'mid' | 'slow'): string {
  const type = (data.ma_type ?? 'ema').toUpperCase()
  const period = data.periods ? data.periods[key] : null
  return period === null || period === undefined ? `${type} ${key}` : `${type}${period}`
}

/** One accessible sentence describing the whole card, for screen readers. */
function ariaSummary(symbol: TrendSymbol, reading: Reading): string {
  const name = symbolTitle(reading.data?.symbol ?? symbol)
  const head = `Trend alignment, ${name}`
  if (reading.loading) return `${head}: loading`
  if (reading.error !== null) return `${head}: unavailable`
  if (reading.data === null || reading.neverEvaluated) return `${head}: never evaluated`
  const data = reading.data
  const rows = TIMEFRAME_ORDER.map(
    (key) => `${TIMEFRAME_LABEL[key]} ${TREND_LABEL[trendOf(data, key)].toLowerCase()}`
  ).join(', ')
  const stale = data.data_fresh ? '' : ', data stale'
  return `${head}: ${ALIGNMENT_LABEL[data.alignment].toLowerCase()}${stale}. ${rows}.`
}

function TrendSkeleton({ label }: { label: string }) {
  return (
    <div className="trend-skeleton" role="status" aria-live="polite" aria-label={label}>
      {SKELETON_ROWS.map((row) => (
        <span key={row} className="trend-skeleton-bar" />
      ))}
    </div>
  )
}

function StaleChip({ when, calendar }: { when: string | null; calendar: CalendarMode }) {
  return (
    <span
      className="badge badge-warn trend-stale"
      title={
        when !== null
          ? `Last evaluated ${formatDateTime(when, calendar)} (Tehran) — at least one timeframe is running on old candles`
          : 'At least one timeframe is running on old candles'
      }
    >
      STALE
    </span>
  )
}

function NeverEvaluated() {
  return (
    <EmptyState
      title="NEVER EVALUATED"
      hint="The trend indicator has not run for this symbol yet — the read appears after its first evaluation."
    />
  )
}

/** Glyph + word, so colour is never carrying the meaning on its own. */
function TrendMark({ trend }: { trend: TrendState }) {
  return (
    <span className={`trend-state trend-${trend}`}>
      <span className="trend-glyph" aria-hidden="true">
        {TREND_GLYPH[trend]}
      </span>{' '}
      {TREND_LABEL[trend]}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Compact card — Overview
// ---------------------------------------------------------------------------

export function TrendAlignmentCard({ symbol = 'IR_GOLD_18K' }: TrendAlignmentProps) {
  const { calendar } = useSettings()
  const reading = useReading(symbol)
  const data = reading.data
  const readable = data !== null && !reading.neverEvaluated && reading.error === null
  const stale = readable && data.data_fresh === false

  return (
    <section
      className="card trend-card"
      aria-label={ariaSummary(symbol, reading)}
      data-testid="trend-alignment-card"
    >
      <div className="row space-between trend-head">
        <div className="card-title">TREND ALIGNMENT</div>
        <div className="trend-head-meta">
          {stale && <StaleChip when={data.calculated_at} calendar={calendar} />}
          {readable && (
            <span className={`badge ${ALIGNMENT_BADGE[data.alignment]}`}>
              {ALIGNMENT_LABEL[data.alignment]}
            </span>
          )}
        </div>
      </div>

      {reading.loading ? (
        <TrendSkeleton label="Loading trend alignment" />
      ) : reading.error !== null ? (
        // The card keeps its frame on failure: a card that vanishes reads as
        // "no trend", which is a different and misleading claim.
        <div className="trend-error">
          <div className="trend-state-title">TREND READ UNAVAILABLE</div>
          <ErrorMessage message={reading.error} onRetry={reading.reload} />
        </div>
      ) : data === null || reading.neverEvaluated ? (
        <NeverEvaluated />
      ) : (
        <>
          <ul className="trend-rows">
            {TIMEFRAME_ORDER.map((key) => {
              const timeframe = timeframeOf(data, key)
              const trend = trendOf(data, key)
              return (
                <li
                  className="trend-row"
                  key={key}
                  aria-label={`${TIMEFRAME_LABEL[key]} ${TREND_LABEL[trend]}`}
                  title={timeframe?.reason ? timeframe.reason : undefined}
                >
                  <span className="trend-tf mono">{TIMEFRAME_LABEL[key]}</span>
                  <TrendMark trend={trend} />
                </li>
              )
            })}
          </ul>

          <div className={`trend-verdict trend-align-${data.alignment}`}>
            <span className="muted small">Overall</span>
            <span className="trend-verdict-label">
              <span className="trend-glyph" aria-hidden="true">
                {ALIGNMENT_GLYPH[data.alignment]}
              </span>{' '}
              {ALIGNMENT_LABEL[data.alignment]}
            </span>
          </div>

          <p className="muted small trend-note">
            {data.calculated_at !== null
              ? `Read ${formatDateTime(data.calculated_at, calendar)} (Tehran).`
              : ''}{' '}
            Technical context only — it feeds no forecast and no buy/sell call.
          </p>
        </>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------
// Detailed table — Technical analysis
// ---------------------------------------------------------------------------

export function TrendAlignmentTable({ symbol = 'IR_GOLD_18K' }: TrendAlignmentProps) {
  const { unit, calendar } = useSettings()
  const reading = useReading(symbol)
  const data = reading.data
  const readable = data !== null && !reading.neverEvaluated && reading.error === null
  const stale = readable && data.data_fresh === false
  const shown = readable ? data.symbol : symbol
  const price = priceFormatter(shown, unit)

  return (
    <section
      className="card trend-table-card"
      aria-label={ariaSummary(symbol, reading)}
      data-testid="trend-alignment-table"
    >
      <div className="row space-between trend-head">
        <div className="card-title">TREND ALIGNMENT — {symbolTitle(shown)}</div>
        <div className="trend-head-meta">
          {stale && <StaleChip when={data.calculated_at} calendar={calendar} />}
        </div>
      </div>

      {reading.loading ? (
        <TrendSkeleton label="Loading trend alignment" />
      ) : reading.error !== null ? (
        <div className="trend-error">
          <div className="trend-state-title">TREND READ UNAVAILABLE</div>
          <ErrorMessage message={reading.error} onRetry={reading.reload} />
        </div>
      ) : data === null || reading.neverEvaluated ? (
        <NeverEvaluated />
      ) : (
        <>
          <div className={`trend-verdict trend-align-${data.alignment}`}>
            <span className="muted small">Overall</span>
            <span className="trend-verdict-label">
              <span className="trend-glyph" aria-hidden="true">
                {ALIGNMENT_GLYPH[data.alignment]}
              </span>{' '}
              {ALIGNMENT_LABEL[data.alignment]}
            </span>
            {data.previous_alignment !== null && data.previous_alignment !== data.alignment && (
              <span className="muted small trend-previous">
                was {ALIGNMENT_LABEL[data.previous_alignment]}
                {data.last_transition_at !== null
                  ? ` until ${formatDateTime(data.last_transition_at, calendar)}`
                  : ''}
              </span>
            )}
          </div>

          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Timeframe</th>
                  <th className="num">Price</th>
                  <th className="num">{maColumn(data, 'fast')}</th>
                  <th className="num">{maColumn(data, 'mid')}</th>
                  <th className="num">{maColumn(data, 'slow')}</th>
                  <th>Trend</th>
                  <th>Candle close</th>
                  <th>Fresh</th>
                </tr>
              </thead>
              <tbody>
                {TIMEFRAME_ORDER.map((key) => {
                  const timeframe = timeframeOf(data, key)
                  const trend = trendOf(data, key)
                  return (
                    <tr key={key}>
                      <td
                        className="mono"
                        title={
                          timeframe !== null
                            ? `${timeframe.history_points} candles of history`
                            : undefined
                        }
                      >
                        {TIMEFRAME_LABEL[key]}
                      </td>
                      <td className="num mono">{price(timeframe?.price ?? null)}</td>
                      <td className="num mono">{price(timeframe?.ma26 ?? null)}</td>
                      <td className="num mono">{price(timeframe?.ma48 ?? null)}</td>
                      <td className="num mono">{price(timeframe?.ma220 ?? null)}</td>
                      <td title={timeframe?.reason ? timeframe.reason : undefined}>
                        <TrendMark trend={trend} />
                      </td>
                      <td
                        className="mono"
                        title={
                          timeframe?.confirmed === false
                            ? 'The candle is still forming'
                            : undefined
                        }
                      >
                        {formatDateTime(timeframe?.candle_close_time ?? null, calendar)}
                      </td>
                      <td>
                        {timeframe === null ? (
                          <span className="badge badge-off">— NO DATA</span>
                        ) : timeframe.data_fresh ? (
                          <span className="badge badge-ok">✓ FRESH</span>
                        ) : (
                          <span className="badge badge-warn">! STALE</span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          <p className="muted small trend-note">
            {unitNote(shown, unit)} Candle close times are Tehran wall clock. Every value is served
            by the API — the page performs no moving-average arithmetic of its own. Technical
            context only: this read feeds no forecast, no confidence and no buy/sell call.
          </p>
        </>
      )}
    </section>
  )
}
