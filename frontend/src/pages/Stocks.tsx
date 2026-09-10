import { useMemo, useState } from 'react'
import { useApi } from '../hooks/useApi'
import {
  STOCK_NUMERAIRES,
  STOCK_NUMERAIRE_LABELS,
  STOCK_PERIODS,
  STOCK_PERIOD_LABELS,
  type AbsentMetric,
  type StockExcludedItem,
  type StockNumeraire,
  type StockPeriod,
  type StockScreenItem,
  type StockScreenResponse
} from '../api/types'
import { useSettings } from '../lib/settings'
import { compareNullable, compareText, finiteOrNull, type SortDir } from '../lib/markets'
// NOTE the two format helpers that are deliberately NOT imported here:
// `formatToman` and `convertDisplay`. Every amount on this page is a RIAL
// figure the exchange itself quoted, and both of those helpers exist to apply
// the app-wide toman→rial ×10 to a canonical toman value. Applying either one
// here would multiply an already-rial number by ten under a rial heading —
// the same defect as the P1 `formatEndValue` bug, where a converted value was
// formatted with the wrong unit and then converted again.
import { formatCompact, formatDate, formatGrouped, formatPct, formatUsd, pctClass } from '../lib/format'
import Loading from '../components/Loading'
import ErrorMessage from '../components/ErrorMessage'
import EmptyState from '../components/EmptyState'

/**
 * The Tehran equity screener.
 *
 * Everything on it is arithmetic over stored bars, computed once by
 * GET /api/v1/stocks/screen: last price, period return, volatility, maximum
 * drawdown, liquidity, and the last price re-expressed in dollars and in grams
 * of 18k gold. Three properties of that endpoint are what this page is built
 * to render faithfully:
 *
 *  1. RETURNS COME FROM ADJUSTED CLOSES, and every row says so. A screener
 *     sorted on returns from raw Tehran closes ranks companies by how recently
 *     each did a capital increase — فولاد is ×1.52 raw against ×907.86
 *     adjusted over the same nineteen years. The adjustment version and the
 *     number of corporate actions applied are therefore a COLUMN, not a
 *     footnote.
 *  2. THE UNIT IS RIAL. TSETMC quotes rial; the rest of this app reports
 *     Iranian amounts in toman. The top bar's toman/rial toggle does not touch
 *     these figures, and the page says so where a reader will see it.
 *  3. A METRIC THAT COULD NOT BE COMPUTED IS A DASH WITH THE SERVER'S OWN
 *     REASON, never a zero and never an empty cell — and the five columns this
 *     dataset cannot support at all (P/E, EPS growth, ROE, dividend yield,
 *     market cap) are rendered from the response's `absent_metrics`, so the
 *     boundary this page states is the one the backend actually knows.
 *
 * Nothing here is a recommendation. Every figure is a measurement of a window
 * that has already closed.
 */

type ColKey =
  | 'symbol'
  | 'sector'
  | 'last'
  | 'return'
  | 'volatility'
  | 'drawdown'
  | 'turnover'
  | 'volume'
  | 'usd'
  | 'gold'
  | 'adjustment'
  | 'coverage'

interface Column {
  key: ColKey
  label: string
  numeric: boolean
  /** What the header means. The titles are terse; this is not. */
  help: string
  metric?: (item: StockScreenItem) => number | null
}

function buildColumns(numeraire: string, periodLabel: string): Column[] {
  const inUnit = numeraire === 'IRR' ? 'in rials' : numeraire === 'USD' ? 'in USD' : 'in grams of 18k gold'
  return [
    {
      key: 'symbol',
      label: 'Symbol',
      numeric: false,
      help: 'The Persian trading symbol and the company it belongs to.'
    },
    {
      key: 'sector',
      label: 'Sector',
      numeric: false,
      help: "TSETMC's own sector for the instrument."
    },
    {
      key: 'last',
      label: 'Last close (IRR)',
      numeric: true,
      help: "The exchange's RAW official closing price (قیمت پایانی) on the last session that actually traded, in RIALS and unadjusted. It is the one figure on this table that is not adjusted.",
      metric: (i) => finiteOrNull(i.last_close)
    },
    {
      key: 'return',
      label: `Return (${periodLabel})`,
      numeric: true,
      help: `Simple return from the first covered session to the last, computed from ADJUSTED closes and measured ${inUnit}. Raw closes would rank by how recently each company did a capital increase.`,
      metric: (i) => finiteOrNull(i.return_pct)
    },
    {
      key: 'volatility',
      label: 'Volatility',
      numeric: true,
      help: 'Sample standard deviation of session-over-session returns, in percent, NOT ANNUALISED — Tehran trades Saturday to Wednesday and halts are common, so there is no single N for which sqrt(N) would be right.',
      metric: (i) => finiteOrNull(i.volatility_pct)
    },
    {
      key: 'drawdown',
      label: 'Max drawdown',
      numeric: true,
      help: 'Largest peak-to-trough decline over the covered sessions. 0% means the series never closed below a previous peak, which is a measurement and not a missing value.',
      metric: (i) => finiteOrNull(i.max_drawdown_pct)
    },
    {
      key: 'turnover',
      label: 'Turnover / session (IRR)',
      numeric: true,
      help: 'Mean rial turnover per TRADED session in the window. Halted sessions are excluded from the average — averaging their zeros in would report lower turnover for a suspended share than for a thinly traded one.',
      metric: (i) => finiteOrNull(i.avg_value)
    },
    {
      key: 'volume',
      label: 'Shares / session',
      numeric: true,
      help: 'Mean shares traded per TRADED session in the window.',
      metric: (i) => finiteOrNull(i.avg_volume)
    },
    {
      key: 'usd',
      label: 'Price in USD',
      numeric: true,
      help: 'The last raw close re-expressed in US dollars through the numéraire engine, on the session it printed. Reported whatever numéraire the table is measured in. USD_IRT is the free-market proxy, not an official rate.',
      metric: (i) => finiteOrNull(i.price_usd)
    },
    {
      key: 'gold',
      label: 'Price in 18k gold (g)',
      numeric: true,
      help: 'The last raw close re-expressed in grams of 18k gold, on the session it printed. Reported whatever numéraire the table is measured in.',
      metric: (i) => finiteOrNull(i.price_gold_grams)
    },
    {
      key: 'adjustment',
      label: 'Adjustment',
      numeric: false,
      help: 'Which back-adjustment produced the returns in this row, and how many corporate actions it applied. Sorting this column sorts on the action count.',
      metric: (i) => finiteOrNull(i.adjustment.actions_applied)
    },
    {
      key: 'coverage',
      label: 'Sessions',
      numeric: true,
      help: 'Observations the period metrics were computed from, out of the stored sessions inside the window. Halted sessions are stored and are not observations.',
      metric: (i) => finiteOrNull(i.metrics_observations)
    }
  ]
}

/**
 * The one rendering of "not measured": an em-dash carrying the server's own
 * reason. Never an empty cell, never a 0. The same treatment (and the same
 * `.metric-absent` class) the purchasing-power table uses.
 */
function Absent({ reason }: { reason: string }) {
  return (
    <span className="mono metric-absent" title={reason} data-absent="true">
      —
    </span>
  )
}

/** A percentage, or the dash with its reason. */
function Metric({
  value,
  reason,
  tint = true,
  signed = true
}: {
  value: number | null
  reason: string | null
  tint?: boolean
  signed?: boolean
}) {
  if (value === null) return <Absent reason={reasonOrFallback(reason)} />
  return (
    <span className={`mono ${tint ? pctClass(value) : ''}`}>
      {formatPct(value, { sign: signed })}
    </span>
  )
}

/**
 * The endpoint's contract is that a null figure always arrives with a
 * non-empty sibling reason. When one does not, the cell says THAT rather than
 * inventing an explanation of its own.
 */
function reasonOrFallback(reason: string | null | undefined): string {
  const text = (reason ?? '').trim()
  return text.length > 0
    ? text
    : 'The API returned no value and no reason for it, so none is shown here.'
}

/**
 * A rial amount, compacted for a dense column.
 *
 * `formatCompact` stops at billions and a session's turnover on this roster
 * runs into the thousands of billions, so the trillion tier is added here.
 * This is a display abbreviation of a rial figure and NOT a unit conversion:
 * the exact grouped number travels with it in the cell's title.
 */
function formatRialCompact(value: number): string {
  const abs = Math.abs(value)
  if (abs >= 1e12) return `${(value / 1e12).toFixed(abs >= 1e13 ? 0 : 1)}T`
  return formatCompact(value)
}

/** A dollar price that may be a fraction of a cent on a rial-quoted share. */
function formatUsdPrice(value: number): string {
  return Math.abs(value) >= 1 ? formatUsd(value) : formatUsd(value, 6)
}

/** "2025-09-10 → 2026-09-09", or '' when neither end is known. */
function windowText(
  from: string | null | undefined,
  to: string | null | undefined,
  calendar: 'jalali' | 'gregorian'
): string {
  if (!from && !to) return ''
  return `${formatDate(from, calendar)} → ${formatDate(to, calendar)}`
}

/**
 * The absent columns, gathered across rows.
 *
 * `absent_metrics` is published PER ROW, deliberately: a later increment that
 * ingests statements for some symbols and not others shortens the list on
 * those rows without changing the response's shape. So the page counts how
 * many rows carry each entry instead of assuming the list is global, and says
 * so beside the reason.
 */
interface AbsentSummary extends AbsentMetric {
  rows: number
}

function absentSummaries(items: StockScreenItem[]): AbsentSummary[] {
  const index = new Map<string, AbsentSummary>()
  for (const item of items) {
    for (const entry of item.absent_metrics ?? []) {
      const found = index.get(entry.metric)
      if (found) {
        found.rows += 1
        continue
      }
      index.set(entry.metric, { ...entry, rows: 1 })
    }
  }
  return Array.from(index.values())
}

/** The excluded block: the roster minus what could be screened, with why. */
function ExcludedBlock({
  excluded,
  note,
  calendar
}: {
  excluded: StockExcludedItem[]
  note: string
  calendar: 'jalali' | 'gregorian'
}) {
  if (excluded.length === 0) return null
  return (
    <div className="card stk-excluded" data-testid="stk-excluded">
      <div className="card-title">
        Not screened ({excluded.length}
        {excluded.length === 1 ? ' roster symbol' : ' roster symbols'})
      </div>
      <p className="muted small">{note}</p>
      <ul className="stk-excluded-list">
        {excluded.map((row) => (
          <li key={row.ins_code} data-testid={`stk-excluded-${row.symbol}`}>
            <div className="stk-excluded-head">
              <span className="bidi-fa mono" lang="fa" dir="rtl">
                {row.symbol}
              </span>
              <span className="bidi-fa" lang="fa" dir="rtl">
                {row.name_fa}
              </span>
              <span className="badge badge-off">{row.reason_code.replace(/_/g, ' ')}</span>
              <span className="muted small">
                {row.enabled ? 'enabled' : 'disabled'} · {formatGrouped(row.bar_count)} stored bars ·
                adjustment {row.adjustment.status.replace(/_/g, ' ')}
                {row.adjustment.version ? ` (${row.adjustment.version})` : ''}
              </span>
            </div>
            <div className="stk-excluded-reason">{row.reason}</div>
            {row.notes && <div className="muted small stk-excluded-notes">{row.notes}</div>}
            {row.adjustment.refusal_reason && (
              <div className="muted small stk-excluded-notes">{row.adjustment.refusal_reason}</div>
            )}
            {row.adjustment.computed_at && (
              <div className="muted small">
                Verdict computed {formatDate(row.adjustment.computed_at, calendar)}.
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

const DEFAULT_PERIOD: StockPeriod = '1y'
const DEFAULT_NUMERAIRE: StockNumeraire = 'IRR'
/** Go's own default sort for this endpoint (`defaultScreenSort`), so the first
 *  paint is the order the server already put the rows in. */
const DEFAULT_SORT: ColKey = 'turnover'

/**
 * Why a numéraire cannot be offered on this deployment, or null when it can.
 *
 * Derived from the rows rather than declared: when every row reports a null
 * converted price for the same reason, that reason IS the deployment's answer.
 * Nothing is hard-coded, so a deployment that later collects the backing series
 * enables the option with no code change.
 */
export function numeraireUnavailableReason(
  data: StockScreenResponse | null,
  n: StockNumeraire
): string | null {
  if (n === 'IRR' || !data || data.items.length === 0) return null
  const reasons = new Set<string>()
  for (const item of data.items) {
    const value = n === 'USD' ? item.price_usd : item.price_gold_grams
    // One row that converted is proof the deployment can back this numéraire.
    if (value != null) return null
    const why = n === 'USD' ? item.price_usd_reason : item.price_gold_grams_reason
    if (typeof why === 'string' && why !== '') reasons.add(why)
  }
  if (reasons.size === 0) return null
  return [...reasons][0]
}

export default function Stocks() {
  const { calendar } = useSettings()
  const [period, setPeriod] = useState<StockPeriod>(DEFAULT_PERIOD)
  const [numeraire, setNumeraire] = useState<StockNumeraire>(DEFAULT_NUMERAIRE)
  const [sector, setSector] = useState<string>('')
  const [sortKey, setSortKey] = useState<ColKey>(DEFAULT_SORT)
  const [sortDir, setSortDir] = useState<SortDir>('desc')
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [showBasis, setShowBasis] = useState(false)

  const query =
    `/stocks/screen?period=${encodeURIComponent(period)}` +
    `&numeraire=${encodeURIComponent(numeraire)}` +
    (sector ? `&sector=${encodeURIComponent(sector)}` : '')
  const screen = useApi<StockScreenResponse>(query)

  const data = screen.data
  const items = useMemo(() => data?.items ?? [], [data])

  /**
   * The numéraire and window the numbers ON SCREEN were measured in — the
   * server's echo, not the pending selection. While a new request is in flight
   * the previous table is still rendered, and heading those rows with the new
   * choice would put a dollar label over rial digits for as long as the fetch
   * takes.
   */
  const served = data?.numeraire || numeraire
  const servedPeriod = data?.period || period
  const periodLabel = STOCK_PERIOD_LABELS[servedPeriod as StockPeriod] ?? servedPeriod
  const columns = useMemo(() => buildColumns(served, periodLabel), [served, periodLabel])
  const absent = useMemo(() => absentSummaries(items), [items])

  const rows = useMemo(() => {
    const column = columns.find((c) => c.key === sortKey)
    const copy = items.slice()
    if (!column || !column.metric) {
      const text = (i: StockScreenItem) => (sortKey === 'sector' ? i.sector_fa : i.symbol)
      return copy.sort((a, b) => compareText(text(a), text(b), sortDir))
    }
    const metric = column.metric
    return copy.sort((a, b) => compareNullable(metric(a), metric(b), sortDir))
  }, [items, columns, sortKey, sortDir])

  const toggleSort = (key: ColKey) => {
    if (key === sortKey) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
      return
    }
    setSortKey(key)
    // Drawdowns are negative, so "sort by drawdown" means WORST first;
    // descending there would rank the shares that never fell above the ones
    // that halved. Same per-column default direction Go applies.
    setSortDir(key === 'symbol' || key === 'sector' || key === 'drawdown' ? 'asc' : 'desc')
  }

  const ariaSort = (key: ColKey): 'ascending' | 'descending' | 'none' =>
    key === sortKey ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'

  const sectors = data?.sectors ?? []
  /**
   * The size of the WHOLE roster, which `items` alone cannot report once a
   * sector filter is on: `sectors` is built by Go from the unfiltered roster
   * and is echoed on every response, filtered or not.
   */
  const rosterSize = sectors.reduce((total, s) => total + s.instrument_count, 0)
  const warnings = data?.warnings ?? []
  const basis = data?.price_basis
  const totalColumns = columns.length + absent.length

  return (
    <div className="page-body">
      <h2 className="page-title">Stocks</h2>
      <p className="muted small stk-preamble">
        Every enabled instrument in the Tehran roster over one window, measured from adjusted
        closes: what the share last printed, what it returned, how much it moved, how far it fell
        from its own peak, how much of it changed hands, and what one share was worth in dollars
        and in gold. Each figure describes a window that has already closed; a figure that could
        not be computed shows a dash and the API's reason for it, never a zero.
      </p>

      <div className="row wrap stk-controls">
        <div className="field">
          <span className="field-label">Window</span>
          <div className="chip-row" role="group" aria-label="Window">
            {STOCK_PERIODS.map((p) => (
              <button
                key={p}
                type="button"
                className={`chip ${period === p ? 'active' : ''}`}
                aria-pressed={period === p}
                title={STOCK_PERIOD_LABELS[p]}
                onClick={() => setPeriod(p)}
              >
                {p}
              </button>
            ))}
          </div>
        </div>

        <div className="field">
          <label htmlFor="stk-numeraire">Measured in</label>
          <select
            id="stk-numeraire"
            value={numeraire}
            onChange={(e) => setNumeraire(e.target.value as StockNumeraire)}
          >
            {/* Offered against what THIS deployment can actually back, the way
                the sector filter beside it already works and the way Markets.tsx
                gates its own menu.
                
                A hard-coded list looked harmless and was not: on a deployment
                with no IR_GOLD_18K series, picking "grams of gold" 400s, useApi
                nulls `data`, and the page loses the table AND the excluded-symbol
                block -- so کچاد and its reason disappear along with everything
                else. The payload already knows: every row carries
                price_gold_grams null with a reason when the backing series is
                absent, so the option is disabled and says why instead of
                erasing the page. */}
            {STOCK_NUMERAIRES.map((n) => {
              const why = numeraireUnavailableReason(data, n)
              return (
                <option key={n} value={n} disabled={why !== null} title={why ?? undefined}>
                  {STOCK_NUMERAIRE_LABELS[n]}
                  {why ? ' — unavailable' : ''}
                </option>
              )
            })}
          </select>
        </div>

        <div className="field">
          <label htmlFor="stk-sector">Sector</label>
          {/* The vocabulary comes from the roster the server actually carries,
              not from a hard-coded list a new listing would silently
              invalidate. Its values are TSETMC sector CODES — the endpoint
              refuses a Persian name. */}
          <select id="stk-sector" value={sector} onChange={(e) => setSector(e.target.value)}>
            <option value="">All sectors{rosterSize > 0 ? ` (${rosterSize})` : ''}</option>
            {sectors.map((s) => (
              <option key={s.sector_code} value={s.sector_code}>
                {s.sector_fa} ({s.instrument_count})
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* The unit, stated where a reader will see it and taken from the
          response rather than assumed. The top bar's toman/rial toggle is an
          app-wide display setting over TOMAN-canonical values; these are rial
          figures the exchange quoted, and nothing on this page multiplies
          them. */}
      <div className="callout callout-info stk-currency" data-testid="stk-currency">
        Amounts on this page are in <strong>{data?.currency ?? 'IRR'}</strong> — rials, the unit
        TSETMC quotes — and the top bar's toman/rial toggle does not apply to them.
        {data?.currency_note ? <div className="muted small">{data.currency_note}</div> : null}
      </div>

      {screen.error && <ErrorMessage message={screen.error} onRetry={screen.reload} />}

      {warnings.map((w, i) => (
        <div key={i} className="callout callout-warn">
          {w}
        </div>
      ))}

      {screen.loading && !data ? (
        <Loading label="Loading the screener…" />
      ) : !data ? null : items.length === 0 ? (
        <EmptyState
          title="No instrument could be screened over this window"
          hint={`${data.excluded_count} roster symbol(s) are listed below with the reason each was left out.`}
        >
          <ExcludedBlock
            excluded={data.excluded}
            note={data.excluded_note}
            calendar={calendar}
          />
        </EmptyState>
      ) : (
        <>
          <div className="card">
            <div className="row space-between wrap">
              <div className="card-title">
                {/* items + excluded is the universe the request asked for:
                    under a sector filter the server excludes only within that
                    sector, so this count is the filtered universe and says so
                    rather than implying the whole roster. */}
                {periodLabel} · measured in {served} · {data.count} of{' '}
                {data.count + data.excluded_count}{' '}
                {data.sector ? 'symbols in this sector' : 'roster symbols'}
              </div>
              <div className="muted small">
                {data.from ? formatDate(data.from, calendar) : 'each instrument’s own first bar'} →{' '}
                {formatDate(data.to, calendar)}
                {data.as_of ? ` · as of ${formatDate(data.as_of, calendar)}` : ''}
              </div>
            </div>

            {/* The adjustment, said once at the top of the table as well as on
                every row: a screener sorted on returns from unadjusted Tehran
                closes ranks companies by how recently they did a capital
                increase. */}
            <div className="muted small stk-adjusted-note" data-testid="stk-adjusted-note">
              Returns, volatility and drawdown are computed from{' '}
              <strong>adjusted closes</strong>; the Adjustment column carries each instrument's own
              version and the number of corporate actions applied to it. Only the last close is
              raw.
              {data.numeraire_series?.series ? (
                <>
                  {' '}
                  Conversion into {served} runs through{' '}
                  <span className="mono">{data.numeraire_series.series}</span>
                  {(data.numeraire_series.notes ?? []).length > 0
                    ? ` — ${(data.numeraire_series.notes ?? []).join(' ')}`
                    : '.'}
                </>
              ) : null}
              <button
                type="button"
                className="btn btn-ghost btn-sm stk-basis-toggle"
                aria-expanded={showBasis}
                onClick={() => setShowBasis((v) => !v)}
              >
                {showBasis ? 'Hide how these are measured' : 'How these are measured'}
              </button>
            </div>

            {showBasis && basis && (
              <ul className="stk-basis-list" data-testid="stk-basis">
                <li>
                  <strong>Currency</strong>: {basis.currency}; closes are taken from{' '}
                  <span className="mono">{basis.close_field}</span>. Last close is{' '}
                  {basis.last_close_adjusted ? 'adjusted' : 'raw'}; returns are{' '}
                  {basis.returns_adjusted ? 'adjusted' : 'raw'}.
                </li>
                <li>
                  <strong>Return</strong>: {basis.return_basis}
                </li>
                <li>
                  <strong>Volatility</strong>: {basis.volatility_basis}
                </li>
                <li>
                  <strong>Drawdown</strong>: {basis.drawdown_basis}
                </li>
                <li>
                  <strong>Liquidity</strong>: {basis.liquidity_basis}
                </li>
                <li>
                  <strong>Sessions</strong>: {basis.session_basis}
                </li>
              </ul>
            )}

            <div className="table-wrap">
              <table className="table stk-table">
                <thead>
                  <tr>
                    {columns.map((col) => (
                      <th
                        key={col.key}
                        className={col.numeric ? 'num' : undefined}
                        aria-sort={ariaSort(col.key)}
                        scope="col"
                      >
                        <button
                          type="button"
                          className="th-sort"
                          onClick={() => toggleSort(col.key)}
                          title={col.help}
                        >
                          {col.label}
                          <span className="th-sort-mark" aria-hidden="true">
                            {sortKey === col.key ? (sortDir === 'asc' ? '▲' : '▼') : '↕'}
                          </span>
                        </button>
                      </th>
                    ))}
                    {/* The columns this dataset cannot support. They are
                        columns and not an omission: a reader scanning for a
                        P/E must find the heading and its reason, not a gap
                        they will fill in with an assumption. They carry no
                        sort control — there is nothing to order. */}
                    {absent.map((entry) => (
                      <th
                        key={entry.metric}
                        className="num stk-col-absent"
                        scope="col"
                        title={entry.reason}
                        data-testid={`stk-absent-col-${entry.metric}`}
                      >
                        {entry.label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((item) => {
                    const isOpen = expanded[item.ins_code] === true
                    const notes = item.notes ?? []
                    const lastClose = finiteOrNull(item.last_close)
                    const turnover = finiteOrNull(item.avg_value)
                    const volume = finiteOrNull(item.avg_volume)
                    const usd = finiteOrNull(item.price_usd)
                    const gold = finiteOrNull(item.price_gold_grams)
                    const metricsWindow = windowText(item.metrics_from, item.metrics_to, calendar)
                    return [
                      <tr key={item.ins_code} data-testid={`stk-row-${item.symbol}`}>
                        <td>
                          <div className="stk-symbol">
                            {/* Persian symbol and company name, each isolated:
                                without bidi isolation an RTL name beside LTR
                                digits reorders the whole cell. */}
                            <span className="bidi-fa mono stk-symbol-code" lang="fa" dir="rtl">
                              {item.symbol}
                            </span>
                            <span className="bidi-fa stk-symbol-name" lang="fa" dir="rtl">
                              {item.name_fa}
                            </span>
                          </div>
                          <div className="stk-symbol-meta muted small">
                            <span className="mono">{item.ins_code}</span>
                            <span>{item.market}</span>
                            {notes.length > 0 && (
                              <button
                                type="button"
                                className="btn btn-ghost btn-sm stk-why"
                                aria-expanded={isOpen}
                                onClick={() =>
                                  setExpanded((prev) => ({ ...prev, [item.ins_code]: !isOpen }))
                                }
                              >
                                {isOpen ? 'Hide notes' : `Why? (${notes.length})`}
                              </button>
                            )}
                          </div>
                        </td>
                        <td>
                          <span className="bidi-fa" lang="fa" dir="rtl">
                            {item.sector_fa}
                          </span>
                          <div className="muted small mono">{item.sector_code}</div>
                        </td>
                        <td
                          className="num"
                          title={item.last_close_basis}
                          data-testid={`stk-last-${item.symbol}`}
                        >
                          {lastClose === null ? (
                            <Absent reason={reasonOrFallback(item.last_close_basis)} />
                          ) : (
                            <>
                              <span className="mono">{formatGrouped(lastClose)}</span>
                              <div className="muted small">
                                {item.last_trade_date
                                  ? formatDate(item.last_trade_date, calendar)
                                  : 'no traded session'}
                              </div>
                            </>
                          )}
                        </td>
                        <td
                          className="num"
                          title={metricsWindow ? `Measured over ${metricsWindow}.` : undefined}
                          data-testid={`stk-return-${item.symbol}`}
                        >
                          <Metric value={finiteOrNull(item.return_pct)} reason={item.return_reason} />
                        </td>
                        <td className="num" data-testid={`stk-volatility-${item.symbol}`}>
                          {/* A dispersion has no direction: tinting it green
                              above zero would read as approval. */}
                          <Metric
                            value={finiteOrNull(item.volatility_pct)}
                            reason={item.volatility_reason}
                            tint={false}
                            signed={false}
                          />
                        </td>
                        <td className="num" data-testid={`stk-drawdown-${item.symbol}`}>
                          <Metric
                            value={finiteOrNull(item.max_drawdown_pct)}
                            reason={item.max_drawdown_reason}
                            tint={false}
                          />
                        </td>
                        <td
                          className="num"
                          data-testid={`stk-turnover-${item.symbol}`}
                          title={
                            turnover === null
                              ? undefined
                              : `${formatGrouped(turnover)} rials per traded session, over ${item.traded_sessions} traded session(s).`
                          }
                        >
                          {turnover === null ? (
                            <Absent reason={reasonOrFallback(item.liquidity_reason)} />
                          ) : (
                            <span className="mono">{formatRialCompact(turnover)}</span>
                          )}
                        </td>
                        <td className="num">
                          {volume === null ? (
                            <Absent reason={reasonOrFallback(item.liquidity_reason)} />
                          ) : (
                            <span className="mono" title={`${formatGrouped(volume)} shares per traded session.`}>
                              {formatCompact(volume)}
                            </span>
                          )}
                        </td>
                        <td className="num" data-testid={`stk-usd-${item.symbol}`}>
                          {usd === null ? (
                            <Absent reason={reasonOrFallback(item.price_usd_reason)} />
                          ) : (
                            <>
                              <span className="mono">{formatUsdPrice(usd)}</span>
                              {item.price_usd_carried_forward && item.price_usd_rate_date && (
                                <div
                                  className="muted small stk-carried"
                                  title="The dollar rate in force on this session was carried forward from an earlier day; nothing was interpolated."
                                >
                                  rate {formatDate(item.price_usd_rate_date, calendar)}
                                </div>
                              )}
                            </>
                          )}
                        </td>
                        <td className="num" data-testid={`stk-gold-${item.symbol}`}>
                          {gold === null ? (
                            <Absent reason={reasonOrFallback(item.price_gold_grams_reason)} />
                          ) : (
                            <>
                              <span className="mono">{formatGrouped(gold, 6)}</span>
                              {item.price_gold_grams_carried_forward &&
                                item.price_gold_grams_rate_date && (
                                  <div
                                    className="muted small stk-carried"
                                    title="The gold quote in force on this session was carried forward from an earlier day; nothing was interpolated."
                                  >
                                    rate {formatDate(item.price_gold_grams_rate_date, calendar)}
                                  </div>
                                )}
                            </>
                          )}
                        </td>
                        {/* The adjustment, on every row. Version AND action
                            count, both visible rather than in a title: they
                            are what makes the return column readable. */}
                        <td className="stk-adj" data-testid={`stk-adjustment-${item.symbol}`}>
                          <span className="mono small stk-adj-version">
                            {item.adjustment.version || item.adjustment.status.replace(/_/g, ' ')}
                          </span>
                          <span className="muted small">
                            {formatGrouped(item.adjustment.actions_applied)} action
                            {item.adjustment.actions_applied === 1 ? '' : 's'}
                          </span>
                        </td>
                        <td
                          className="num"
                          title={`${item.traded_sessions} traded and ${item.halted_sessions} halted of ${item.sessions_in_period} stored sessions in this window; ${formatGrouped(item.bar_count)} bars stored in total.`}
                        >
                          <span className="mono">{formatGrouped(item.metrics_observations)}</span>
                          <div className="muted small">of {formatGrouped(item.sessions_in_period)}</div>
                        </td>
                        {absent.map((entry) => (
                          <td
                            key={entry.metric}
                            className="num stk-col-absent"
                            data-testid={`stk-absent-${item.symbol}-${entry.metric}`}
                          >
                            <Absent reason={`${entry.label}: ${entry.reason}`} />
                          </td>
                        ))}
                      </tr>,
                      isOpen ? (
                        <tr key={`${item.ins_code}-notes`} className="stk-note-row">
                          <td colSpan={totalColumns}>
                            <div className="stk-note-window">
                              {metricsWindow
                                ? `Metrics measured over ${metricsWindow} · ${item.metrics_observations} traded session(s) in ${item.metrics_numeraire}`
                                : 'No session in this window carries a measurable close.'}
                              {item.first_bar
                                ? ` · stored history from ${formatDate(item.first_bar, calendar)}`
                                : ''}
                              {item.adjustment.adjusted_first_bar
                                ? ` · adjusted series begins ${formatDate(item.adjustment.adjusted_first_bar, calendar)}`
                                : ''}
                              {item.adjustment.pre_listing_bars
                                ? ` (${formatGrouped(item.adjustment.pre_listing_bars)} pre-listing placeholder bars excluded)`
                                : ''}
                            </div>
                            <div className="stk-note-window muted small">
                              {/* `?? 0` here printed "0 shares in 0 trades" for an
                                  instrument whose every stored bar is a halt or a
                                  pre-listing placeholder -- 9,521 of the 77,344
                                  stored bars are halts, and نوری carried 161 before
                                  its first trade. Zero shares in zero trades is a
                                  measurement of a session that never happened, and
                                  it is exactly the null-as-zero rule the rest of
                                  this page enforces. last_value on this same line
                                  was already handled correctly. */}
                              {item.last_volume === null && item.last_trade_count === null ? (
                                <>No traded session is stored for this instrument.</>
                              ) : (
                                <>
                                  Last session:{' '}
                                  {item.last_volume === null
                                    ? 'volume not reported'
                                    : `${formatGrouped(item.last_volume)} shares`}{' '}
                                  in{' '}
                                  {item.last_trade_count === null
                                    ? 'an unreported number of trades'
                                    : `${formatGrouped(item.last_trade_count)} trades`}
                                  ,{' '}
                                  {item.last_value === null
                                    ? 'turnover not reported'
                                    : `${formatGrouped(item.last_value)} rials of turnover`}
                                  .
                                </>
                              )}
                            </div>
                            {item.conversion && (
                              <div className="stk-note-window muted small">
                                Converted through{' '}
                                <span className="mono">
                                  {item.conversion.chain.join(' → ') || 'no chain'}
                                </span>
                                : {item.conversion.carried_forward_days} session(s) priced with a
                                carried-forward quote, {item.conversion.dropped_no_prior_quote}{' '}
                                dropped with no quote at or before them,{' '}
                                {item.conversion.dropped_non_positive_quote} dropped on a
                                non-positive quote.
                              </div>
                            )}
                            <ul className="stk-note-list">
                              {notes.map((n, i) => (
                                <li key={i}>{n}</li>
                              ))}
                            </ul>
                          </td>
                        </tr>
                      ) : null
                    ]
                  })}
                </tbody>
              </table>
            </div>

            <div className="muted small stk-footer">
              Sorting keeps un-measured rows at the bottom in both directions: a dash is not a
              smallest value. Liquidity is averaged over traded sessions only, and volatility is
              per session and not annualised.
            </div>
          </div>

          {/* The boundary, in the API's own words. */}
          {absent.length > 0 && (
            <div className="card stk-absent-card" data-testid="stk-absent-metrics">
              <div className="card-title">What this screen cannot show, and why</div>
              <p className="muted small">
                These are the columns a reader expects on a screener and will not find here. Each
                reason below is the one the API published beside the row it belongs to — this page
                holds no copy of its own about the boundary, so it cannot drift from what the
                backend actually knows.
              </p>
              <ul className="stk-absent-list">
                {absent.map((entry) => (
                  <li key={entry.metric} data-testid={`stk-absent-reason-${entry.metric}`}>
                    <div className="stk-absent-head">
                      <span className="mono metric-absent" data-absent="true" aria-hidden="true">
                        —
                      </span>
                      <strong>{entry.label}</strong>
                      <span className="mono muted small">{entry.metric}</span>
                      <span className="muted small">
                        absent on {entry.rows} of {items.length} rows
                      </span>
                    </div>
                    <div className="stk-absent-reason">{entry.reason}</div>
                    <div className="muted small">Requires: {entry.requires}</div>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <ExcludedBlock
            excluded={data.excluded}
            note={data.excluded_note}
            calendar={calendar}
          />
        </>
      )}
    </div>
  )
}
