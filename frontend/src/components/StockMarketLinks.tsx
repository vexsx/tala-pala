import { useMemo, useRef } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from 'recharts'
import { useApi } from '../hooks/useApi'
import { datedTip } from './PriceChart'
import Loading from './Loading'
import { formatCompact, formatDate, formatGrouped, formatPct, pctClass } from '../lib/format'
import { axisDate, formatTomanScaled, spanDaysOf } from '../lib/bourse'
import type { FlowSummary, StockFlowsResponse, StockRelativeResponse } from '../api/types'

/**
 * A share against its market, on the share's own page: its adjusted return
 * beside TEDPIX and beside its own sector's index, and who has been buying it.
 *
 * Both sections take the page's window, so the price chart, the comparison and
 * the flow always describe the same stretch of time.
 */

type Calendar = 'jalali' | 'gregorian'

function Pct({ value, digits = 1 }: { value: number | null | undefined; digits?: number }) {
  return <span className={`mono ${pctClass(value)}`}>{formatPct(value, { digits })}</span>
}

function Fa({ children }: { children: string }) {
  return (
    <span className="bidi-fa" lang="fa" dir="rtl">
      {children}
    </span>
  )
}

function useHeld<T>(state: { data: T | null }): T | null {
  const last = useRef<T | null>(null)
  if (state.data) last.current = state.data
  return state.data ?? last.current
}

// --- against the market --------------------------------------------------------

export function StockRelative({
  symbol,
  period,
  calendar
}: {
  symbol: string
  period: string
  calendar: Calendar
}) {
  const path = `/stocks/${encodeURIComponent(symbol)}/relative?period=${period}`
  const rel = useApi<StockRelativeResponse>(path, [path])
  const data = useHeld(rel)
  const span = data ? spanDaysOf(data.points) : 0
  const market = data?.benchmarks.find((b) => b.role === 'market')
  const sector = data?.benchmarks.find((b) => b.role === 'sector')
  const hasSector = Boolean(sector?.index && !sector.reason)
  const s = data?.summary

  return (
    <div className="card" data-testid="sd-relative">
      <div className="card-title">Against its market</div>
      {rel.error && !data ? (
        <p className="muted small" data-testid="sd-relative-error">
          {rel.error}
        </p>
      ) : !data ? (
        <Loading />
      ) : (
        <div className={rel.loading ? 'bx-refetch' : undefined}>
          <div className="bx-legend" aria-hidden="true">
            <span className="bx-legend-item"><span className="bx-key" style={{ borderTopColor: 'var(--series-1)' }} /> <Fa>{data.symbol}</Fa> (adjusted)</span>
            <span className="bx-legend-item"><span className="bx-key" style={{ borderTopColor: 'var(--series-2)' }} /> {market?.index?.name_en ?? 'TEDPIX'}</span>
            {hasSector ? (
              <>
                <span className="bx-legend-item"><span className="bx-key" style={{ borderTopColor: 'var(--series-3)' }} /> {sector?.index?.name_en} (sector)</span>
              </>
            ) : null}
          </div>
          <ResponsiveContainer width="100%" height={280}>
            <LineChart data={data.points} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
              <CartesianGrid stroke="var(--border)" vertical={false} />
              <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} tickFormatter={(d: string) => axisDate(d, calendar, span)} />
              <YAxis tick={{ fontSize: 11 }} width={56} domain={['auto', 'auto']} tickFormatter={(v: number) => formatCompact(v)} />
              <Tooltip content={datedTip(calendar, (v) => v.toFixed(1))} />
              <ReferenceLine y={100} stroke="var(--muted)" strokeOpacity={0.6} />
              {/* connectNulls stays false: a halted session is a gap in the share, never a bridge. */}
              <Line type="linear" dataKey="stock" name={data.symbol} stroke="var(--series-1)" strokeWidth={2} dot={false} connectNulls={false} isAnimationActive={false} />
              <Line type="linear" dataKey="market" name={market?.index?.name_en ?? 'TEDPIX'} stroke="var(--series-2)" strokeWidth={2} dot={false} isAnimationActive={false} />
              {hasSector ? (
                <Line type="linear" dataKey="sector" name={sector?.index?.name_en ?? 'Sector'} stroke="var(--series-3)" strokeWidth={2} dot={false} isAnimationActive={false} />
              ) : null}
            </LineChart>
          </ResponsiveContainer>
          {s ? (
            <p className="bx-verdict" data-testid="sd-relative-summary">
              From {formatDate(s.from, calendar)} to {formatDate(s.to, calendar)}: <Fa>{data.symbol}</Fa> <Pct value={s.stock_return_pct} />,{' '}
              {market?.index?.name_en?.split(',')[0] ?? 'TEDPIX'} <Pct value={s.market_return_pct} />
              {hasSector ? (
                <>
                  , its sector <Pct value={s.sector_return_pct} />
                </>
              ) : null}
              . That is{' '}
              <span className={`mono bx-nowrap ${pctClass(s.excess_vs_market_pp)}`}>
                {s.excess_vs_market_pp == null ? '—' : `${s.excess_vs_market_pp > 0 ? '+' : ''}${s.excess_vs_market_pp.toFixed(1)} pp`}
              </span>{' '}
              against the market
              {hasSector && s.excess_vs_sector_pp != null ? (
                <>
                  {' '}
                  and{' '}
                  <span className={`mono bx-nowrap ${pctClass(s.excess_vs_sector_pp)}`}>
                    {`${s.excess_vs_sector_pp > 0 ? '+' : ''}${s.excess_vs_sector_pp.toFixed(1)} pp`}
                  </span>{' '}
                  against its sector
                </>
              ) : null}
              .
            </p>
          ) : null}
          {s ? (
            <div className="bx-stats" data-testid="sd-beta">
              <div>
                <div className="field-label">Beta to the market</div>
                <span className="mono">{s.vs_market.beta == null ? '—' : s.vs_market.beta.toFixed(2)}</span>
                <div className="muted small">
                  {s.vs_market.beta == null
                    ? s.vs_market.reason
                    : `correlation ${s.vs_market.correlation?.toFixed(2) ?? '—'}, ${formatGrouped(s.vs_market.pairs)} sessions`}
                </div>
              </div>
              {s.vs_sector ? (
                <div>
                  <div className="field-label">Beta to its sector</div>
                  <span className="mono">{s.vs_sector.beta == null ? '—' : s.vs_sector.beta.toFixed(2)}</span>
                  <div className="muted small">
                    {s.vs_sector.beta == null
                      ? s.vs_sector.reason
                      : `correlation ${s.vs_sector.correlation?.toFixed(2) ?? '—'}, ${formatGrouped(s.vs_sector.pairs)} sessions`}
                  </div>
                </div>
              ) : null}
              <div>
                <div className="field-label">Traded / halted sessions</div>
                <span className="mono">
                  {formatGrouped(s.traded_sessions)} / {formatGrouped(s.halted_sessions)}
                </span>
                <div className="muted small">
                  {s.vs_market.multi_session_spans > 0
                    ? `${formatGrouped(s.vs_market.multi_session_spans)} span(s) across a halt are left out of the beta`
                    : 'every span in the beta is a single session'}
                </div>
              </div>
            </div>
          ) : null}
          {sector?.reason ? <p className="muted small">No sector line: {sector.reason}.</p> : null}
          {data.notes.map((n) => (
            <p key={n} className="muted small">
              {n}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}

// --- who has been buying ---------------------------------------------------------

/** Daily bars are drawn for windows up to about a year; past that they are
 * thinner than a pixel and the cumulative line carries the story alone. */
const MAX_DAILY_BARS = 400

function WindowStat({ label, s }: { label: string; s: FlowSummary | undefined }) {
  const v = s?.net_individual_toman ?? null
  return (
    <div>
      <div className="field-label">{label}</div>
      <span className={`mono ${pctClass(v)}`}>
        {v === null ? '—' : `${v > 0 ? '+' : ''}${formatTomanScaled(v)}`}
      </span>
      <div className="muted small">
        {s && s.consistent > 0 ? (
          <>
            {formatPct(s.net_individual_pct_of_value, { digits: 1 })} of value · buyer power{' '}
            {s.buyer_power == null ? '—' : s.buyer_power.toFixed(2)}
            {s.excluded ? ` · ${s.excluded} excluded` : ''}
          </>
        ) : (
          'no checked session'
        )}
      </div>
    </div>
  )
}

export function StockFlows({
  symbol,
  period,
  calendar
}: {
  symbol: string
  period: string
  calendar: Calendar
}) {
  const path = `/stocks/${encodeURIComponent(symbol)}/flows?period=${period}`
  const fl = useApi<StockFlowsResponse>(path, [path])
  const data = useHeld(fl)
  const bars = useMemo(
    () =>
      (data?.days ?? [])
        .filter((d) => d.consistent && d.net_individual_toman !== null)
        .map((d) => ({ date: d.date, net: d.net_individual_toman as number })),
    [data]
  )
  const cumulative = data?.cumulative_net_individual ?? []
  const span = spanDaysOf(cumulative)

  return (
    <div className="card" data-testid="sd-flows">
      <div className="card-title">Who has been buying — individuals (حقیقی) against institutions (حقوقی)</div>
      {fl.error && !data ? (
        <p className="muted small">{fl.error}</p>
      ) : !data ? (
        <Loading />
      ) : data.days.length === 0 ? (
        <p className="muted small" data-testid="sd-flows-empty">
          No money-flow session is stored for this share in this window.
        </p>
      ) : (
        <div className={fl.loading ? 'bx-refetch' : undefined}>
          <div className="bx-stats" data-testid="sd-flow-windows">
            <WindowStat label="Last 5 sessions" s={data.windows['5']} />
            <WindowStat label="Last 20 sessions" s={data.windows['20']} />
            <WindowStat label="Last 60 sessions" s={data.windows['60']} />
            <WindowStat label="This window" s={data.summary} />
          </div>
          {bars.length <= MAX_DAILY_BARS ? (
            <>
              <div className="field-label bx-subhead">Net individual flow per session, toman</div>
              <div className="bx-legend" aria-hidden="true">
                <span className="bx-legend-item"><span className="bx-key" style={{ borderTopColor: 'var(--flow-in)' }} /> individuals bought more</span>
                <span className="bx-legend-item"><span className="bx-key" style={{ borderTopColor: 'var(--flow-out)' }} /> individuals sold more</span>
              </div>
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={bars} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
                  <CartesianGrid stroke="var(--border)" vertical={false} />
                  <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} tickFormatter={(d: string) => axisDate(d, calendar, spanDaysOf(bars))} />
                  <YAxis tick={{ fontSize: 11 }} width={64} tickFormatter={(v: number) => formatCompact(v)} />
                  <Tooltip content={datedTip(calendar, (v) => formatTomanScaled(v))} />
                  <ReferenceLine y={0} stroke="var(--muted)" />
                  <Bar dataKey="net" name="Net individual" maxBarSize={24} radius={[2, 2, 0, 0]} isAnimationActive={false}>
                    {bars.map((b) => (
                      <Cell key={b.date} fill={b.net >= 0 ? 'var(--flow-in)' : 'var(--flow-out)'} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </>
          ) : (
            <p className="muted small">
              {formatGrouped(bars.length)} sessions are too many to draw one bar each; the cumulative line below carries
              them.
            </p>
          )}
          <div className="field-label bx-subhead">Cumulative net individual flow over the window, toman</div>
          <ResponsiveContainer width="100%" height={180}>
            <LineChart data={cumulative} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
              <CartesianGrid stroke="var(--border)" vertical={false} />
              <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} tickFormatter={(d: string) => axisDate(d, calendar, span)} />
              <YAxis tick={{ fontSize: 11 }} width={64} tickFormatter={(v: number) => formatCompact(v)} />
              <Tooltip content={datedTip(calendar, (v) => formatTomanScaled(v))} />
              <ReferenceLine y={0} stroke="var(--muted)" />
              <Line type="linear" dataKey="value" name="Cumulative" stroke="var(--series-1)" strokeWidth={2} dot={false} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
          {data.notes.map((n) => (
            <p key={n} className="muted small">
              {n}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}
