import { useMemo, useState } from 'react'
import { useApi } from '../hooks/useApi'
import {
  MARKET_PERIODS,
  MARKET_PERIOD_LABELS,
  type MarketPerformanceItem,
  type MarketPerformanceResponse,
  type MarketPeriod,
  type NumeraireOption,
  type NumerairesResponse
} from '../api/types'
import { unwrapList } from '../lib/unwrap'
import { useSettings } from '../lib/settings'
import {
  compareNullable,
  compareText,
  finiteOrNull,
  noteText,
  numeraireLabel,
  numeraireLabelFa,
  offeredNumeraires,
  rankBy,
  resolveNumeraire,
  valueUnitFor,
  withheldNumeraires,
  type SortDir,
  type ValueUnit
} from '../lib/markets'
import {
  currencyLabel,
  formatDate,
  formatGrouped,
  formatPct,
  formatToman,
  formatUsd,
  pctClass,
  type DisplayUnit
} from '../lib/format'
import Provenance from '../components/Provenance'
import Loading from '../components/Loading'
import ErrorMessage from '../components/ErrorMessage'
import EmptyState from '../components/EmptyState'

type ColKey =
  | 'asset'
  | 'value'
  | 'nominal'
  | 'real'
  | 'usd'
  | 'gold'
  | 'volatility'
  | 'drawdown'
  | 'observations'

interface Column {
  key: ColKey
  label: string
  /** Right-aligned, bidi-isolated numeric column. */
  numeric: boolean
  /** What the header means, spelled out — the column titles are terse. */
  help: string
  metric?: (item: MarketPerformanceItem) => number | null
}

/**
 * Sample stdev of log returns between consecutive OBSERVATIONS.
 *
 * Go published this as `daily_volatility_pct` and renamed it to
 * `observation_volatility_pct`, because these symbols do not quote every day
 * and most steps span more than one. Both spellings are read: on either side
 * of that rename the column shows the number instead of a page of dashes, and
 * a dash here goes on meaning "not measured".
 */
function volatilityOf(item: MarketPerformanceItem): number | null {
  return finiteOrNull(item.observation_volatility_pct) ?? finiteOrNull(item.daily_volatility_pct)
}

/**
 * The table's columns. `valueUnit` is a parameter because the Value column is
 * denominated in the SELECTED numéraire — the heading has to say which, or the
 * digits are unreadable.
 */
function buildColumns(valueUnit: ValueUnit): Column[] {
  return [
    {
      key: 'asset',
      label: 'Asset',
      numeric: false,
      help: 'Instrument, its source tier and its own quote currency.'
    },
    {
      key: 'value',
      label: `Value (${valueUnit.short})`,
      numeric: true,
      help: `Latest observation in the window, converted into the selected numéraire — ${valueUnit.long}. It is NOT in the instrument's own quote currency.`,
      metric: (i) => finiteOrNull(i.end_value)
    },
    {
      key: 'nominal',
      label: 'Nominal',
      numeric: true,
      help: 'Return over the window measured in the selected numéraire, undeflated.',
      metric: (i) => finiteOrNull(i.nominal_return_pct)
    },
    {
      key: 'real',
      label: 'Real (CPI)',
      numeric: true,
      help: "The nominal return above, deflated by Iran's CPI. Its window can be narrower than the one requested — the cell says which.",
      metric: (i) => finiteOrNull(i.real_return_pct)
    },
    {
      key: 'usd',
      label: 'In USD',
      numeric: true,
      help: 'The value re-expressed in USD before the return is taken. Reported whatever the selected numéraire is, and over its own window.',
      metric: (i) => finiteOrNull(i.usd_return_pct)
    },
    {
      key: 'gold',
      label: 'In gold',
      numeric: true,
      help: 'The value re-expressed in grams of 18k gold before the return is taken. Reported whatever the selected numéraire is, and over its own window.',
      metric: (i) => finiteOrNull(i.gold_return_pct)
    },
    {
      key: 'volatility',
      label: 'Obs vol',
      numeric: true,
      help: 'Standard deviation of log returns between consecutive observations — these symbols do not quote every day. Not annualised.',
      metric: volatilityOf
    },
    {
      key: 'drawdown',
      label: 'Max drawdown',
      numeric: true,
      help: 'Largest peak-to-trough fall inside the window.',
      metric: (i) => finiteOrNull(i.max_drawdown_pct)
    },
    {
      key: 'observations',
      label: 'Obs',
      numeric: true,
      help: 'Observations the metrics were computed from.',
      metric: (i) => finiteOrNull(i.observations)
    }
  ]
}

/**
 * Last-resort numéraire: Go's own `defaultNumeraire`, the identity conversion,
 * the one key that can never become unavailable. It is used only before the
 * registry has answered — once it has, `response.default` is what decides.
 */
const DEFAULT_NUMERAIRE = 'IRT'

/**
 * A metric that may be absent. The em-dash carries the backend's own reason in
 * its title; it is never a 0, never blank, and no value is invented to fill
 * the cell.
 */
function Metric({
  value,
  reason,
  tint = true,
  signed = true
}: {
  value: number | null
  reason: string
  tint?: boolean
  /** A dispersion has no direction: a leading '+' on it reads as a gain. */
  signed?: boolean
}) {
  if (value === null) return <Absent reason={reason} />
  return (
    <span className={`mono ${tint ? pctClass(value) : ''}`}>{formatPct(value, { sign: signed })}</span>
  )
}

/** The one rendering of "not measured". Never an empty cell. */
function Absent({ reason }: { reason: string }) {
  return (
    <span className="mono metric-absent" title={reason} data-absent="true">
      —
    </span>
  )
}

function absentReason(item: MarketPerformanceItem, metric: string): string {
  const notes = noteText(item.notes)
  const head = `${metric} is not reported for ${item.code} over this window.`
  return notes ? `${head} ${notes}` : head
}

/**
 * The Value column.
 *
 * `end_value` is the close of the series AFTER conversion into the selected
 * numéraire, so the unit is the numéraire's and the instrument's own
 * `quote_currency` says nothing about it. Formatting it by quote_currency put
 * toman digits under a dollar heading, and then the display-unit toggle
 * multiplied them by ten — a rial conversion applied to a number that was
 * never toman. The ×10 fires here only where `rialToggleApplies` says the
 * value really is toman.
 */
function formatEndValue(
  item: MarketPerformanceItem,
  valueUnit: ValueUnit,
  unit: DisplayUnit
): string | null {
  const value = finiteOrNull(item.end_value)
  if (value === null) return null
  switch (valueUnit.kind) {
    case 'toman':
      return formatToman(value, unit, false)
    case 'usd':
      return formatUsd(value)
    case 'gold':
      // A count of grams, often well under one: rounding it to a whole number
      // would print 0 for a real holding.
      return formatGrouped(value, 3)
    default:
      return formatGrouped(value, 2)
  }
}

/** "2024-01-01 → 2025-01-01", or '' when neither end is known. */
function windowText(
  from: string | null | undefined,
  to: string | null | undefined,
  calendar: 'jalali' | 'gregorian'
): string {
  if (!from && !to) return ''
  return `${formatDate(from, calendar)} → ${formatDate(to, calendar)}`
}

export default function Markets() {
  const { unit, calendar } = useSettings()
  const [period, setPeriod] = useState<MarketPeriod>('1y')
  const [chosen, setChosen] = useState<string>(DEFAULT_NUMERAIRE)
  const [sortKey, setSortKey] = useState<ColKey>('real')
  const [sortDir, setSortDir] = useState<SortDir>('desc')
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  // Which numéraires this deployment can actually back. Offering one it cannot
  // would fill every re-expressed column with dashes.
  const numeraires = useApi<NumerairesResponse>('/markets/numeraires')
  const registry = useMemo(
    () => unwrapList<NumeraireOption>(numeraires.data, 'items', 'numeraires'),
    [numeraires.data]
  )
  const offered = useMemo(() => offeredNumeraires(registry), [registry])
  const withheld = useMemo(() => withheldNumeraires(registry), [registry])

  /**
   * The numéraire actually requested. DERIVED, not synced by an effect: an
   * effect that reconciles the choice against the registry can only correct
   * itself one render late, which is how a request for `numeraire=undefined`
   * escaped in the first place. `resolveNumeraire` cannot return undefined.
   */
  const selected = useMemo(
    () => resolveNumeraire(chosen, offered, numeraires.data?.default, DEFAULT_NUMERAIRE),
    [chosen, offered, numeraires.data]
  )

  const perf = useApi<MarketPerformanceResponse>(
    `/markets/performance?period=${encodeURIComponent(period)}&numeraire=${encodeURIComponent(selected)}`
  )

  const data = perf.data
  const items = useMemo(() => data?.items ?? [], [data])

  /**
   * The numéraire the numbers ON SCREEN are in — the server's echo, not the
   * request. While a new request is in flight the previous table is still
   * rendered, and labelling those rows with the pending choice would put a
   * gram heading over toman digits for exactly as long as the fetch takes.
   */
  const served = data?.numeraire || selected
  const servedOption = useMemo(
    () => data?.numeraire_series ?? offered.find((n) => n.key === served) ?? null,
    [data, offered, served]
  )
  const valueUnit = useMemo(
    () => valueUnitFor(served, currencyLabel(unit), unit === 'IRR', servedOption?.unit),
    [served, unit, servedOption]
  )
  const columns = useMemo(() => buildColumns(valueUnit), [valueUnit])

  const rows = useMemo(() => {
    const column = columns.find((c) => c.key === sortKey)
    const copy = items.slice()
    if (!column || column.key === 'asset' || !column.metric) {
      return copy.sort((a, b) => compareText(a.name_en ?? a.code, b.name_en ?? b.code, sortDir))
    }
    const metric = column.metric
    return copy.sort((a, b) => compareNullable(metric(a), metric(b), sortDir))
  }, [items, columns, sortKey, sortDir])

  const bestReal = rankBy(items, (i) => finiteOrNull(i.real_return_pct), 'highest')
  const worstReal = rankBy(items, (i) => finiteOrNull(i.real_return_pct), 'lowest')
  const deepestDrawdown = rankBy(items, (i) => finiteOrNull(i.max_drawdown_pct), 'lowest')

  const toggleSort = (key: ColKey) => {
    if (key === sortKey) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
      return
    }
    setSortKey(key)
    setSortDir(key === 'asset' ? 'asc' : 'desc')
  }

  const ariaSort = (key: ColKey): 'ascending' | 'descending' | 'none' =>
    key === sortKey ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'

  const warnings = data?.warnings ?? []
  const selectedOption = offered.find((n) => n.key === selected) ?? null

  return (
    <div className="page-body">
      <h2 className="page-title">Purchasing power</h2>
      <p className="muted small mkt-preamble">
        What each asset did over the selected window, measured four ways: in the numéraire you
        choose, that same return deflated by CPI, re-expressed in USD, and re-expressed in grams of
        18k gold. A metric that was not measured shows a dash and its reason — never a zero.
      </p>

      <div className="row wrap mkt-controls">
        <div className="chip-row" role="group" aria-label="Window">
          {MARKET_PERIODS.map((p) => (
            <button
              key={p}
              type="button"
              className={`chip ${period === p ? 'active' : ''}`}
              aria-pressed={period === p}
              title={MARKET_PERIOD_LABELS[p]}
              onClick={() => setPeriod(p)}
            >
              {p}
            </button>
          ))}
        </div>
        <div className="field mkt-numeraire">
          <label htmlFor="mkt-numeraire">Numéraire</label>
          {offered.length > 0 ? (
            <select
              id="mkt-numeraire"
              value={selected}
              onChange={(e) => setChosen(e.target.value)}
            >
              {/* Every option carries an explicit value. Without one the option
                  value falls back to its own visible text and the page asks for
                  numeraire=US%20dollar. */}
              {offered.map((n) => (
                <option key={n.key} value={n.key}>
                  {numeraireLabel(n)}
                </option>
              ))}
              {/* Listed and unpickable, rather than absent: an operator looking
                  for a missing option must find it, with its reason, not find
                  nothing at all. */}
              {withheld.map((n) => (
                <option key={n.key} value={n.key} disabled>
                  {numeraireLabel(n)} — unavailable here
                </option>
              ))}
            </select>
          ) : (
            <select id="mkt-numeraire" value={selected} disabled>
              <option value={selected}>{selected}</option>
            </select>
          )}
        </div>
      </div>

      {numeraires.error && (
        <div className="callout callout-warn">
          The numéraire registry did not answer ({numeraires.error}), so only{' '}
          <span className="mono">{selected}</span> is offered. Others are withheld rather than
          offered and returned empty.
        </div>
      )}

      {selectedOption && (
        <div className="muted small mkt-coverage">
          <div>
            <span className="mono">{selectedOption.key}</span> — {numeraireLabel(selectedOption)}
            {numeraireLabelFa(selectedOption) ? (
              <>
                {' '}
                <span className="bidi-fa" lang="fa" dir="rtl">
                  {numeraireLabelFa(selectedOption)}
                </span>
              </>
            ) : null}
            {selectedOption.series ? (
              <>
                {' '}
                is backed by <span className="mono">{selectedOption.series}</span>
              </>
            ) : (
              <> is the identity conversion and needs no backing series</>
            )}
            {windowText(selectedOption.coverage_from, selectedOption.coverage_to, calendar) ? (
              <>
                , covering{' '}
                {windowText(selectedOption.coverage_from, selectedOption.coverage_to, calendar)}
              </>
            ) : null}
            .
          </div>
          {/* The numéraire's own caveats — that USD_IRT is a free-market proxy
              and not an official rate, that a gram count is 18-karat and not
              troy ounces. They arrive as an ARRAY and are shown, not folded
              into a title attribute where nobody reads them. */}
          {(selectedOption.notes ?? []).length > 0 && (
            <ul className="mkt-note-list mkt-numeraire-notes">
              {(selectedOption.notes ?? []).map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {withheld.length > 0 && (
        <div className="muted small mkt-coverage">
          Not offered by this deployment:
          <ul className="mkt-note-list">
            {withheld.map((n) => (
              <li key={n.key}>
                <span className="mono">{n.key}</span> —{' '}
                {n.unavailable_reason?.trim() ||
                  'the registry marked it unavailable without giving a reason.'}
              </li>
            ))}
          </ul>
        </div>
      )}

      {(numeraires.data?.warnings ?? []).map((w, i) => (
        <div key={`num-warn-${i}`} className="callout callout-warn">
          {w}
        </div>
      ))}

      {perf.error && <ErrorMessage message={perf.error} onRetry={perf.reload} />}

      {warnings.map((w, i) => (
        <div key={i} className="callout callout-warn">
          {w}
        </div>
      ))}

      {perf.loading && !data ? (
        <Loading label="Loading purchasing-power table…" />
      ) : !data ? null : items.length === 0 ? (
        <EmptyState
          title="No asset has a measured window here"
          hint={`No instrument returned a ${MARKET_PERIOD_LABELS[period] ?? period} window in ${served}.`}
        />
      ) : (
        <>
          <div className="grid mkt-rankings">
            <div className="card mkt-rank">
              <div className="card-title">Best purchasing-power preservation</div>
              {bestReal ? (
                <>
                  <div className="stat-value">{bestReal.item.name_en || bestReal.item.code}</div>
                  <div className={`delta ${pctClass(bestReal.value)}`}>
                    {formatPct(bestReal.value)}
                  </div>
                  <div className="muted small">
                    Ranked on real (CPI-deflated) return · {bestReal.measured} of {items.length}{' '}
                    assets have one over this window.
                  </div>
                </>
              ) : (
                <div className="muted small">
                  No asset has a CPI-deflated return over this window, so nothing is ranked on it.
                </div>
              )}
            </div>

            <div className="card mkt-rank">
              <div className="card-title">Most inflation-lagged</div>
              {worstReal ? (
                <>
                  <div className="stat-value">{worstReal.item.name_en || worstReal.item.code}</div>
                  <div className={`delta ${pctClass(worstReal.value)}`}>
                    {formatPct(worstReal.value)}
                  </div>
                  <div className="muted small">
                    {/* Past tense, and nothing about what happens next: a lag is
                        a measurement over a window, not a debt the asset owes. */}
                    {worstReal.value < 0
                      ? `It has lagged CPI by ${formatPct(-worstReal.value, { sign: false })} over this window.`
                      : 'Every measured asset kept pace with CPI over this window.'}{' '}
                    Ranked on the lowest real (CPI-deflated) return · {worstReal.measured} of{' '}
                    {items.length} assets have one over this window.
                  </div>
                </>
              ) : (
                <div className="muted small">
                  No asset has a CPI-deflated return over this window, so nothing is ranked on it.
                </div>
              )}
            </div>

            <div className="card mkt-rank">
              <div className="card-title">Highest drawdown</div>
              {deepestDrawdown ? (
                <>
                  <div className="stat-value">
                    {deepestDrawdown.item.name_en || deepestDrawdown.item.code}
                  </div>
                  <div className="delta flat mono">{formatPct(deepestDrawdown.value)}</div>
                  <div className="muted small">
                    Ranked on maximum peak-to-trough drawdown inside the window ·{' '}
                    {deepestDrawdown.measured} of {items.length} assets have one.
                  </div>
                </>
              ) : (
                <div className="muted small">
                  No asset has a measured drawdown over this window, so nothing is ranked on it.
                </div>
              )}
            </div>
          </div>

          <div className="card">
            <div className="row space-between wrap">
              <div className="card-title">
                {MARKET_PERIOD_LABELS[period] ?? period} · numéraire {served} · values in{' '}
                {valueUnit.long}
              </div>
              <div className="muted small">
                {formatDate(data.from, calendar)} → {formatDate(data.to, calendar)}
              </div>
            </div>

            <div className="muted small mkt-cpi">
              {data.cpi_series ? (
                <>
                  Real returns are deflated by <span className="mono">{data.cpi_series}</span>
                  {data.cpi_coverage_to ? (
                    <>
                      , whose coverage ends {formatDate(data.cpi_coverage_to, calendar)} — a real
                      return can therefore span a shorter window than the one selected, and each
                      cell carries its own.
                    </>
                  ) : (
                    '.'
                  )}
                </>
              ) : (
                'No CPI series is configured for this deployment, so real returns are not computed.'
              )}
              {data.cpi_provenance ? (
                <div className="mkt-cpi-prov">
                  {/* A real return is uninterpretable without the deflator's
                      identity. Base period especially: WB_CPI_IRN is the World
                      Bank's rebase, not the Statistical Centre of Iran's own
                      index, and the two are not comparable level-for-level. */}
                  <span className="mono">{data.cpi_provenance.base_period || 'base not stated'}</span>
                  {' · '}
                  {data.cpi_provenance.frequency === 'A' ? 'annual' : data.cpi_provenance.frequency}
                  {' · '}
                  <span className="tag-quality">{data.cpi_provenance.quality_tier.replace(/_/g, ' ')}</span>
                  {data.cpi_provenance.notes ? (
                    <div className="mkt-cpi-note">{data.cpi_provenance.notes}</div>
                  ) : null}
                </div>
              ) : null}
            </div>

            <div className="table-wrap">
              <table className="table mkt-table">
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
                  </tr>
                </thead>
                <tbody>
                  {rows.map((item) => {
                    const notes = item.notes ?? []
                    const isOpen = expanded[item.code] === true
                    const value = formatEndValue(item, valueUnit, unit)
                    const observations = finiteOrNull(item.observations)
                    const realWindow = windowText(
                      item.real_return_from,
                      item.real_return_to,
                      calendar
                    )
                    const usdWindow = windowText(item.usd_return_from, item.usd_return_to, calendar)
                    const goldWindow = windowText(
                      item.gold_return_from,
                      item.gold_return_to,
                      calendar
                    )
                    return [
                      <tr key={item.code} data-testid={`mkt-row-${item.code}`}>
                        <td>
                          <div className="mkt-asset">
                            <span className="mkt-asset-name">{item.name_en || item.code}</span>
                            {item.name_fa && (
                              <span className="bidi-fa" lang="fa" dir="rtl">
                                {item.name_fa}
                              </span>
                            )}
                          </div>
                          <div className="mkt-asset-meta">
                            <span className="mono muted small">{item.code}</span>
                            <span className="muted small">
                              quoted in {item.quote_currency}
                              {item.unit ? ` / ${item.unit}` : ''}
                            </span>
                            <Provenance tier={item.quality_tier} isProxy={item.is_proxy} />
                            {notes.length > 0 && (
                              <button
                                type="button"
                                className="btn btn-ghost btn-sm mkt-why"
                                aria-expanded={isOpen}
                                onClick={() =>
                                  setExpanded((prev) => ({ ...prev, [item.code]: !isOpen }))
                                }
                              >
                                {isOpen ? 'Hide notes' : `Why? (${notes.length})`}
                              </button>
                            )}
                          </div>
                        </td>
                        <td className="num" title={`In ${valueUnit.long}.`}>
                          {value === null ? (
                            <Absent reason={absentReason(item, 'A value')} />
                          ) : (
                            <span className="mono">{value}</span>
                          )}
                        </td>
                        <td className="num">
                          <Metric
                            value={finiteOrNull(item.nominal_return_pct)}
                            reason={absentReason(item, 'A nominal return')}
                          />
                        </td>
                        <td className="num" title={realWindow ? `Deflated over ${realWindow}.` : undefined}>
                          <Metric
                            value={finiteOrNull(item.real_return_pct)}
                            reason={absentReason(item, 'A real (CPI-deflated) return')}
                          />
                        </td>
                        <td className="num" title={usdWindow ? `Measured over ${usdWindow}.` : undefined}>
                          <Metric
                            value={finiteOrNull(item.usd_return_pct)}
                            reason={absentReason(item, 'A USD return')}
                          />
                        </td>
                        <td
                          className="num"
                          title={goldWindow ? `Measured over ${goldWindow}.` : undefined}
                        >
                          <Metric
                            value={finiteOrNull(item.gold_return_pct)}
                            reason={absentReason(item, 'A gold return')}
                          />
                        </td>
                        <td className="num">
                          {/* Volatility is a magnitude, not a signed result: tinting it
                              green above zero would read as approval. */}
                          <Metric
                            value={volatilityOf(item)}
                            reason={absentReason(item, 'An observation volatility')}
                            tint={false}
                            signed={false}
                          />
                        </td>
                        <td className="num">
                          <Metric
                            value={finiteOrNull(item.max_drawdown_pct)}
                            reason={absentReason(item, 'A maximum drawdown')}
                            tint={false}
                          />
                        </td>
                        <td className="num">
                          {observations === null ? (
                            <Absent reason={absentReason(item, 'An observation count')} />
                          ) : (
                            <span className="mono">{formatGrouped(observations)}</span>
                          )}
                        </td>
                      </tr>,
                      isOpen ? (
                        <tr key={`${item.code}-notes`} className="mkt-note-row">
                          <td colSpan={columns.length}>
                            <div className="mkt-note-window">
                              Covered{' '}
                              {windowText(item.coverage_from, item.coverage_to, calendar) ||
                                'over no observation at all'}
                              {realWindow ? ` · real return deflated over ${realWindow}` : ''}
                            </div>
                            <ul className="mkt-note-list">
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

            <div className="muted small mkt-footer">
              Observation volatility is the standard deviation of log returns between consecutive
              observations and is not annualised. Sorting keeps un-measured rows at the bottom in
              both directions: a dash is not a smallest value.
            </div>
          </div>
        </>
      )}
    </div>
  )
}
