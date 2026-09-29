import { useMemo, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from 'recharts'
import { useApi } from '../hooks/useApi'
import { useSettings } from '../lib/settings'
import { datedTip } from '../components/PriceChart'
import Loading from '../components/Loading'
import { formatCompact, formatDate, formatDateTime, formatGrouped, formatPct, pctClass } from '../lib/format'
import {
  axisDate,
  breadthWidths,
  closureRuns,
  formatIndexLevel,
  formatTomanScaled,
  formatUsdScaled,
  indexGroups,
  jalaliYearStart,
  sortIndices,
  spanDaysOf,
  type IndexSortKey
} from '../lib/bourse'
import type {
  BourseBreadthResponse,
  BourseDataAge,
  BourseFlowsResponse,
  BourseHistoryResponse,
  BourseIndexItem,
  BourseIndicesResponse,
  BourseMarketValueResponse,
  BourseOverviewResponse,
  BourseSectorBreadth,
  FlowSummary
} from '../api/types'

/**
 * The Tehran market as a whole: TSETMC's own indices — the all-share, the
 * equal-weighted, every board, segment and sector — the equal- against the
 * cap-weighted index, the market's total value, and who has been buying the
 * roster's shares.
 *
 * WHAT THIS PAGE WILL NOT LET A READER MISTAKE
 *
 *  1. A CLOSED MARKET FOR A CALM ONE. TEDPIX stood at 3,713,955.9 for fifty
 *     sessions from 2026-02-25. The line is flat there because that is what
 *     the exchange published, and the stretch is shaded and named rather than
 *     left to read as a quiet quarter.
 *  2. A CORRECTED VALUE FOR A SERVED ONE. Six indices carry values TSETMC
 *     stores off by exactly a factor of ten (the second-market index since
 *     2026-08-16). The API corrects them and says so; the table marks every
 *     index that needed it, and the chart's notes count the corrected points.
 *  3. A NOMINAL RISE FOR A REAL ONE. The index can be drawn in dollars, in
 *     gold and in constant prices, one click from the points, because in this
 *     economy a doubling index can be a loss.
 *  4. THE ROSTER FOR THE MARKET. The money-flow table covers the nineteen
 *     shares this deployment stores, and its total row says so.
 *
 * Nothing here is computed in the browser beyond formatting: returns,
 * drawdowns, dispersion, breadth and flow all arrive measured from the API.
 */

const TEDPIX = '32097828799138957'

const PERIODS = [
  { key: '1m', label: '1M' },
  { key: '3m', label: '3M' },
  { key: '6m', label: '6M' },
  { key: '1y', label: '1Y' },
  { key: '3y', label: '3Y' },
  { key: '5y', label: '5Y' },
  { key: '10y', label: '10Y' },
  { key: 'max', label: 'Max' }
] as const
type Period = (typeof PERIODS)[number]['key']

const UNITS = [
  { key: 'points', label: 'Points' },
  { key: 'usd', label: 'In dollars' },
  { key: 'gold', label: 'In gold' },
  { key: 'real', label: 'Real (CPI)' }
] as const
type Unit = (typeof UNITS)[number]['key']

function Fa({ children }: { children: string }) {
  return (
    <span className="bidi-fa" lang="fa" dir="rtl">
      {children}
    </span>
  )
}

function Pct({ value, digits = 2 }: { value: number | null | undefined; digits?: number }) {
  return <span className={`mono ${pctClass(value)}`}>{formatPct(value, { digits })}</span>
}

// --- data age -------------------------------------------------------------------

/**
 * The same rule as the screener's notice: shown only when the API's data_age
 * carries a warning, and it does no arithmetic of its own.
 */
export function BourseAgeNotice({
  age,
  calendar
}: {
  age: BourseDataAge | undefined
  calendar: 'jalali' | 'gregorian'
}) {
  if (!age || !age.warning) return null
  return (
    <div className="callout callout-warn" data-testid="bx-data-age" role="status">
      <div>
        This is the newest Tehran market data <strong>stored</strong>, not the newest that
        exists
        {age.newest_trade_date ? (
          <>
            {' '}
            — the last stored session is <strong>{formatDate(age.newest_trade_date, calendar)}</strong>
            {age.age_days !== null ? <>, {formatGrouped(age.age_days)} days ago</> : null}
          </>
        ) : null}
        .
      </div>
      <div className="small">{age.warning}</div>
    </div>
  )
}

// --- the headline row -----------------------------------------------------------

function Tile({
  label,
  value,
  delta,
  hint,
  testId
}: {
  label: string
  value: string
  delta?: number | null
  hint?: ReactNode
  testId?: string
}) {
  return (
    <div className="card bx-tile" data-testid={testId}>
      <div className="field-label">{label}</div>
      <div className="bx-tile-value">{value}</div>
      {delta !== undefined ? <Pct value={delta} /> : null}
      {hint ? <div className="muted small">{hint}</div> : null}
    </div>
  )
}

function HeadlineRow({ overview, calendar }: { overview: BourseOverviewResponse; calendar: 'jalali' | 'gregorian' }) {
  const bourse = overview.snapshots.find((s) => s.market === 'bourse')
  const fara = overview.snapshots.find((s) => s.market === 'farabourse')
  const tradeValue =
    bourse?.trade_value_toman != null || fara?.trade_value_toman != null
      ? (bourse?.trade_value_toman ?? 0) + (fara?.trade_value_toman ?? 0)
      : null
  const marketValue =
    bourse?.market_value_toman != null && fara?.market_value_toman != null
      ? bourse.market_value_toman + fara.market_value_toman
      : null
  const snapAt = bourse?.activity_at ?? fara?.activity_at
  const snapHint = snapAt ? `Live figure as of ${formatDateTime(snapAt, calendar)} Tehran` : undefined
  return (
    <div className="bx-tiles" data-testid="bx-headline">
      {overview.headline.map((h) => (
        <Tile
          key={h.ins_code}
          testId={`bx-tile-${h.ins_code}`}
          label={h.name_en}
          value={formatIndexLevel(h.last?.value)}
          delta={h.change_1d_pct}
          hint={
            h.last
              ? `${formatDate(h.last.date, calendar)} close${h.last_unchanged ? ' · unchanged: market closed' : ''}`
              : h.status
          }
        />
      ))}
      <Tile
        testId="bx-tile-trade-value"
        label="Trade value"
        value={formatTomanScaled(tradeValue)}
        hint={snapHint ? `${snapHint}. Bourse and Farabourse, every instrument.` : 'No snapshot stored.'}
      />
      <Tile
        testId="bx-tile-market-value"
        label="Market value"
        value={formatTomanScaled(marketValue)}
        hint={
          snapHint ? (
            <>
              {snapHint}. 1T toman is one <Fa>همت</Fa>.
            </>
          ) : (
            'No snapshot stored.'
          )
        }
      />
      <Tile
        testId="bx-tile-breadth"
        label="Instruments up"
        value={overview.sector_totals.up_share_pct == null ? '—' : `${overview.sector_totals.up_share_pct.toFixed(1)}%`}
        hint={
          overview.sector_totals.total
            ? `${formatGrouped(overview.sector_totals.up_under_2 + overview.sector_totals.up_over_2)} of ${formatGrouped(
                overview.sector_totals.total
              )} in TSETMC's two "increase" buckets, same instant.`
            : undefined
        }
      />
    </div>
  )
}

// --- the index chart --------------------------------------------------------------

function IndexChartCard({
  code,
  setCode,
  period,
  items,
  calendar
}: {
  code: string
  setCode: (c: string) => void
  period: Period
  items: BourseIndexItem[]
  calendar: 'jalali' | 'gregorian'
}) {
  const [unit, setUnit] = useState<Unit>('points')
  const [log, setLog] = useState(false)
  const path = `/bourse/indices/${code}/history?period=${period}&unit=${unit}`
  const hist = useApi<BourseHistoryResponse>(path, [path])
  // Hold the previous render while the next loads, at reduced opacity, rather
  // than flashing a spinner in place of a chart the reader was looking at.
  const last = useRef<BourseHistoryResponse | null>(null)
  if (hist.data) last.current = hist.data
  const data = hist.data ?? last.current

  const points = useMemo(() => (data ? data.points.map((p) => ({ date: p.date, value: p.value })) : []), [data])
  const closures = useMemo(() => (data && data.unit !== 'real' ? closureRuns(data.points) : []), [data])
  const span = spanDaysOf(points)
  const s = data?.summary
  const rebased = data ? data.unit !== 'points' : false
  const selected = items.find((i) => i.ins_code === code)

  return (
    <div className="card bx-chart-card" data-testid="bx-index-chart">
      <div className="row space-between wrap bx-card-head">
        <div className="card-title">Index</div>
        <label className="bx-picker">
          <span className="sr-only">Index</span>
          <select value={code} onChange={(e) => setCode(e.target.value)} data-testid="bx-index-picker">
            {indexGroups(items).map((g) => (
              <optgroup key={g.label} label={g.label}>
                {g.items.map((i) => (
                  <option key={i.ins_code} value={i.ins_code}>
                    {i.name_en} — {i.name_fa}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
      </div>
      <div className="row wrap bx-controls">
        <div className="chip-row" role="group" aria-label="Unit">
          {UNITS.map((u) => (
            <button
              key={u.key}
              className={`chip ${unit === u.key ? 'active' : ''}`}
              aria-pressed={unit === u.key}
              onClick={() => setUnit(u.key)}
            >
              {u.label}
            </button>
          ))}
        </div>
        <button
          className={`chip ${log ? 'active' : ''}`}
          aria-pressed={log}
          onClick={() => setLog(!log)}
          title="A log scale draws equal percentage moves as equal heights"
        >
          Log scale
        </button>
      </div>

      {hist.error && !data ? (
        <p className="muted" data-testid="bx-index-error">
          {hist.error}
        </p>
      ) : !data ? (
        <Loading />
      ) : (
        <div className={hist.loading ? 'bx-refetch' : undefined}>
          <p className="muted small bx-unit" data-testid="bx-unit-label">
            {selected ? (
              <>
                <Fa>{selected.name_fa}</Fa> ·{' '}
              </>
            ) : null}
            {rebased ? data.unit_label : 'Index points — a level, not a price'}
            {data.unit === 'real' ? ' · one point per month' : ''}
          </p>
          <ResponsiveContainer width="100%" height={340}>
            <LineChart data={points} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
              <CartesianGrid stroke="var(--border)" vertical={false} />
              <XAxis
                dataKey="date"
                tick={{ fontSize: 11 }}
                minTickGap={48}
                tickFormatter={(d: string) => axisDate(d, calendar, span)}
              />
              <YAxis
                tick={{ fontSize: 11 }}
                width={64}
                scale={log ? 'log' : 'auto'}
                domain={['auto', 'auto']}
                tickFormatter={(v: number) => formatCompact(v)}
              />
              <Tooltip content={datedTip(calendar, (v) => (rebased ? v.toFixed(1) : formatIndexLevel(v)))} />
              {closures.map((r) => (
                <ReferenceArea
                  key={r.from}
                  x1={r.from}
                  x2={r.to}
                  fill="var(--muted)"
                  fillOpacity={0.14}
                  strokeOpacity={0}
                  ifOverflow="extendDomain"
                />
              ))}
              {rebased ? <ReferenceLine y={100} stroke="var(--muted)" strokeOpacity={0.6} /> : null}
              <Line
                type="linear"
                dataKey="value"
                stroke="var(--series-1)"
                strokeWidth={2}
                dot={false}
                isAnimationActive={false}
                name={selected?.name_en ?? 'Index'}
              />
            </LineChart>
          </ResponsiveContainer>

          {closures.length > 0 ? (
            <p className="muted small" data-testid="bx-closures">
              Shaded: the index repeated its previous value for{' '}
              {closures
                .map((r) => `${formatGrouped(r.sessions)} sessions from ${formatDate(r.from, calendar)}`)
                .join('; ')}{' '}
              — the market was closed, not calm.
            </p>
          ) : null}

          {s ? (
            <div className="bx-stats" data-testid="bx-index-summary">
              <div>
                <div className="field-label">Change over window</div>
                <Pct value={s.return_pct} />
              </div>
              <div>
                <div className="field-label">Worst fall</div>
                <Pct value={s.max_drawdown.pct} />
                {s.max_drawdown.peak_date ? (
                  <div className="muted small">
                    {formatDate(s.max_drawdown.peak_date, calendar)} → {formatDate(s.max_drawdown.trough_date, calendar)}
                    {s.max_drawdown.recovered_date
                      ? `, recovered ${formatDate(s.max_drawdown.recovered_date, calendar)}`
                      : ', not recovered in this window'}
                  </div>
                ) : null}
              </div>
              <div>
                <div className="field-label">Session dispersion</div>
                <span className="mono">{s.dispersion_pct == null ? '—' : `${s.dispersion_pct.toFixed(2)}%`}</span>
                <div className="muted small">
                  {s.dispersion_pct == null
                    ? s.dispersion_reason
                    : `sd of ${formatGrouped(s.sessions)} session returns, not annualised`}
                </div>
              </div>
              <div>
                <div className="field-label">Closed-market rows</div>
                <span className="mono">{formatGrouped(s.unchanged_sessions)}</span>
              </div>
            </div>
          ) : null}
          {data.deflator ? <p className="muted small">{data.deflator.note}</p> : null}
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

// --- breadth: equal- against cap-weighted -------------------------------------------

function BreadthCard({ period, calendar }: { period: Period; calendar: 'jalali' | 'gregorian' }) {
  const b = useApi<BourseBreadthResponse>(`/bourse/breadth?period=${period}`, [period])
  const last = useRef<BourseBreadthResponse | null>(null)
  if (b.data) last.current = b.data
  const data = b.data ?? last.current
  const span = data ? spanDaysOf(data.points) : 0
  const s = data?.summary
  return (
    <div className="card bx-chart-card" data-testid="bx-breadth">
      <div className="card-title">Breadth — the typical company against the large ones</div>
      {b.error && !data ? (
        <p className="muted">{b.error}</p>
      ) : !data ? (
        <Loading />
      ) : (
        <div className={b.loading ? 'bx-refetch' : undefined}>
          <p className="muted small bx-unit">
            Both rebased to 100 at {formatDate(s?.from ?? null, calendar)}. {data.cap.name_en} weighs a company by
            its market value; the equal-weighted index weighs every company the same.
          </p>
          <div className="bx-legend" aria-hidden="true">
            <span className="bx-legend-item"><span className="bx-key" style={{ borderTopColor: 'var(--series-1)' }} /> {data.cap.name_en}</span>
            <span className="bx-legend-item"><span className="bx-key" style={{ borderTopColor: 'var(--series-2)' }} /> {data.equal.name_en}</span>
          </div>
          <ResponsiveContainer width="100%" height={260}>
            <LineChart data={data.points} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
              <CartesianGrid stroke="var(--border)" vertical={false} />
              <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} tickFormatter={(d: string) => axisDate(d, calendar, span)} />
              <YAxis tick={{ fontSize: 11 }} width={56} domain={['auto', 'auto']} tickFormatter={(v: number) => formatCompact(v)} />
              <Tooltip content={datedTip(calendar, (v) => v.toFixed(1))} />
              <ReferenceLine y={100} stroke="var(--muted)" strokeOpacity={0.6} />
              <Line type="linear" dataKey="cap" name={data.cap.name_en} stroke="var(--series-1)" strokeWidth={2} dot={false} isAnimationActive={false} />
              <Line type="linear" dataKey="equal" name={data.equal.name_en} stroke="var(--series-2)" strokeWidth={2} dot={false} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
          <div className="field-label bx-subhead">Ratio: equal-weighted ÷ cap-weighted × 100</div>
          <ResponsiveContainer width="100%" height={150}>
            <LineChart data={data.points} margin={{ top: 4, right: 16, bottom: 0, left: 8 }}>
              <CartesianGrid stroke="var(--border)" vertical={false} />
              <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} tickFormatter={(d: string) => axisDate(d, calendar, span)} />
              <YAxis tick={{ fontSize: 11 }} width={56} domain={['auto', 'auto']} tickFormatter={(v: number) => v.toFixed(0)} />
              <Tooltip content={datedTip(calendar, (v) => v.toFixed(1))} />
              <ReferenceLine y={100} stroke="var(--muted)" strokeOpacity={0.6} />
              <Line type="linear" dataKey="ratio" name="Ratio" stroke="var(--text)" strokeWidth={1.5} dot={false} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
          {s ? (
            <p className="bx-verdict" data-testid="bx-breadth-summary">
              {data.cap.name_en.split(',')[0]} <Pct value={s.cap_return_pct} />, equal-weighted <Pct value={s.equal_return_pct} />
              : a spread of{' '}
              <span className={`mono bx-nowrap ${pctClass(s.spread_pp)}`}>
                {s.spread_pp == null ? '—' : `${s.spread_pp > 0 ? '+' : ''}${s.spread_pp.toFixed(1)} pp`}
              </span>
              {s.spread_pp != null ? (s.spread_pp < 0 ? ' — the large companies carried it.' : ' — the typical company did better.') : null}{' '}
              The equal-weighted index did better on {formatGrouped(s.equal_beat_cap)} of {formatGrouped(s.sessions_compared)} sessions
              {s.correlation != null ? `; the two move together with a correlation of ${s.correlation.toFixed(2)}` : ''}.
            </p>
          ) : null}
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

// --- the index and sector tables -------------------------------------------------------

const COLUMNS: Array<{ key: IndexSortKey; label: string; title?: string }> = [
  { key: 'change_1d_pct', label: '1D' },
  { key: '1w', label: '1W' },
  { key: '1m', label: '1M' },
  { key: '3m', label: '3M' },
  { key: '1y', label: '1Y' },
  { key: 'since', label: 'YTD', title: 'Since 1 Farvardin, from the last close of the previous Jalali year' },
  { key: 'from_ath', label: 'From high', title: 'Below the all-time high of the stored history' },
  { key: 'dispersion', label: 'Disp. 1Y', title: 'Standard deviation of session returns over the year, not annualised' }
]

function cellValue(item: BourseIndexItem, key: IndexSortKey): number | null {
  switch (key) {
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

function reasonFor(item: BourseIndexItem, key: IndexSortKey): string | undefined {
  if (key === 'since') return item.since_reason
  if (key === 'dispersion') return item.dispersion_1y_reason
  return item.return_reasons?.[key]
}

function BreadthBar({ b }: { b: BourseSectorBreadth }) {
  const [d2, d1, u1, u2] = breadthWidths(b)
  const label = `${b.down_over_2} down >2%, ${b.down_under_2} down <2%, ${b.up_under_2} up <2%, ${b.up_over_2} up >2%`
  return (
    <span className="bx-bbar" title={label} aria-label={label} role="img">
      <span style={{ width: `${d2}%`, background: 'var(--flow-out)' }} />
      <span style={{ width: `${d1}%`, background: 'var(--flow-out)', opacity: 0.45 }} />
      <span style={{ width: `${u1}%`, background: 'var(--flow-in)', opacity: 0.45 }} />
      <span style={{ width: `${u2}%`, background: 'var(--flow-in)' }} />
    </span>
  )
}

function IndexTable({
  items,
  onPick,
  breadth,
  testId,
  calendar
}: {
  items: BourseIndexItem[]
  onPick: (code: string) => void
  breadth?: Map<string, BourseSectorBreadth>
  testId: string
  calendar: 'jalali' | 'gregorian'
}) {
  const [sort, setSort] = useState<{ key: IndexSortKey; desc: boolean }>({ key: 'order', desc: false })
  const rows = useMemo(() => sortIndices(items, sort.key, sort.desc), [items, sort])
  const header = (key: IndexSortKey, label: string, title?: string) => (
    <th
      key={key}
      className="num bx-sortable"
      title={title}
      aria-sort={sort.key === key ? (sort.desc ? 'descending' : 'ascending') : 'none'}
      onClick={() => setSort((s) => ({ key, desc: s.key === key ? !s.desc : true }))}
    >
      {label}
      {sort.key === key ? (sort.desc ? ' ↓' : ' ↑') : ''}
    </th>
  )
  return (
    <div className="table-wrap">
      <table className="table bx-table" data-testid={testId}>
        <thead>
          <tr>
            <th className="bx-sortable" onClick={() => setSort({ key: 'order', desc: false })}>
              Index
            </th>
            <th className="num">Last</th>
            {COLUMNS.map((c) => header(c.key, c.label, c.title))}
            {breadth ? <th>Now</th> : null}
            {breadth ? <th>Roster</th> : null}
          </tr>
        </thead>
        <tbody>
          {rows.map((it) => {
            const b = breadth && it.sector_code ? breadth.get(it.sector_code) : undefined
            const corrected = it.check.rows_rescaled > 0
            return (
              <tr key={it.ins_code} data-testid={`bx-row-${it.ins_code}`}>
                <td className="bx-name">
                  <button className="link-btn" onClick={() => onPick(it.ins_code)} title="Draw this index above">
                    {it.name_en}
                  </button>
                  {corrected ? (
                    <span
                      className="badge badge-info bx-badge"
                      title={`${it.check.rows_rescaled} value(s) stored off by a factor of ten, corrected; checked against TSETMC's live figure (ratio ${
                        it.check.live_ratio?.toFixed(4) ?? 'n/a'
                      })`}
                    >
                      ×10 corrected
                    </span>
                  ) : null}
                  {it.check.status !== 'validated' ? (
                    <span className="badge badge-bad bx-badge" title={it.check.refusal_reason}>
                      {it.check.status}
                    </span>
                  ) : null}
                  {/* The Persian name on its own line keeps this column narrow
                      enough that the figures stay on screen on a phone. */}
                  <div className="muted small">
                    <Fa>{it.name_fa}</Fa>
                  </div>
                </td>
                <td className="num mono" title={it.last ? formatDate(it.last.date, calendar) : undefined}>
                  {formatIndexLevel(it.last?.value)}
                </td>
                {COLUMNS.map((c) => {
                  const v = cellValue(it, c.key)
                  if (c.key === 'dispersion') {
                    return (
                      <td key={c.key} className="num mono" title={reasonFor(it, c.key)}>
                        {v == null ? '—' : `${v.toFixed(2)}%`}
                      </td>
                    )
                  }
                  return (
                    <td key={c.key} className="num" title={v == null ? reasonFor(it, c.key) : undefined}>
                      <Pct value={v} digits={1} />
                    </td>
                  )
                })}
                {breadth ? <td>{b ? <BreadthBar b={b} /> : <span className="muted small">—</span>}</td> : null}
                {breadth ? (
                  <td>
                    {(it.roster_symbols ?? []).map((sym) => (
                      <Link key={sym} className="stk-symbol-link bx-sym" to={`/stocks/${encodeURIComponent(sym)}`}>
                        <Fa>{sym}</Fa>
                      </Link>
                    ))}
                  </td>
                ) : null}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// --- market value -------------------------------------------------------------------

function MarketValueCard({ period, calendar }: { period: Period; calendar: 'jalali' | 'gregorian' }) {
  const [inUsd, setInUsd] = useState(false)
  const mv = useApi<BourseMarketValueResponse>(`/bourse/market-value?period=${period}`, [period])
  const last = useRef<BourseMarketValueResponse | null>(null)
  if (mv.data) last.current = mv.data
  const data = mv.data ?? last.current
  const points = useMemo(
    () =>
      (data?.points ?? [])
        .filter((p) => (inUsd ? p.total_usd != null : p.total_toman != null))
        .map((p) => ({
          date: p.date,
          bourse: inUsd ? p.bourse_usd : p.bourse_toman,
          farabourse: inUsd ? p.farabourse_usd : p.farabourse_toman
        })),
    [data, inUsd]
  )
  const span = spanDaysOf(points)
  const fmt = inUsd ? formatUsdScaled : formatTomanScaled
  const latest = data?.summary.latest
  return (
    <div className="card bx-chart-card" data-testid="bx-market-value">
      <div className="row space-between wrap bx-card-head">
        <div className="card-title">Market value — how big, not how it did</div>
        <div className="chip-row" role="group" aria-label="Currency">
          <button className={`chip ${!inUsd ? 'active' : ''}`} aria-pressed={!inUsd} onClick={() => setInUsd(false)}>
            Toman
          </button>
          <button className={`chip ${inUsd ? 'active' : ''}`} aria-pressed={inUsd} onClick={() => setInUsd(true)}>
            US dollars
          </button>
        </div>
      </div>
      {mv.error && !data ? (
        <p className="muted">{mv.error}</p>
      ) : !data ? (
        <Loading />
      ) : (
        <div className={mv.loading ? 'bx-refetch' : undefined}>
          {latest ? (
            <p className="bx-verdict" data-testid="bx-mv-latest">
              On {formatDate(latest.date, calendar)} the two markets were worth{' '}
              <strong>{formatTomanScaled(latest.total_toman)}</strong> ({formatUsdScaled(latest.total_usd)}):{' '}
              bourse {formatTomanScaled(latest.bourse_toman)}, Farabourse {formatTomanScaled(latest.farabourse_toman)}. Over
              this window the total changed <Pct value={data.summary.total_toman_change_pct} digits={1} /> in toman and{' '}
              <Pct value={data.summary.total_usd_change_pct} digits={1} /> in dollars.
            </p>
          ) : null}
          <div className="bx-legend" aria-hidden="true">
            <span className="bx-legend-item"><span className="bx-key bx-key-area" style={{ borderTopColor: 'var(--series-1)', color: 'var(--series-1)' }} /> Bourse</span>
            <span className="bx-legend-item"><span className="bx-key bx-key-area" style={{ borderTopColor: 'var(--series-3)', color: 'var(--series-3)' }} /> Farabourse</span>
          </div>
          <ResponsiveContainer width="100%" height={260}>
            <AreaChart data={points} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
              <CartesianGrid stroke="var(--border)" vertical={false} />
              <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} tickFormatter={(d: string) => axisDate(d, calendar, span)} />
              <YAxis tick={{ fontSize: 11 }} width={64} tickFormatter={(v: number) => (inUsd ? `$${formatCompact(v)}` : formatCompact(v))} />
              <Tooltip content={datedTip(calendar, (v) => fmt(v))} />
              <Area type="linear" dataKey="bourse" name="Bourse" stackId="mv" stroke="var(--series-1)" strokeWidth={2} fill="var(--series-1)" fillOpacity={0.1} isAnimationActive={false} />
              <Area type="linear" dataKey="farabourse" name="Farabourse" stackId="mv" stroke="var(--series-3)" strokeWidth={2} fill="var(--series-3)" fillOpacity={0.1} isAnimationActive={false} />
            </AreaChart>
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

// --- money flow ---------------------------------------------------------------------

function FlowCell({ s }: { s: FlowSummary | undefined }) {
  const v = s?.net_individual_toman ?? null
  return (
    <td className="num" title={s && s.excluded ? `${s.excluded} session(s) failed a check and are excluded` : undefined}>
      <span className={`mono ${pctClass(v === null ? null : v)}`}>
        {v === null ? '—' : `${v > 0 ? '+' : ''}${formatTomanScaled(v).replace(' toman', '')}`}
      </span>
    </td>
  )
}

function FlowsCard({ calendar }: { calendar: 'jalali' | 'gregorian' }) {
  const f = useApi<BourseFlowsResponse>('/bourse/flows')
  const data = f.data
  return (
    <div className="card" data-testid="bx-flows">
      <div className="card-title">Money flow — individuals (حقیقی) against institutions (حقوقی)</div>
      {f.error ? (
        <p className="muted">{f.error}</p>
      ) : !data ? (
        <Loading />
      ) : (
        <>
          <p className="muted small bx-unit">
            Net individual flow in toman over the newest 1, 5, 20 and 60 stored sessions of each share: positive means
            individuals bought more than they sold. Buyer power is the average individual buy ticket over the average sell
            ticket (20 sessions).
          </p>
          <div className="table-wrap">
            <table className="table bx-table" data-testid="bx-flows-table">
              <thead>
                <tr>
                  <th>Share</th>
                  <th>Last session</th>
                  <th className="num">1</th>
                  <th className="num">5</th>
                  <th className="num">20</th>
                  <th className="num">60</th>
                  <th className="num" title="Net individual flow as a share of traded value, 20 sessions">
                    % of value
                  </th>
                  <th className="num">Buyer power</th>
                  <th className="num" title="Individuals' share of buying, 20 sessions">
                    Indiv. buy
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((it) => {
                  const w20 = it.windows['20']
                  return (
                    <tr key={it.ins_code}>
                      <td>
                        <Link className="stk-symbol-link" to={`/stocks/${encodeURIComponent(it.symbol)}`}>
                          <Fa>{it.symbol}</Fa>
                        </Link>{' '}
                        <span className="muted small">
                          <Fa>{it.sector_fa}</Fa>
                        </span>
                      </td>
                      <td className="small">{formatDate(it.last_date, calendar)}</td>
                      <FlowCell s={it.windows['1']} />
                      <FlowCell s={it.windows['5']} />
                      <FlowCell s={w20} />
                      <FlowCell s={it.windows['60']} />
                      <td className="num">
                        <Pct value={w20?.net_individual_pct_of_value} digits={1} />
                      </td>
                      <td className="num mono">{w20?.buyer_power == null ? '—' : w20.buyer_power.toFixed(2)}</td>
                      <td className="num mono">
                        {w20?.individual_buy_share_pct == null ? '—' : `${w20.individual_buy_share_pct.toFixed(0)}%`}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
              <tfoot>
                <tr className="bx-roster-row" data-testid="bx-flows-roster">
                  <td colSpan={2}>
                    <strong>These {formatGrouped(data.count)} shares together</strong>
                    <div className="muted small">the roster, not the market</div>
                  </td>
                  <FlowCell s={data.roster['1']} />
                  <FlowCell s={data.roster['5']} />
                  <FlowCell s={data.roster['20']} />
                  <FlowCell s={data.roster['60']} />
                  <td className="num">
                    <Pct value={data.roster['20']?.net_individual_pct_of_value} digits={1} />
                  </td>
                  <td className="num mono">
                    {data.roster['20']?.buyer_power == null ? '—' : data.roster['20'].buyer_power.toFixed(2)}
                  </td>
                  <td className="num mono">
                    {data.roster['20']?.individual_buy_share_pct == null
                      ? '—'
                      : `${data.roster['20'].individual_buy_share_pct.toFixed(0)}%`}
                  </td>
                </tr>
              </tfoot>
            </table>
          </div>
          {data.notes.map((n) => (
            <p key={n} className="muted small">
              {n}
            </p>
          ))}
          <p className="small" data-testid="bx-flows-market-link">
            The whole market, sector by sector: <Link to="/money-flow">Money flow</Link>.
          </p>
        </>
      )}
    </div>
  )
}

// --- the page -------------------------------------------------------------------------

export default function Bourse() {
  const { calendar } = useSettings()
  const [period, setPeriod] = useState<Period>('1y')
  const [code, setCode] = useState(TEDPIX)
  const year = useMemo(() => jalaliYearStart(new Date()), [])
  const overview = useApi<BourseOverviewResponse>('/bourse/overview')
  const indices = useApi<BourseIndicesResponse>(`/bourse/indices?since=${year.iso}`, [year.iso])
  const chartRef = useRef<HTMLDivElement | null>(null)

  const items = indices.data?.items ?? []
  const nonSector = items.filter((i) => i.kind !== 'sector')
  const sectors = items.filter((i) => i.kind === 'sector')
  const breadthBySector = useMemo(() => {
    const m = new Map<string, BourseSectorBreadth>()
    for (const s of overview.data?.sectors ?? []) m.set(s.sector_code, s)
    return m
  }, [overview.data])

  const pick = (c: string) => {
    setCode(c)
    chartRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  if (indices.loading && !indices.data) return <Loading />
  if (indices.error && !indices.data) {
    return (
      <div className="page">
        <h2 className="page-title">Tehran market</h2>
        <div className="card error-box">
          <div className="card-title">The market data could not be loaded</div>
          <p className="muted small">{indices.error}</p>
        </div>
      </div>
    )
  }

  return (
    <div className="page bx">
      <div className="row space-between wrap">
        <div>
          <h2 className="page-title">
            Tehran market <span className="muted">·</span> <Fa>بورس تهران</Fa>
          </h2>
          <div className="muted small">
            TSETMC's own indices, corrected where the exchange stores a value off by a factor of ten, with the
            market's value and who has been buying. Descriptions of what happened — not forecasts.
          </div>
        </div>
      </div>

      <BourseAgeNotice age={indices.data?.data_age} calendar={calendar} />

      {overview.data ? <HeadlineRow overview={overview.data} calendar={calendar} /> : null}

      <div className="row wrap bx-filter" role="group" aria-label="Window for the charts below">
        <span className="field-label">Window</span>
        <div className="chip-row">
          {PERIODS.map((p) => (
            <button
              key={p.key}
              className={`chip ${period === p.key ? 'active' : ''}`}
              aria-pressed={period === p.key}
              onClick={() => setPeriod(p.key)}
            >
              {p.label}
            </button>
          ))}
        </div>
        <span className="muted small">Charts end at the newest stored session.</span>
      </div>

      <div ref={chartRef}>
        <IndexChartCard code={code} setCode={setCode} period={period} items={items} calendar={calendar} />
      </div>
      <BreadthCard period={period} calendar={calendar} />

      <div className="card">
        <div className="card-title">Indices</div>
        <IndexTable items={nonSector} onPick={pick} testId="bx-indices-table" calendar={calendar} />
      </div>

      <div className="card">
        <div className="card-title">Sectors</div>
        <p className="muted small bx-unit">
          Every TSETMC sector index. <em>Now</em> is TSETMC's breadth for the sector at the snapshot
          {overview.data?.sectors_at ? ` (${formatDateTime(overview.data.sectors_at, calendar)} Tehran)` : ''}: red is
          down, blue is up, pale is under 2%. <em>Roster</em> links the shares this deployment stores in each sector.
        </p>
        <IndexTable items={sectors} onPick={pick} breadth={breadthBySector} testId="bx-sectors-table" calendar={calendar} />
      </div>

      <MarketValueCard period={period} calendar={calendar} />
      <FlowsCard calendar={calendar} />

      <div className="card">
        <div className="card-title">How these numbers are made</div>
        {(indices.data?.notes ?? []).map((n) => (
          <p key={n} className="muted small">
            {n}
          </p>
        ))}
        {overview.data?.notes.map((n) => (
          <p key={n} className="muted small">
            {n}
          </p>
        ))}
        <p className="muted small">
          YTD is measured from the value in force on 1 Farvardin {year.jy} ({year.iso}).
        </p>
      </div>
    </div>
  )
}
