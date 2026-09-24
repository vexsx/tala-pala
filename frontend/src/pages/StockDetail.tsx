import { useMemo, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import {
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from 'recharts'
import { useApi } from '../hooks/useApi'
import { useSettings } from '../lib/settings'
import { ChartTip } from '../components/PriceChart'
import Loading from '../components/Loading'
import { formatCompact, formatDate, formatGrouped, formatPct } from '../lib/format'
import type { StockBar, StockBarsResponse } from '../api/types'

/**
 * One Tehran instrument, its adjusted price history and the corporate-action
 * chain behind it.
 *
 * This page exists because /api/v1/stocks/{symbol}/bars was fully built,
 * validated and tested, and had NO user interface at all: the screener could
 * rank nineteen instruments and a reader could not then look at any of them.
 *
 * THREE THINGS THIS PAGE REFUSES TO DO, each of which is the obvious
 * implementation:
 *
 *  1. DRAW A LINE THROUGH A HALT. TSETMC stores a suspended session as a bar
 *     with zeros for open/high/low and the CARRIED REFERENCE PRICE in close,
 *     so a halt looks like a flat trading day rather than an absent one.
 *     Connecting those points draws prices nobody transacted at. This is not
 *     a corner case on this exchange: فولاد had 43 halted sessions in one
 *     66-session window while شستا had none. The series is broken at every
 *     halt and the gap is counted in prose.
 *
 *  2. CALL THE PRICES TOMAN. This endpoint serves RIALS -- the only one in the
 *     API that does -- and the unit is printed beside every number rather than
 *     assumed from the house convention.
 *
 *  3. SHOW RAW PRICES WITHOUT SAYING WHAT THEY ARE. The raw close series is
 *     not a price history: فولاد is x1.52 raw against x907.86 adjusted over
 *     nineteen years, because capital increases are not price moves. Raw is
 *     available, because checking the adjustment against it is legitimate and
 *     useful, but it is never the default and never unlabelled.
 */

/** Windows offered, in days back from the newest stored session. */
const WINDOWS = [
  { key: '3m', label: '3 months', days: 92 },
  { key: '1y', label: '1 year', days: 366 },
  { key: '5y', label: '5 years', days: 1827 },
  { key: 'max', label: 'Max', days: 0 }
] as const

type WindowKey = (typeof WINDOWS)[number]['key']

/** The API's own ceiling (maxBarLimit), so `max` asks for everything it has. */
const MAX_BARS = 6000

function windowQuery(w: WindowKey): string {
  const spec = WINDOWS.find((x) => x.key === w)
  if (!spec || spec.days === 0) return `limit=${MAX_BARS}`
  const from = new Date(Date.now() - spec.days * 86400000).toISOString().slice(0, 10)
  return `from=${from}&limit=${MAX_BARS}`
}

/**
 * A chart point per stored session, with `price` NULL on a halt.
 *
 * Recharts breaks a Line wherever the value is null, which is exactly the
 * right rendering: no price formed, so no segment is drawn. The alternative --
 * carrying the reference price forward -- produces a flat line that a reader
 * cannot distinguish from a quiet but real market.
 */
export function toChartPoints(items: StockBar[]): Array<{
  date: string
  price: number | null
  halted: number | null
}> {
  return items.map((b) => ({
    date: b.date,
    price: b.traded ? b.final_close : null,
    halted: b.traded ? null : b.final_close
  }))
}

/** Contiguous runs of halted sessions, longest first. */
export function haltRuns(items: StockBar[]): Array<{ from: string; to: string; sessions: number }> {
  const runs: Array<{ from: string; to: string; sessions: number }> = []
  let start: string | null = null
  let last = ''
  let n = 0
  for (const b of items) {
    if (!b.traded) {
      if (start === null) start = b.date
      last = b.date
      n += 1
      continue
    }
    if (start !== null) {
      runs.push({ from: start, to: last, sessions: n })
      start = null
      n = 0
    }
  }
  if (start !== null) runs.push({ from: start, to: last, sessions: n })
  return runs.sort((a, b) => b.sessions - a.sessions)
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="sd-stat">
      <div className="field-label">{label}</div>
      <div className="stat-value mono">{value}</div>
      {hint ? <div className="muted small">{hint}</div> : null}
    </div>
  )
}

export default function StockDetail() {
  const { symbol = '' } = useParams<{ symbol: string }>()
  const { calendar } = useSettings()
  const [win, setWin] = useState<WindowKey>('1y')
  const [adjusted, setAdjusted] = useState(true)

  const path = symbol
    ? `/stocks/${encodeURIComponent(symbol)}/bars?${windowQuery(win)}&adjusted=${adjusted}`
    : null
  const bars = useApi<StockBarsResponse>(path, [symbol, win, adjusted])

  const data = bars.data
  // What the RESPONSE says, not what the toggle asked for. The two can differ
  // — a caller can ask for adjusted and the server is entitled to answer
  // otherwise — and a page that labels its prices from its own request state
  // will eventually describe a series it is not showing.
  const showingAdjusted = data?.adjusted ?? adjusted
  const points = useMemo(() => (data ? toChartPoints(data.items) : []), [data])
  const runs = useMemo(() => (data ? haltRuns(data.items) : []), [data])
  const tradedCount = useMemo(
    () => (data ? data.items.filter((b) => b.traded).length : 0),
    [data]
  )

  if (bars.loading) return <Loading />
  if (bars.error) {
    return (
      <div className="page">
        <h2 className="page-title">{symbol}</h2>
        <div className="card error-box">
          <div className="card-title">This instrument could not be loaded</div>
          <p className="muted small">{bars.error}</p>
          <p className="muted small">
            An adjusted read of an instrument whose corporate-action adjustment failed
            validation is refused outright rather than served as a quietly-raw series. If that
            is what happened, the raw series is still available.
          </p>
          <button className="btn" onClick={() => setAdjusted(false)} disabled={!adjusted}>
            Show the raw series instead
          </button>
        </div>
      </div>
    )
  }
  if (!data) return null

  const adj = data.adjustment
  const halted = data.items.length - tradedCount

  return (
    <div className="page sd">
      <div className="row space-between wrap">
        <div>
          <h2 className="page-title">
            <span className="bidi-fa" lang="fa" dir="rtl">
              {data.symbol}
            </span>
          </h2>
          <div className="muted">
            <span className="bidi-fa" lang="fa" dir="rtl">
              {data.name_fa}
            </span>
            {' · '}
            <span className="mono small">{data.ins_code}</span>
          </div>
        </div>
        <Link className="btn btn-ghost" to="/stocks">
          ← Back to the screener
        </Link>
      </div>

      <div className="row wrap sd-controls">
        <div className="chip-row" role="group" aria-label="Window">
          {WINDOWS.map((w) => (
            <button
              key={w.key}
              className={`chip ${win === w.key ? 'active' : ''}`}
              aria-pressed={win === w.key}
              onClick={() => setWin(w.key)}
            >
              {w.label}
            </button>
          ))}
        </div>
        <div className="chip-row" role="group" aria-label="Price basis">
          <button
            className={`chip ${adjusted ? 'active' : ''}`}
            aria-pressed={adjusted}
            onClick={() => setAdjusted(true)}
          >
            Adjusted
          </button>
          <button
            className={`chip ${!adjusted ? 'active' : ''}`}
            aria-pressed={!adjusted}
            onClick={() => setAdjusted(false)}
          >
            Raw
          </button>
        </div>
      </div>

      {/*
       * The unit, before any number. This is the only endpoint in the API that
       * serves rials; every other Iranian amount on this site is toman, and a
       * reader carrying the house convention across is out by ten.
       */}
      <p className="muted small sd-unit" data-testid="sd-basis">
        Prices in <strong>{data.currency === 'IRR' ? 'rials' : data.currency}</strong> — this
        endpoint is the exception on this site, where Iranian amounts are otherwise toman.{' '}
        {showingAdjusted ? (
          <>
            Closes are <strong>adjusted</strong> for {formatGrouped(adj.actions_applied)} corporate
            action{adj.actions_applied === 1 ? '' : 's'}: a raw Tehran series is not a price
            history, because capital increases and splits are not price moves.
          </>
        ) : (
          <>
            <strong>Raw</strong> closes, exactly as TSETMC published them. Useful for checking the
            adjustment against, but not a price history: every capital increase appears here as a
            fall that never happened.
          </>
        )}
      </p>

      <div className="card">
        <div className="row space-between wrap">
          <div className="card-title">
            {showingAdjusted ? 'Adjusted' : 'Raw'} close · {WINDOWS.find((w) => w.key === win)?.label}
          </div>
          <div className="muted small">
            {formatDate(data.from ?? null, calendar)} → {formatDate(data.to ?? null, calendar)}
          </div>
        </div>

        {tradedCount === 0 ? (
          <p className="muted" data-testid="sd-no-trades">
            No session in this window had a trade, so there is no price line to draw. The
            instrument was suspended throughout; widen the window to see when it last traded.
          </p>
        ) : (
          <div className="sd-chart">
            <ResponsiveContainer width="100%" height={340}>
              <ComposedChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={40} />
                <YAxis
                  tick={{ fontSize: 11 }}
                  width={64}
                  domain={['auto', 'auto']}
                  tickFormatter={(v: number) => formatCompact(v)}
                />
                <Tooltip content={<ChartTip format={(v) => `${formatGrouped(v)} rial`} />} />
                {/*
                 * The connect-nulls prop is FALSE and must stay false: true would bridge
                 * every suspension with a straight line between the last and
                 * next real print — drawing a price path through days on which
                 * nothing traded.
                 */}
                <Line
                  type="monotone"
                  dataKey="price"
                  stroke="var(--accent)"
                  dot={false}
                  strokeWidth={2}
                  connectNulls={false}
                  isAnimationActive={false}
                  name={showingAdjusted ? 'Adjusted close' : 'Raw close'}
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        )}

        <div className="muted small sd-halts" data-testid="sd-halts">
          {halted === 0 ? (
            <>Every one of the {formatGrouped(data.items.length)} stored sessions in this window traded.</>
          ) : (
            <>
              <strong>
                {formatGrouped(halted)} of {formatGrouped(data.items.length)} stored sessions had no
                trade
              </strong>{' '}
              and are gaps in the line above, not flat days. TSETMC stores a suspension as a bar
              carrying the previous reference price, so joining across them would draw prices
              nobody transacted at.
              {runs.length > 0 && (
                <>
                  {' '}
                  The longest run is {formatGrouped(runs[0].sessions)} session
                  {runs[0].sessions === 1 ? '' : 's'}, {formatDate(runs[0].from, calendar)} →{' '}
                  {formatDate(runs[0].to, calendar)}.
                </>
              )}
            </>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-title">The corporate-action chain</div>
        <p className="muted small">
          Every adjusted price above is a raw TSETMC close multiplied by a cumulative factor this
          platform computed from {formatGrouped(adj.actions_applied)} detected action
          {adj.actions_applied === 1 ? '' : 's'}. That is why these numbers do not match a TSETMC
          screen, and the chain is published here so the difference is checkable rather than
          mysterious.
        </p>

        <div className="sd-stats">
          <Stat
            label="Validation"
            value={adj.status}
            hint={
              adj.adjusted_servable
                ? 'Passed the gate, so adjusted reads are served.'
                : 'Refused: an adjusted read is a 409, never a quietly-raw series.'
            }
          />
          <Stat label="Actions applied" value={formatGrouped(adj.actions_applied)} />
          <Stat
            label="Worst session return"
            value={
              adj.worst_session_return === undefined
                ? '—'
                : formatPct(adj.worst_session_return * 100, { sign: true })
            }
            hint="The largest single-session move that survived adjustment. A missed action shows up here."
          />
          <Stat
            label="Reopenings"
            value={formatGrouped(adj.reopenings)}
            hint="Sessions after a long suspension, held to a looser bound than ordinary ones."
          />
          <Stat
            label="Adjusted from"
            value={adj.adjusted_first_bar ? formatDate(adj.adjusted_first_bar, calendar) : '—'}
            hint={
              adj.pre_listing_bars
                ? `${formatGrouped(adj.pre_listing_bars)} pre-listing bars at par value are excluded.`
                : undefined
            }
          />
          <Stat
            label="Computed"
            value={adj.computed_at ? formatDate(adj.computed_at, calendar) : '—'}
            hint={adj.version || undefined}
          />
        </div>

        {adj.refusal_reason ? (
          <p className="muted small sd-refusal" data-testid="sd-refusal">
            <strong>Why it was refused:</strong> {adj.refusal_reason}
          </p>
        ) : null}
      </div>

      {data.has_more ? (
        <p className="muted small">
          This window holds more sessions than were returned ({formatGrouped(data.count)} of a{' '}
          {formatGrouped(data.limit)} limit). Narrow the window to see the rest.
        </p>
      ) : null}
      {data.note ? <p className="muted small">{data.note}</p> : null}
    </div>
  )
}
